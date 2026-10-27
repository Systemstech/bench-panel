#!/usr/bin/env python3
"""Self-test for the plugin manager's recovery path and connection addresses.

    python3 tests/test_manager.py          # exit 0 = all good (deploy.sh runs this)

Covers three faults found on a desktop install 2026-09-24, after the plugin standard gained S17:
  * a plugin refused at start was listed as "Install" (as if absent), not "Update";
  * two installs of one plugin in the same second collided on the .old-<id>-<time>
    backup name, and the OSError dropped the request ("Failed to fetch");
  * a dotted address with a leading zero ("192.0.2.010") reached the C resolver,
    which reads it as OCTAL (.8, not .10).
Runs no instruments and no panel.
"""
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from benchcore import transport  # noqa: E402
from benchcore.plugins import PluginStore  # noqa: E402

CATALOG = os.path.join(ROOT, "catalog")
fails = 0


def check(what, cond):
    global fails
    print(f"{'ok  ' if cond else 'FAIL'}  {what}")
    fails += not cond


def stale_copy(dest, pid):
    """The catalog plugin as an older core would have shipped it: no action docs."""
    shutil.copytree(os.path.join(CATALOG, pid), dest)
    mf = os.path.join(dest, "manifest.json")
    with open(mf) as fh:
        m = json.load(fh)
    m["version"] = "1.1.0"
    for a in m["actions"].values():
        a.pop("doc", None)
    with open(mf, "w") as fh:
        json.dump(m, fh, indent=2)


with tempfile.TemporaryDirectory() as tmp:
    inst = os.path.join(tmp, "plugins")
    os.makedirs(inst)
    stale_copy(os.path.join(inst, "e3641a"), "e3641a")
    store = PluginStore(inst, CATALOG)
    store.load_all()
    check("a stale plugin is refused at load", "e3641a" in store.broken and store.get("e3641a") is None)

    row = {c["id"]: c for c in store.listing()["catalog"]}
    check("the catalog reports the refused plugin's on-disk version",
          row["e3641a"]["installed_version"] == "1.1.0")
    check("the catalog marks it not_loaded", row["e3641a"]["not_loaded"] is True)
    check("an absent plugin is still plain Install",
          row["34401a"]["installed_version"] is None and row["34401a"]["not_loaded"] is False)

    r1 = store.install_catalog("e3641a", busy_ids=set())
    check("Update from the catalog loads it", store.get("e3641a") is not None and "e3641a" not in store.broken)
    row = {c["id"]: c for c in store.listing()["catalog"]}
    check("after the update it is no longer not_loaded", row["e3641a"]["not_loaded"] is False)

    # Same second, same plugin, three times: every one must succeed with its own backup.
    backups = [r1["backup"]]
    for _ in range(2):
        try:
            backups.append(store.install_catalog("e3641a", busy_ids=set())["backup"])
        except OSError as e:
            backups.append(f"OSError {e}")
    check("back-to-back installs each keep a distinct backup",
          len(set(backups)) == 3 and not any(b.startswith("OSError") for b in backups))
    check("every backup folder exists", all(os.path.isdir(os.path.join(inst, b)) for b in backups))

# Addresses: decimal is what a person means.
for typed, want in [("192.0.2.05", "192.0.2.5"), ("192.0.002.010", "192.0.2.10"),
                    (" 10.0.0.1 ", "10.0.0.1"), ("bench-gen.local", "bench-gen.local")]:
    spec, err = transport.validate({"type": "vxi11", "host": typed})
    check(f"vxi11 host {typed!r} -> {want!r}", err is None and spec["host"] == want)
spec, err = transport.validate({"type": "vxi11", "host": "192.0.2.256"})
check("a part over 255 is refused, not passed on", spec is None and "0-255" in err)
spec, err = transport.validate({"type": "gpib-gateway", "gateway": "192.0.2.060", "address": 5})
check("the GPIB gateway address is made decimal too", err is None and spec["gateway"] == "192.0.2.60")

print("ALL OK" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
