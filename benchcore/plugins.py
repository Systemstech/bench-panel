#!/usr/bin/env python3
"""Instrument plugins -- find them, check them, load them, install them.

A PLUGIN IS ONE FOLDER
----------------------
    <id>/manifest.json   what it is: model, *IDN? match, has_output, connections
    <id>/driver.py       one Instrument subclass (benchcore.instrument)
    <id>/card.html       the card body -- markup only, no ids (see PLUGINS.md)
    <id>/card.js         registers the card with BenchPanel.register(<id>, {...})
    <id>/sim.py          optional: a model of the instrument for the /sim/ bench
    <id>/README.md       optional, shown nowhere yet but shipped with it

The core knows nothing about any instrument. It loads whatever is in the INSTALLED
directory and nothing else; there is no built-in list to fall back on.

WHERE PLUGINS MAY COME FROM  (decided 2026-09-23: trusted sources only)
-----------------------------------------------------------------------
A plugin is Python that runs with the bench in reach -- it can switch on a power
supply. So there are exactly two sources, and no "install from URL":
  catalog   the catalog/ folder that ships with this program (git pull updates it)
  upload    a .zip the operator chose on their own machine
Both go through the same two steps: INSPECT (manifest, file list, SHA-256 shown to
the operator, nothing written) then INSTALL (only after they confirm). An upload
is held in memory between the two, keyed by its hash, so what gets installed is
byte-for-byte what was shown.
"""
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import threading
import time
import zipfile

from . import conformance, transport
from .instrument import Instrument

SCHEMA = 1
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,39}$")
ALLOWED_EXT = {".py", ".json", ".html", ".js", ".css", ".svg", ".md", ".txt", ".png"}
MAX_FILE = 2 * 1024 * 1024
MAX_TOTAL = 5 * 1024 * 1024
STAGE_TTL_S = 900

_mod_seq = 0
_mod_guard = threading.Lock()


class PluginError(Exception):
    pass


def check_manifest(m):
    """Validate a parsed manifest. Returns a list of problems (empty = good)."""
    bad = []
    if not isinstance(m, dict):
        return ["manifest.json is not an object"]
    if m.get("schema") != SCHEMA:
        bad.append(f"schema must be {SCHEMA} (got {m.get('schema')!r})")
    if not ID_RE.match(str(m.get("id", ""))):
        bad.append("id must be 2-40 of a-z 0-9 _ - starting with a letter or digit")
    for k in ("name", "model", "version"):
        if not isinstance(m.get(k), str) or not m.get(k).strip():
            bad.append(f"{k} is required")
    idm = m.get("idn_match")
    if not (isinstance(idm, list) and idm and all(isinstance(x, str) and x for x in idm)):
        bad.append("idn_match must be a non-empty list of strings")
    if not isinstance(m.get("has_output"), bool):
        bad.append("has_output must be true or false -- the safety rules depend on it")
    conns = m.get("connections")
    if not (isinstance(conns, list) and conns and all(c in transport.TYPES for c in conns)):
        bad.append(f"connections must list one or more of {', '.join(transport.TYPES)}")
    drv = m.get("driver") or {}
    if not (isinstance(drv, dict) and drv.get("file") and drv.get("class")):
        bad.append("driver needs file and class")
    card = m.get("card") or {}
    if not (isinstance(card, dict) and card.get("html") and card.get("js")):
        bad.append("card needs html and js")
    # Instruments older than IEEE 488.2 have no *IDN? and no *OPC? (the HP 438A answers
    # ?ID, and an unknown code shows an entry error on its display and aborts a ZERO in
    # progress). They name their own identify query, and a keepalive that is harmless to
    # them: another query, or "serial-poll" (a bus operation, not a program code).
    ident = m.get("identify", "*IDN?")
    if not (isinstance(ident, str) and 0 < len(ident) <= 32 and ident.isprintable()):
        bad.append("identify, when present, is the query that returns the instrument's identity")
    keep = m.get("keepalive", "*OPC?")
    if not (keep == "serial-poll" or (isinstance(keep, str) and 0 < len(keep) <= 32
                                      and keep.isprintable() and keep.endswith("?"))):
        bad.append('keepalive, when present, is "serial-poll" or a query ending in "?"')
    sim = m.get("sim")
    if sim is not None and not (isinstance(sim, dict) and sim.get("file") and sim.get("class")):
        bad.append("sim, when present, needs file and class")
        sim = None
    for f in (drv.get("file"), card.get("html"), card.get("js"), (sim or {}).get("file")):
        if f and not _safe_name(f):
            bad.append(f"file name {f!r} must be a plain name inside the plugin folder")
    return bad


def _safe_name(name):
    return bool(name) and "/" not in name and "\\" not in name and not name.startswith(".")


def read_manifest(folder):
    path = os.path.join(folder, "manifest.json")
    try:
        with open(path, encoding="utf-8") as fh:
            m = json.load(fh)
    except FileNotFoundError:
        raise PluginError(f"no manifest.json in {folder}")
    except json.JSONDecodeError as e:
        raise PluginError(f"manifest.json does not parse: {e}")
    bad = check_manifest(m)
    if bad:
        raise PluginError("; ".join(bad))
    for f in (m["driver"]["file"], m["card"]["html"], m["card"]["js"], (m.get("sim") or {}).get("file")):
        if f and not os.path.isfile(os.path.join(folder, f)):
            raise PluginError(f"manifest names {f} but the folder has no such file")
    return m


def load_driver(folder, m):
    """Import driver.py under a fresh module name and return its class.

    A fresh name every time means an upgraded plugin really runs its new code,
    instead of Python handing back the module it already imported.
    """
    global _mod_seq
    with _mod_guard:
        _mod_seq += 1
        modname = f"benchplugin_{m['id'].replace('-', '_')}_{_mod_seq}"
    path = os.path.join(folder, m["driver"]["file"])
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as e:
        raise PluginError(f"driver failed to import: {type(e).__name__}: {e}")
    cls = getattr(mod, m["driver"]["class"], None)
    if not (isinstance(cls, type) and issubclass(cls, Instrument)):
        raise PluginError(f"{m['driver']['class']} is not an Instrument subclass")
    # The manifest is the authority. A driver that declares its own has_output
    # and disagrees is refused: one of the two is wrong about a safety fact.
    own = cls.__dict__.get("has_output")
    if own is not None and own != m["has_output"]:
        raise PluginError(f"driver says has_output={own} but the manifest says "
                          f"{m['has_output']} -- refusing to guess which is right")
    if m["has_output"] and cls.all_off is Instrument.all_off:
        raise PluginError("has_output is true but the driver has no all_off()")
    cls.plugin_id = m["id"]
    cls.model = m["model"]
    cls.idn_match = tuple(m["idn_match"])
    cls.has_output = m["has_output"]
    cls.idn_query = m.get("identify", "*IDN?")
    cls.keepalive = m.get("keepalive", "*OPC?")
    return cls


class Plugin:
    _revs = 0

    def __init__(self, folder, manifest, cls):
        # rev changes on every load, so the page knows to rebuild the cards of a
        # plugin that was just upgraded -- even if its version string did not change.
        Plugin._revs += 1
        self.rev = Plugin._revs
        self.folder = folder
        self.manifest = manifest
        self.cls = cls
        self.id = manifest["id"]

    def asset(self, name):
        """Path of a card file, only if the manifest names it."""
        if name not in (self.manifest["card"]["html"], self.manifest["card"]["js"]):
            return None
        return os.path.join(self.folder, name)

    def summary(self):
        m = self.manifest
        return {k: m.get(k) for k in ("id", "name", "vendor", "model", "version",
                                      "has_output", "connections", "capabilities", "description",
                                      "quirks", "author", "license")}


class PluginStore:
    def __init__(self, installed_dir, catalog_dir):
        self.installed_dir = installed_dir
        self.catalog_dir = catalog_dir
        self.plugins = {}           # id -> Plugin (loaded and usable)
        self.broken = {}            # folder name -> why it did not load
        self._staged = {}           # sha256 -> (time, zip bytes, manifest)
        self.lock = threading.RLock()
        os.makedirs(installed_dir, exist_ok=True)

    # -- installed ----------------------------------------------------------
    def load_all(self):
        with self.lock:
            self.plugins, self.broken = {}, {}
            for name in sorted(os.listdir(self.installed_dir)):
                folder = os.path.join(self.installed_dir, name)
                if name.startswith(".") or not os.path.isdir(folder):
                    continue
                try:
                    self._load(folder, expect_id=name)
                except PluginError as e:
                    self.broken[name] = str(e)

    def _load(self, folder, expect_id):
        m = read_manifest(folder)
        if m["id"] != expect_id:
            raise PluginError(f"folder is {expect_id} but manifest id is {m['id']}")
        # The standard is enforced here too, not only at install: a plugin copied
        # in by hand, or one that conformed to an older core, is judged at start.
        rep = conformance.check_plugin(folder)
        if not rep.ok:
            raise PluginError("does not meet the plugin standard -- " + rep.summary())
        cls = load_driver(folder, m)
        p = Plugin(folder, m, cls)
        self.plugins[p.id] = p
        self.broken.pop(expect_id, None)
        return p

    def get(self, pid):
        return self.plugins.get(pid)

    def listing(self):
        with self.lock:
            installed = [p.summary() for p in self.plugins.values()]
            catalog = []
            for m in self.catalog():
                have = self.plugins.get(m["id"])
                version = have.manifest["version"] if have else self._broken_version(m["id"])
                catalog.append({"id": m["id"], "name": m["name"], "model": m["model"],
                                "version": m["version"], "has_output": m["has_output"],
                                "capabilities": m.get("capabilities"),
                                "connections": m["connections"],
                                "installed_version": version,
                                # On disk but refused at load: the page must offer
                                # Update, not Install -- it IS installed, just unusable.
                                "not_loaded": have is None and m["id"] in self.broken})
            return {"installed": installed, "catalog": catalog,
                    "broken": [{"folder": k, "error": v} for k, v in self.broken.items()]}

    def _broken_version(self, pid):
        """Version of a plugin folder that is on disk but did not load, or None."""
        if pid not in self.broken:
            return None
        try:
            return read_manifest(os.path.join(self.installed_dir, pid))["version"]
        except (PluginError, KeyError):
            return "?"

    # -- catalog ------------------------------------------------------------
    def catalog(self):
        out = []
        if not os.path.isdir(self.catalog_dir):
            return out
        for name in sorted(os.listdir(self.catalog_dir)):
            folder = os.path.join(self.catalog_dir, name)
            if not os.path.isdir(folder):
                continue
            try:
                m = read_manifest(folder)
                if m["id"] == name:
                    out.append(m)
            except PluginError:
                pass
        return out

    def inspect_catalog(self, pid):
        folder = os.path.join(self.catalog_dir, pid)
        if not ID_RE.match(pid or "") or not os.path.isdir(folder):
            raise PluginError(f"{pid!r} is not in the catalog")
        m = read_manifest(folder)
        rep = conformance.check_plugin(folder, dynamic=False)
        return self._inspection(m, sorted(_plugin_files(folder)), _hash_folder(folder), "catalog", rep)

    def install_catalog(self, pid, busy_ids):
        folder = os.path.join(self.catalog_dir, pid)
        m = read_manifest(folder)
        if m["id"] != pid:
            raise PluginError("catalog folder and manifest id disagree")
        files = {}
        for name in _plugin_files(folder):
            with open(os.path.join(folder, name), "rb") as fh:
                files[name] = fh.read()
        return self._install(m, files, busy_ids)

    # -- upload -------------------------------------------------------------
    def inspect_zip(self, data):
        m, files = _unpack_zip(data)
        digest = hashlib.sha256(data).hexdigest()
        # Static checks only: they read the files and run none of the plugin's code,
        # so the operator sees the verdict BEFORE deciding to trust it.
        tmp = os.path.join(self.installed_dir, f".inspect-{m['id']}")
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp)
        try:
            for name, raw in files.items():
                with open(os.path.join(tmp, name), "wb") as fh:
                    fh.write(raw)
            rep = conformance.check_plugin(tmp, dynamic=False)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        with self.lock:
            now = time.time()
            for k in [k for k, v in self._staged.items() if now - v[0] > STAGE_TTL_S]:
                del self._staged[k]
            self._staged[digest] = (now, data, m)
        return self._inspection(m, sorted(files), digest, "upload", rep)

    def install_zip(self, digest, busy_ids):
        with self.lock:
            staged = self._staged.pop(digest, None)
        if staged is None:
            raise PluginError("that upload is no longer staged -- inspect it again")
        m, files = _unpack_zip(staged[1])
        return self._install(m, files, busy_ids)

    def _inspection(self, m, files, digest, source, rep):
        have = self.plugins.get(m["id"])
        return {"source": source, "sha256": digest, "files": files,
                "manifest": m, "standard": rep.as_dict(),
                "replaces": have.manifest["version"] if have else None}

    # -- the one write path -------------------------------------------------
    def _install(self, m, files, busy_ids):
        """Write the plugin folder atomically and load it.

        An upgrade is refused while any instrument using the plugin is ON in the
        panel: swapping a driver under a live session is how outputs get stranded.
        The old folder is kept as .old-<id>-<time> so an upgrade can be undone.
        """
        pid = m["id"]
        if pid in busy_ids:
            raise PluginError(f"switch every {m['model']} OFF in the panel before "
                              "replacing its plugin")
        with self.lock:
            dest = os.path.join(self.installed_dir, pid)
            tmp = os.path.join(self.installed_dir, f".new-{pid}")
            shutil.rmtree(tmp, ignore_errors=True)
            os.makedirs(tmp)
            for name, raw in files.items():
                with open(os.path.join(tmp, name), "wb") as fh:
                    fh.write(raw)
            # Prove it meets the standard -- the full check, which runs the driver
            # against a pretend connection -- BEFORE it replaces anything.
            try:
                rep = conformance.check_plugin(tmp)
                if not rep.ok:
                    raise PluginError("does not meet the plugin standard -- " + rep.summary())
            except PluginError:
                shutil.rmtree(tmp, ignore_errors=True)
                raise
            old = None
            if os.path.isdir(dest):
                old = self._backup_path(pid)
                os.rename(dest, old)
            os.rename(tmp, dest)
            p = self._load(dest, expect_id=pid)
            return {"id": pid, "version": p.manifest["version"], "backup": old and os.path.basename(old)}

    def _backup_path(self, pid):
        """A fresh .old-<id>-<time> name. Two installs in the same second (a
        double-click) must not collide -- rename onto an existing folder fails."""
        base = os.path.join(self.installed_dir, f".old-{pid}-{time.strftime('%Y%m%d-%H%M%S')}")
        path, n = base, 1
        while os.path.exists(path):
            n += 1
            path = f"{base}-{n}"
        return path

    def uninstall(self, pid, used_by):
        if used_by:
            raise PluginError(f"remove {', '.join(used_by)} from the bench first -- "
                              "they use this plugin")
        with self.lock:
            dest = os.path.join(self.installed_dir, pid)
            if not ID_RE.match(pid or "") or not os.path.isdir(dest):
                raise PluginError(f"{pid!r} is not installed")
            old = self._backup_path(pid)
            os.rename(dest, old)
            self.plugins.pop(pid, None)
            self.broken.pop(pid, None)
            return {"id": pid, "backup": os.path.basename(old)}


def _plugin_files(folder):
    """The files that make up a plugin folder: flat, allowed types, no dotfiles.

    __pycache__ and the like are left behind -- they are build output, not plugin.
    """
    return [n for n in os.listdir(folder)
            if os.path.isfile(os.path.join(folder, n)) and not n.startswith(".")
            and os.path.splitext(n)[1].lower() in ALLOWED_EXT]


def _hash_folder(folder):
    """SHA-256 over exactly the files an install would copy, in name order."""
    h = hashlib.sha256()
    for name in sorted(_plugin_files(folder)):
        h.update(name.encode() + b"\0")
        with open(os.path.join(folder, name), "rb") as fh:
            h.update(fh.read())
    return h.hexdigest()


def _unpack_zip(data):
    """Read a plugin zip in memory. Nothing touches the disk here.

    Accepts either the plugin's files at the top level or inside one folder.
    Refuses anything that could land outside the plugin folder, nested folders,
    file types a plugin has no business shipping, and oversize archives.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise PluginError("that is not a zip file")
    entries = [i for i in zf.infolist() if not i.is_dir()]
    if not entries:
        raise PluginError("the zip is empty")
    names = [i.filename for i in entries]
    tops = {n.split("/", 1)[0] for n in names}
    prefix = ""
    if all("/" in n for n in names) and len(tops) == 1:
        prefix = tops.pop() + "/"
    files, total = {}, 0
    for i in entries:
        rel = i.filename[len(prefix):]
        if not _safe_name(rel) or ".." in rel:
            raise PluginError(f"refusing {i.filename!r}: a plugin is one flat folder")
        if os.path.splitext(rel)[1].lower() not in ALLOWED_EXT:
            raise PluginError(f"refusing {rel!r}: not a file type a plugin ships")
        if i.file_size > MAX_FILE:
            raise PluginError(f"{rel} is larger than {MAX_FILE // 1024} KB")
        total += i.file_size
        if total > MAX_TOTAL:
            raise PluginError("the plugin is larger than 5 MB unpacked")
        files[rel] = zf.read(i)
    if "manifest.json" not in files:
        raise PluginError("no manifest.json in the zip")
    try:
        m = json.loads(files["manifest.json"].decode("utf-8"))
    except Exception as e:
        raise PluginError(f"manifest.json does not parse: {e}")
    bad = check_manifest(m)
    if bad:
        raise PluginError("; ".join(bad))
    for f in (m["driver"]["file"], m["card"]["html"], m["card"]["js"]):
        if f not in files:
            raise PluginError(f"manifest names {f} but the zip has no such file")
    return m, files
