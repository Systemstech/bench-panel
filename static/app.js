// Bench panel -- the page's core. It knows NO instrument.
//
// It draws a frame for every instrument in the site config (name, where, power
// button, messages) and hands the inside of the frame to that instrument's
// plugin: plugin/<id>/card.html is inserted, and plugin/<id>/card.js registers
//
//     BenchPanel.register('<plugin id>', {
//       mount(card)      { ...wire up controls once... },
//       update(card, st) { ...paint the latest state... },
//     });
//
// `card` is everything a plugin may touch -- see PLUGINS.md for the full list.
// A card may appear more than once (two 34401As), so plugins never use element
// ids: they find their elements with card.k('name') -> [data-k="name"].
'use strict';

const $ = id => document.getElementById(id);

// Every URL is RELATIVE to wherever this page is served from. An absolute
// '/api/state' works on localhost and silently breaks behind a /bench/ proxy.
const BASE = (() => {
  let p = location.pathname;
  if (/\.[a-z0-9]+$/i.test(p)) p = p.replace(/[^/]*$/, '');
  return p.endsWith('/') ? p : p + '/';
})();

// A non-JSON reply means the request went somewhere unexpected. Say that,
// rather than letting a parser error surface as the whole diagnosis.
async function api(path, opts) {
  const r = await fetch(BASE + path, opts);
  const text = await r.text();
  try {
    return JSON.parse(text);
  } catch {
    throw new Error(`${r.status} from ${BASE + path} — expected JSON, got `
      + (text.trim().slice(0, 40) || 'an empty reply'));
  }
}
// X-Bench-Panel: the server refuses any POST without it (cross-site guard).
function post(path, body) {
  return api(path, {method: 'POST',
    headers: {'Content-Type': 'application/json', 'X-Bench-Panel': '1'},
    body: JSON.stringify(body)}).catch(e => ({ok: false, msg: String(e.message || e)}));
}

const esc = s => String(s ?? '').replace(/[<>&"]/g,
  c => ({'<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;'}[c]));

// 9.9e37 is the SCPI overload sentinel. Printed raw it looks like a real reading.
const fmt = (v, d = 4) => {
  if (v === null || v === undefined || Number.isNaN(v)) return '--';
  if (Math.abs(v) >= 9e37) return 'OVLD';
  if (Math.abs(v) >= 1e6 || (v !== 0 && Math.abs(v) < 1e-4)) return v.toExponential(3);
  return v.toFixed(d);
};
function fmtHz(hz) {
  if (hz == null) return ['---', ''];
  if (hz >= 1e6) return [(hz / 1e6).toFixed(6), 'MHz'];
  if (hz >= 1e3) return [(hz / 1e3).toFixed(6), 'kHz'];
  return [hz.toFixed(6), 'Hz'];
}

// Two-click arm for anything that can switch an output on. The server refuses
// those actions without a confirmed flag anyway; this is the human half.
function disarm(btn) {
  clearTimeout(btn._armT);
  if (btn.dataset.armed === '1') {
    btn.dataset.armed = ''; btn.classList.remove('sel');
    btn.textContent = btn.dataset.label;
  }
}
function arm(btn, armedText, fn) {
  if (btn.dataset.armed !== '1') {
    btn.dataset.armed = '1'; btn.dataset.label = btn.textContent;
    btn.classList.add('sel'); btn.textContent = armedText;
    btn._armT = setTimeout(() => disarm(btn), 4000);
    return false;
  }
  disarm(btn);
  fn();
  return true;
}
const isArmed = btn => btn.dataset.armed === '1';

// A row of choice buttons, rebuilt only when the choices change.
// items: strings, or {label, value}.
function buttons(host, items, current, fn) {
  const norm = items.map(it => typeof it === 'object' ? it : {label: String(it), value: it});
  const key = JSON.stringify(norm);
  if (host.dataset.keys !== key) {
    host.dataset.keys = key;
    host.innerHTML = '';
    norm.forEach(it => {
      const b = document.createElement('button');
      b.textContent = it.label; b.dataset.v = it.value;
      b.onclick = () => fn(it.value);
      host.appendChild(b);
    });
  }
  [...host.children].forEach(b =>
    b.classList.toggle('sel', String(b.dataset.v) === String(current)));
}

// ---------------------------------------------------------------- plugins
const REG = {};          // plugin id -> {def, html, rev, ready: Promise}
window.BenchPanel = {
  register(id, def) { (REG[id] = REG[id] || {}).def = def; },
};

function ensurePlugin(id, rev) {
  const have = REG[id];
  if (have && have.rev === rev && have.ready) return have.ready;
  const entry = REG[id] = {rev, def: null};
  entry.ready = (async () => {
    const r = await fetch(`${BASE}plugin/${id}/card.html?r=${rev}`);
    if (!r.ok) throw new Error(`card.html: ${r.status}`);
    entry.html = await r.text();
    document.querySelectorAll(`script[data-plugin="${id}"]`).forEach(s => s.remove());
    await new Promise((ok, fail) => {
      const s = document.createElement('script');
      s.src = `${BASE}plugin/${id}/card.js?r=${rev}`;
      s.dataset.plugin = id;
      s.onload = ok; s.onerror = () => fail(new Error('card.js failed to load'));
      document.head.appendChild(s);
    });
    if (!entry.def) throw new Error(`card.js did not call BenchPanel.register('${id}', …)`);
  })();
  return entry.ready;
}

// ---------------------------------------------------------------- cards
const PWR_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" '
  + 'stroke-linecap="round"><path d="M12 3v9"/><path d="M7.05 6.05a7 7 0 1 0 9.9 0"/></svg>';
const CARDS = {};        // instance id -> card
let lastSnap = null;

function makeCard(inst) {
  const el = document.createElement('div');
  el.className = 'panel off';
  el.innerHTML = `<div class="phead"><span class="dot"></span><b></b><small></small>
    <span class="grow"></span><span class="pills"></span>
    <button class="pwr" title="Switch this instrument ON/OFF in the panel (not at the bench)">${PWR_SVG}</button></div>
    <div class="offnote"></div><div class="body"></div><div class="msg"></div>`;
  const q = s => el.querySelector(s);
  const card = {
    id: inst.id, plugin: inst.plugin, rev: inst.plugin_rev, el,
    head: q('.pills'), body: q('.body'), local: {}, state: inst, mounted: false,
    k: name => q(`.body [data-k="${name}"]`),
    msg: t => { q('.msg').textContent = t || ''; },
    async act(action, args = {}) {
      const r = await post('api/action', {instrument: card.id, action, args});
      card.msg(r.errors && r.errors.length ? r.errors.join(' | ') : (r.msg || (r.ok ? '' : 'refused')));
      refresh();
      return r;
    },
    repaint: () => paint(card, card.state),
    arm, disarm, isArmed, buttons, fmt, fmtHz, esc,
  };
  q('.pwr').onclick = () => power(card);
  CARDS[inst.id] = card;
  if (!inst.missing) {
    card.body.innerHTML = '<p class="msg">loading plugin …</p>';
    ensurePlugin(inst.plugin, inst.plugin_rev).then(() => {
      if (CARDS[inst.id] !== card) return;             // removed meanwhile
      card.body.innerHTML = REG[inst.plugin].html;
      REG[inst.plugin].def.mount(card);
      card.mounted = true;
      paint(card, card.state);
    }).catch(e => { card.body.innerHTML = ''; card.msg(`Plugin ${inst.plugin}: ${e.message}`); });
  }
  return card;
}

async function power(card) {
  if (card.busy || card.state.missing) return;
  const b = card.el.querySelector('.pwr');
  const turningOn = !card.state.enabled;
  card.busy = true; b.classList.add('busy');
  card.el.querySelector('.offnote').textContent = turningOn
    ? 'Connecting — asking it to identify itself …' : 'Switching off …';
  const r = await post('api/action', {instrument: card.id,
    action: turningOn ? 'power_on' : 'power_off'});
  card.busy = false; b.classList.remove('busy');
  card.msg(r.ok ? '' : (r.msg || 'refused'));
  refresh();
}

function paint(card, x) {
  card.state = x;
  const el = card.el, q = s => el.querySelector(s);
  const on = !!x.enabled;
  el.classList.toggle('off', !on);
  el.classList.toggle('missing', !!x.missing);
  q('.phead b').textContent = x.name;
  q('.dot').classList.toggle('on', on && !!x.connected);
  q('.pwr').classList.toggle('on', on);
  q('.pwr').style.display = x.missing ? 'none' : '';
  const where = on ? (x.idn || `${x.where} — ${x.error || 'reconnecting'}`) : `${x.where} — off`;
  q('.phead small').textContent = where; q('.phead small').title = where;
  if (!card.busy) {
    const note = q('.offnote');
    note.classList.toggle('bad', !!(x.power_msg || x.missing));
    note.textContent = x.missing
      ? `Can't load this instrument: ${x.missing}. Fix it in Instruments.`
      : (x.power_msg || (window.SIMULATED
        ? `OFF in the panel. This ${x.model || 'instrument'} is a simulation — press ⏻ to switch it on.`
        : `OFF in the panel — nothing is sent to ${x.where}. `
          + `Switch the ${x.model || 'instrument'} on at the bench, then press ⏻.`));
  }
  if (card.mounted && !x.missing) {
    try { REG[card.plugin].def.update(card, x); }
    catch (e) { card.msg(`Plugin ${card.plugin}: ${e.message}`); }
  }
}

function reconcile(list) {
  const host = $('cards');
  const want = new Set(list.map(i => i.id));
  for (const id of Object.keys(CARDS)) {
    const c = CARDS[id], i = list.find(x => x.id === id);
    // Gone from the bench, or its plugin was replaced: rebuild from scratch.
    if (!want.has(id) || (i && (i.plugin_rev !== c.rev || !!i.missing !== !!c.state.missing))) {
      c.el.remove(); delete CARDS[id];
    }
  }
  list.forEach((inst, n) => {
    const card = CARDS[inst.id] || makeCard(inst);
    if (host.children[n] !== card.el) host.insertBefore(card.el, host.children[n] || null);
    paint(card, inst);
  });
  let empty = host.querySelector('.empty');
  if (!list.length && !empty) {
    empty = document.createElement('div'); empty.className = 'empty';
    empty.innerHTML = '<h2>No instruments on this bench yet</h2>'
      + '<p>Install a plugin for your instrument, then add it with its GPIB address or network address.</p>'
      + '<button class="go" onclick="Manager.open()">Add an instrument</button>';
    host.appendChild(empty);
  } else if (list.length && empty) {
    empty.remove();
  }
}

// ---------------------------------------------------------------- refresh
let lastLog = -1, polling = true;
// The same page serves the real bench at <panel>/ and a simulated copy at <panel>/sim/.
// The server says which; the page changes its colours and a few buttons, nothing else.
window.SIMULATED = false;
function simMode(on) {
  if (on === window.SIMULATED) return;
  window.SIMULATED = on;
  document.body.classList.toggle('sim', on);
  $('simbanner').hidden = !on;
  $('simlink').hidden = on;
  $('apptitle').textContent = on ? 'Bench Panel · Simulator' : 'Bench Panel';
  $('mgrbtn').textContent = on ? 'Wiring' : 'Instruments';
  document.title = on ? 'Bench Panel — SIMULATOR' : 'Bench Panel';
  if (on) followHash();
}
// sim/#run=<id> (from the real bench's Simulate button, or the editor): open that script's Run form.
async function followHash() {
  const m = /[#&]run=([^&]+)/.exec(location.hash);
  if (!m || !window.SIMULATED) return;
  history.replaceState(null, '', location.pathname + location.search);
  await Scripts.open();
  Scripts.form(decodeURIComponent(m[1]));
}
window.addEventListener('hashchange', followHash);

async function refresh() {
  let s;
  try { s = await api('api/state'); }
  catch (e) {
    // Surface a transport failure instead of a frozen page showing stale numbers.
    $('globalmsg').textContent = `Lost contact with the bench panel: ${e.message || e}`;
    document.querySelectorAll('.dot').forEach(d => d.classList.remove('on'));
    return;
  }
  lastSnap = s;
  simMode(!!s.simulated);
  const o = s.other_script, olive = o && (o.state === 'running' || o.state === 'stopping');
  $('otherpill').hidden = !olive;
  if (olive) $('otherpill').textContent = (s.simulated ? 'real bench: ▶ ' : 'simulator: ▶ ') + o.name;
  $('globalmsg').textContent = '';
  $('clock').textContent = s.time;
  reconcile(s.instruments);
  if (s.log_seq !== lastLog) {
    lastLog = s.log_seq;
    $('logbody').innerHTML = s.log.map(l =>
      `<div class="l"><span class="t">${l.t}</span><span class="a">${esc(l.addr)}</span>`
      + `<span class="${esc(l.kind)}">${esc(l.sent)}${l.got ? ' → ' + esc(l.got) : ''}</span></div>`
    ).join('');
    const lb = $('logbody'); lb.scrollTop = lb.scrollHeight;
  }
  if (Manager.isOpen()) Manager.renderBench();
  Scripts.pill(s.script);
}

async function allOff() {
  const b = $('alloff'); b.classList.add('armed');
  const r = await post('api/action', {action: 'all_outputs_off'});
  setTimeout(() => b.classList.remove('armed'), 600);
  const unsure = (r.result || []).filter(x => x.ok !== true);
  $('globalmsg').textContent = unsure.length
    ? 'Not confirmed off: ' + unsure.map(x => `${x.instrument} (${x.msg || 'no confirmation'})`).join(', ')
    : '';
  refresh();
}

async function togglePoll() {
  polling = !polling;
  $('pollbtn').textContent = polling ? 'Pause polling' : 'Resume polling';
  await post('api/action', {action: 'set_polling', value: polling});
}

// ---------------------------------------------------------------- manager
const CONN_LABEL = {'gpib-gateway': 'GPIB, through a LAN gateway',
                    'vxi11': 'Network (VXI-11 / LXI)', 'visa': 'Other VISA resource'};
const Manager = {
  plugins: null, defaults: {},
  isOpen: () => $('mgr').classList.contains('open'),
  async open() {
    $('mgr').classList.add('open');
    await this.load();
  },
  close() { $('mgr').classList.remove('open'); $('m-inspect').innerHTML = ''; },
  async load() {
    this.plugins = await api('api/plugins').catch(() => null);
    const d = await post('api/manage', {op: 'defaults'});
    this.defaults = d.defaults || {};
    this.renderBench(); this.renderPlugins(); this.fillAdd();
  },

  renderBench() {
    const list = lastSnap ? lastSnap.instruments : [];
    const box = $('m-bench');
    // Redraw only when something shown here changed -- rebuilding it on every
    // refresh swaps the buttons out from under the mouse and eats clicks. And
    // leave it alone while someone is mid-click on an armed Remove.
    const key = JSON.stringify(list.map(i => [i.id, i.name, i.where, i.enabled, i.missing || '']));
    if (box.dataset.key === key || box.querySelector('[data-armed="1"]')) return;
    box.dataset.key = key;
    if (!list.length) { box.innerHTML = '<p class="note">Nothing yet — add one below.</p>'; return; }
    box.innerHTML = '<table><tr><th>Name</th><th>Type</th><th>Where</th><th>State</th><th></th></tr>'
      + list.map((i, n) => `<tr><td>${esc(i.name)}</td><td>${esc(i.plugin)}</td>
        <td>${esc(i.where)}</td>
        <td>${i.missing ? `<span class="bad">${esc(i.missing)}</span>` : (i.enabled ? 'ON' : 'off')}</td>
        <td class="act"><button ${n ? '' : 'disabled'} onclick="Manager.move('${i.id}',-1)" title="Move earlier">↑</button>
          <button ${n < list.length - 1 ? '' : 'disabled'} onclick="Manager.move('${i.id}',1)" title="Move later">↓</button>
          <button onclick="Manager.rename('${i.id}')">Rename</button>
          <button class="danger" onclick="Manager.remove(this,'${i.id}')">Remove</button></td></tr>`).join('')
      + '</table><p class="note">Remove switches its output off, hands its front panel back and '
      + 'takes it off this bench. The plugin stays installed.</p>';
  },
  async move(id, delta) { await post('api/manage', {op: 'move', instrument: id, delta}); refresh(); },
  async rename(id) {
    const cur = (lastSnap.instruments.find(i => i.id === id) || {}).name || '';
    const name = prompt('Name on its card:', cur);
    if (name === null) return;
    const r = await post('api/manage', {op: 'rename', instrument: id, name});
    if (!r.ok) alert(r.msg);
    refresh();
  },
  remove(btn, id) {
    arm(btn, 'CONFIRM REMOVE', async () => {
      const r = await post('api/manage', {op: 'remove', instrument: id});
      $('m-addmsg').textContent = r.ok ? (r.msg || '') : (r.msg || 'refused');
      await refresh(); this.renderBench();
    });
  },

  fillAdd() {
    const sel = $('m-plugin');
    const inst = this.plugins ? this.plugins.installed : [];
    const cur = sel.value;
    sel.innerHTML = inst.length
      ? inst.map(p => `<option value="${esc(p.id)}">${esc(p.model)} — ${esc(p.name)}</option>`).join('')
      : '<option value="">(install a plugin first)</option>';
    if (inst.some(p => p.id === cur)) sel.value = cur;
    sel.onchange = () => this.fillTypes();
    this.fillTypes();
  },
  fillTypes() {
    const p = (this.plugins ? this.plugins.installed : []).find(x => x.id === $('m-plugin').value);
    const t = $('m-type');
    t.innerHTML = p ? p.connections.map(c => `<option value="${c}">${CONN_LABEL[c] || c}</option>`).join('') : '';
    t.onchange = () => this.fillFields();
    this.fillFields();
  },
  fillFields() {
    const t = $('m-type').value;
    const f1 = $('m-f1'), f2 = $('m-f2');
    const show2 = t === 'gpib-gateway';
    $('m-l2').style.display = f2.style.display = show2 ? '' : 'none';
    if (t === 'gpib-gateway') {
      $('m-l1').textContent = 'Gateway host'; f1.placeholder = 'IP or name of the LAN/GPIB gateway';
      if (!f1.value && this.defaults.gateway) f1.value = this.defaults.gateway;
      $('m-l2').textContent = 'GPIB address'; f2.placeholder = '0 – 30';
    } else if (t === 'vxi11') {
      $('m-l1').textContent = 'Host'; f1.placeholder = 'the instrument’s IP address or name';
    } else {
      $('m-l1').textContent = 'VISA resource'; f1.placeholder = 'e.g. USB0::0x0957::0x2C07::MY123::INSTR';
    }
  },
  async add() {
    const t = $('m-type').value, f1 = $('m-f1').value.trim(), f2 = $('m-f2').value.trim();
    const connection = t === 'gpib-gateway' ? {type: t, gateway: f1, address: f2}
      : t === 'vxi11' ? {type: t, host: f1} : {type: t, resource: f1};
    const r = await post('api/manage', {op: 'add', plugin: $('m-plugin').value,
      name: $('m-name').value, connection});
    $('m-addmsg').textContent = r.ok
      ? 'Added, OFF in the panel. Switch it on at the bench, then press its ⏻.' : (r.msg || 'refused');
    if (r.ok) { $('m-name').value = ''; $('m-f2').value = ''; }
    await refresh(); this.renderBench();
  },

  renderPlugins() {
    const P = this.plugins;
    if (!P) { $('m-installed').innerHTML = '<p class="bad">Could not read the plugin list.</p>'; return; }
    const tag = p => p.has_output ? '<span class="tag out">has outputs</span>' : '<span class="tag">no outputs</span>';
    $('m-installed').innerHTML = (P.installed.length
      ? '<table><tr><th>Plugin</th><th>Model</th><th>Version</th><th></th><th></th></tr>'
        + P.installed.map(p => `<tr><td>${esc(p.id)}</td><td>${esc(p.model)}</td><td>${esc(p.version)}</td>
          <td>${tag(p)}</td><td class="act"><button class="danger" onclick="Manager.uninstall(this,'${esc(p.id)}')">Uninstall</button></td></tr>`).join('')
        + '</table>'
      : P.broken.length
        ? '<p class="note">None loaded. The plugins below are installed but were refused — press <b>Update</b> or <b>Reinstall</b> beside each one in the catalog.</p>'
        : '<p class="note">None installed. The dashboard stays empty until you install one below.</p>')
      + P.broken.map(b => `<p class="bad">Not loaded — ${esc(b.folder)}: ${esc(b.error)}</p>`).join('');
    $('m-catalog').innerHTML = P.catalog.length
      ? '<table><tr><th>Model</th><th>Name</th><th>Version</th><th></th><th></th></tr>'
        + P.catalog.map(c => {
          // A refused plugin is still installed: offer Update (or Reinstall at the
          // same version), never a bare Install that reads as "not here".
          const state = c.installed_version == null ? 'Install'
            : c.installed_version !== c.version ? `Update ${esc(c.installed_version)} → ${esc(c.version)}`
            : c.not_loaded ? 'Reinstall' : null;
          return `<tr><td>${esc(c.model)}</td><td>${esc(c.name)}</td><td>${esc(c.version)}</td><td>${tag(c)}</td>
            <td class="act">${state ? `<button onclick="Manager.inspectCatalog('${esc(c.id)}')">${state}</button>` : 'installed'}</td></tr>`;
        }).join('') + '</table>'
      : '<p class="note">The catalog folder is empty.</p>';
  },
  uninstall(btn, id) {
    arm(btn, 'CONFIRM UNINSTALL', async () => {
      const r = await post('api/manage', {op: 'uninstall', plugin: id});
      if (!r.ok) alert(r.msg);
      await this.load(); refresh();
    });
  },

  async inspectCatalog(id) {
    const r = await post('api/manage', {op: 'inspect_catalog', plugin: id});
    this.showInspect(r, {op: 'install_catalog', plugin: id});
  },
  inspectZip() {
    const f = $('m-zip').files[0];
    if (!f) { $('m-inspect').innerHTML = '<p class="bad">Choose a .zip first.</p>'; return; }
    const rd = new FileReader();
    rd.onload = async () => {
      const data = String(rd.result).split(',', 2)[1] || '';
      const r = await post('api/manage', {op: 'inspect_zip', data});
      this.showInspect(r, r.ok ? {op: 'install_zip', sha256: r.inspect.sha256} : null);
    };
    rd.readAsDataURL(f);
  },
  // What is about to be installed, shown BEFORE anything is written.
  showInspect(r, confirmReq) {
    const box = $('m-inspect');
    if (!r.ok) { box.innerHTML = `<p class="bad">${esc(r.msg || 'refused')}</p>`; return; }
    const i = r.inspect, m = i.manifest;
    const tested = Object.entries(m.tested_on || {}).map(([k, v]) => `${k}: ${v}`).join('; ') || 'not stated';
    // The plugin standard, file checks only (no plugin code has run yet). The
    // full check -- which runs the driver against a pretend connection -- happens
    // on install, and can still refuse it.
    const std = i.standard || {ok: true, items: []};
    const fails = std.items.filter(x => !x.ok && x.level === 'MUST');
    const warns = std.items.filter(x => !x.ok && x.level === 'SHOULD');
    const stdHtml = std.ok
      ? `<b>Meets the plugin standard</b> (file checks${warns.length ? `; ${warns.length} warning(s)` : ''}). `
        + 'The safety checks run on install.'
      : '<b class="bad">Does NOT meet the plugin standard — it cannot be installed:</b><ul>'
        + fails.map(f => `<li>${esc(f.rule)}: ${esc(f.detail)}</li>`).join('') + '</ul>';
    box.innerHTML = `<div class="inspect">
      <dl><dt>Plugin</dt><dd><b>${esc(m.name)}</b> (${esc(m.id)})</dd>
      <dt>Version</dt><dd>${esc(m.version)}${i.replaces ? ` — replaces installed ${esc(i.replaces)}` : ''}</dd>
      <dt>Vendor / model</dt><dd>${esc(m.vendor || '?')} / ${esc(m.model)}</dd>
      <dt>Outputs</dt><dd>${m.has_output ? '<b>YES — it can source power or signal</b>' : 'none'}</dd>
      <dt>Connections</dt><dd>${esc(m.connections.join(', '))}</dd>
      <dt>Tested on</dt><dd>${esc(tested)}</dd>
      <dt>Author / licence</dt><dd>${esc(m.author || '?')} / ${esc(m.license || '?')}</dd>
      <dt>From</dt><dd>${i.source === 'catalog' ? 'this program’s catalog' : 'the .zip you chose'}</dd>
      <dt>SHA-256</dt><dd><code>${esc(i.sha256)}</code></dd>
      <dt>Files</dt><dd>${esc(i.files.join(', '))}</dd>
      <dt>Capabilities</dt><dd>${esc((m.capabilities || []).join(', ') || 'none declared')}</dd>
      <dt>Standard</dt><dd>${stdHtml}</dd></dl>
      ${(m.quirks || []).length ? '<b>Known quirks</b><ul>' + m.quirks.map(q => `<li>${esc(q.note)}</li>`).join('') + '</ul>' : ''}
      <p class="note">Installing runs this plugin's Python on this computer.</p>
      <div class="row"><button class="go" id="m-confirm" ${std.ok ? '' : 'disabled'}>Install it</button>
        <button onclick="$('m-inspect').innerHTML=''">Cancel</button></div></div>`;
    $('m-confirm').onclick = async () => {
      const res = await post('api/manage', confirmReq);
      box.innerHTML = res.ok
        ? `<p>Installed ${esc(res.id)} ${esc(res.version)}.${res.backup ? ` The previous version was kept as ${esc(res.backup)}.` : ''}</p>`
        : `<p class="bad">${esc(res.msg || 'refused')}</p>`;
      await this.load(); refresh();
    };
  },
};

// ---------------------------------------------------------------- scripts
// Import, list and run automation scripts. A script is a .py file with a
// BENCHPANEL_SCRIPT header (see SCRIPTS.md); the server reads that header without
// running anything, and builds the Run form from its "args".
const Scripts = {
  data: null, logSeq: 0, runKey: null, runStarted: undefined, timer: null, formFor: null, busy: false,
  isOpen: () => $('scr').classList.contains('open'),
  async open() {
    $('scr').classList.add('open');
    this.logSeq = 0; $('s-log').textContent = ''; this.runKey = null; this.runStarted = undefined;
    await this.load();
    clearInterval(this.timer);
    this.timer = setInterval(() => this.poll(), 1000);
    this.poll();
  },
  close() {
    $('scr').classList.remove('open'); clearInterval(this.timer);
    $('s-inspect').innerHTML = '';
  },
  pill(run) {
    const p = $('scrpill');
    const live = run && (run.state === 'running' || run.state === 'stopping');
    p.hidden = !live;
    if (live) p.textContent = (run.state === 'stopping' ? '■ stopping ' : '▶ ') + run.name;
  },
  async load() {
    this.loadResults();
    const r = await post('api/scripts', {op: 'list'});
    if (!r.ok) { $('s-installed').innerHTML = `<p class="bad">${esc(r.msg || 'Could not read the scripts.')}</p>`; return; }
    this.data = r;
    this.renderInstalled(); this.renderCatalog();
  },

  // -- running now ------------------------------------------------------------
  // One status request at a time: two in flight (the 1 s timer plus the one after
  // Stop) both fetched the same new lines and the log showed them twice.
  async poll() {
    if (this.busy) return;
    this.busy = true;
    try { await this.pollOnce(); } finally { this.busy = false; }
  },
  async pollOnce() {
    let r = await post('api/scripts', {op: 'status', since: this.logSeq});
    if (!r.ok) return;
    const run = r.run;
    // A different run from the one on screen: start its log from the top.
    const started = run ? run.started : null;
    if (started !== this.runStarted) {
      this.runStarted = started; this.logSeq = 0; $('s-log').textContent = '';
      r = await post('api/scripts', {op: 'status', since: 0});
      if (!r.ok) return;
    }
    const key = run ? JSON.stringify([run.id, run.state, run.started, run.note]) : '';
    if (key !== this.runKey) {
      const ended = this.runKey && run && run.state !== 'running' && run.state !== 'stopping';
      this.runKey = key;
      this.renderRun(r.run);
      if (ended) this.loadResults();
    }
    // The files this run has written so far (its .log, CSVs ...), each a download.
    const fkey = JSON.stringify((r.files || []).map(f => [f.name, f.bytes]));
    if (fkey !== this.filesKey && $('s-runfiles')) {       // absent until something has run
      this.filesKey = fkey;
      $('s-runfiles').innerHTML = (r.files || []).length
        ? '<b>Files from this run:</b> ' + r.files.map(f => this.link(f)).join(' · ') : '';
    }
    const log = $('s-log');
    if (r.from < this.logSeq) r.log = r.log.slice(this.logSeq - r.from);     // belt and braces
    if (r.truncated) log.insertAdjacentHTML('beforeend', '<span class="sys">… earlier lines dropped …</span>\n');
    if (r.log.length) {
      const stick = log.scrollTop + log.clientHeight >= log.scrollHeight - 20;
      log.insertAdjacentHTML('beforeend', r.log.map(l =>
        /^[#$] /.test(l) ? `<span class="sys">${esc(l)}</span>` : esc(l)).join('\n') + '\n');
      if (stick) log.scrollTop = log.scrollHeight;
    }
    this.logSeq = r.seq;
    log.hidden = !run;
  },
  renderRun(run) {
    const box = $('s-run');
    if (!run) { box.innerHTML = '<p class="note">Nothing has run since the panel started.</p>'; return; }
    const live = run.state === 'running' || run.state === 'stopping';
    const when = new Date(run.started * 1000).toLocaleTimeString();
    const label = {running: 'RUNNING', stopping: 'STOPPING', finished: 'finished', stopped: 'stopped',
                   failed: 'FAILED'}[run.state] || run.state;
    box.innerHTML = `<div class="row">${this.data && this.data.simulated ? '<span class="tag sim">SIMULATED</span>' : ''}<b>${esc(run.name)}</b>
        <span class="tag ${run.state === 'failed' ? 'out' : ''}">${label}${run.exit != null ? ` · exit ${run.exit}` : ''}</span>
        <span class="note" style="margin:0">started ${esc(when)}${run.ended ? ` · took ${(run.ended - run.started).toFixed(1)} s` : ''} ·
          outputs ${run.allow_outputs ? '<b>allowed</b>' : 'refused'}</span>
        <span class="grow"></span>
        ${live ? `<button class="danger" ${run.state === 'stopping' ? 'disabled' : ''} onclick="Scripts.stop(this)">Stop</button>` : ''}</div>
      ${run.args.length ? `<p class="note">settings: <code>${esc(run.args.join(' '))}</code></p>` : ''}
      ${run.note ? `<p class="${/!!|NOT/.test(run.note) ? 'bad' : 'note'}">${esc(run.note)}</p>` : ''}
      <p class="note" id="s-runfiles"></p>`;
    this.filesKey = null;
    // The list's Run buttons depend on whether something is running.
    if (this.data && this.data.running !== (live ? run.id : null)) {
      this.data.running = live ? run.id : null;
      this.renderInstalled();
    }
  },
  async stop(btn) {
    btn.disabled = true;
    const r = await post('api/scripts', {op: 'stop'});
    if (!r.ok) { alert(r.msg); btn.disabled = false; }
    this.poll();
  },

  // -- the list ----------------------------------------------------------------
  renderInstalled() {
    const D = this.data, box = $('s-installed');
    if (box.querySelector('[data-armed="1"]')) return;
    const have = new Set(D.bench_plugins);
    const busy = new Set(D.running_any || []);
    if (!D.installed.length) {
      box.innerHTML = '<p class="note">No scripts yet — import one below.</p>'; return;
    }
    box.innerHTML = '<table class="stable"><tr><th>Script</th><th>Version</th><th>Needs</th><th></th><th></th></tr>'
      + D.installed.map(x => {
        const miss = x.uses.filter(u => !have.has(u));
        const needs = x.uses.length
          ? x.uses.map(u => have.has(u) ? esc(u) : `<span class="bad" title="not on this bench">${esc(u)}</span>`).join(', ')
          : '—';
        return `<tr><td><b>${esc(x.name)}</b><div class="desc">${esc(x.description)}</div>
            ${x.ok ? '' : `<div class="bad">${esc(x.problems.join('; '))}</div>`}</td>
          <td>${esc(x.version || '?')}</td><td>${needs}</td>
          <td>${x.outputs ? '<span class="tag out">can switch outputs on</span>' : '<span class="tag">no outputs</span>'}</td>
          <td class="act"><button class="go" ${x.ok && !miss.length && !D.running ? '' : 'disabled'}
              title="${miss.length ? 'add ' + esc(miss.join(', ')) + ' in Instruments first' : D.running ? 'a script is running' : ''}"
              onclick="Scripts.form('${esc(x.id)}')">${D.simulated ? 'Run (simulated)…' : 'Run…'}</button>
            ${D.simulated ? '' : `<button ${x.ok ? '' : 'disabled'} title="Run it on the simulated bench, in another tab"
              onclick="window.open('sim/#run=${encodeURIComponent(x.id)}','benchsim')">Simulate</button>`}
            <button ${busy.has(x.id) ? 'disabled title="it is running"' : ''} onclick="Editor.open('${esc(x.id)}')">Edit</button>
            <button onclick="Scripts.source('${esc(x.id)}')">Source</button>
            <button class="danger" ${busy.has(x.id) ? 'disabled' : ''} onclick="Scripts.remove(this,'${esc(x.id)}')">Remove</button></td></tr>`;
      }).join('') + '</table>'
      + `<p class="note">Imported scripts live in <code>${esc(D.folder)}</code>. Remove keeps a dated copy there.</p>`;
  },
  remove(btn, id) {
    arm(btn, 'CONFIRM REMOVE', async () => {
      const r = await post('api/scripts', {op: 'remove', script: id});
      if (!r.ok) alert(r.msg);
      if (this.formFor === id) { $('s-form').innerHTML = ''; this.formFor = null; }
      this.load();
    });
  },
  async source(id, catalog) {
    const r = await post('api/scripts', {op: 'source', script: id, catalog: !!catalog});
    this.formFor = null;
    $('s-form').innerHTML = r.ok
      ? `<div class="runform"><div class="row"><b>${esc(id)}.py</b><span class="grow"></span>
           <button onclick="$('s-form').innerHTML=''">Close</button></div>
         <pre class="srcview">${esc(r.text)}</pre></div>`
      : `<p class="bad">${esc(r.msg)}</p>`;
  },

  // -- the Run form, built from the header's "args" ----------------------------
  saved(id) { try { return JSON.parse(localStorage.getItem('bp-script-' + id) || '{}'); } catch { return {}; } },
  save(id, v) { try { localStorage.setItem('bp-script-' + id, JSON.stringify(v)); } catch { /* private window */ } },
  form(id) {
    const x = this.data.installed.find(s => s.id === id);
    if (!x) { $('s-form').innerHTML = `<p class="bad">No script ${esc(id)} here.</p>`; return; }
    this.formFor = id;
    const last = this.saved(id);
    const field = a => {
      const v = a.name in last ? last[a.name] : (a.default ?? '');
      const fid = 's-a-' + a.name, lbl = esc(a.label || a.name);
      let input;
      if (a.type === 'bool') input = `<label><input type="checkbox" id="${fid}" ${v === true ? 'checked' : ''}> yes</label>`;
      else if (a.type === 'choice') input = `<select id="${fid}">${a.choices.map(c =>
        `<option ${String(v) === c ? 'selected' : ''}>${esc(c)}</option>`).join('')}</select>`;
      else input = `<input type="text" id="${fid}" value="${esc(v)}" inputmode="${a.type === 'str' ? 'text' : 'decimal'}"
        placeholder="${a.type}${a.min != null ? ' ≥ ' + a.min : ''}${a.max != null ? ' ≤ ' + a.max : ''}">`;
      return `<label for="${fid}">${lbl}</label><div>${input}</div>`
        + (a.help ? `<div class="help">${esc(a.help)}</div>` : '');
    };
    $('s-form').innerHTML = `<div class="runform">
      <div class="row"><b>Run ${esc(x.name)}</b><span class="grow"></span>
        <button onclick="$('s-form').innerHTML='';Scripts.formFor=null">Cancel</button></div>
      <div class="form" style="margin-top:10px">${x.args.map(field).join('') || '<span class="note">No settings.</span>'}</div>
      ${x.outputs ? `<div class="warnbox"><label><input type="checkbox" id="s-allow" ${this.data.simulated ? 'checked' : ''}>
          <b>Allow this run to switch outputs on${this.data.simulated ? ' (simulated outputs)' : ''}.</b></label>
          Unticked, the panel refuses every output-on command from it — safe for a dry run.</div>` : ''}
      ${this.data.simulated && (x.wiring || []).length ? `<div class="note"><label><input type="checkbox" id="s-wire" checked>
          Wire the simulated bench the way the script says:</label> <code>${esc(x.wiring.map(w => w.join(' → ')).join(', '))}</code></div>` : ''}
      <div class="row" style="margin-top:10px"><button class="go" id="s-go">Run it</button></div>
      <p class="bad" id="s-formmsg"></p></div>`;
    $('s-go').onclick = () => {
      const allow = x.outputs && $('s-allow').checked;
      const go = () => this.run(x);
      // Outputs allowed: the same two-click arm as a card's output button (not on
      // the simulated bench -- there is nothing there to protect).
      if (allow && !this.data.simulated) arm($('s-go'), 'CONFIRM: OUTPUTS MAY GO ON', go); else go();
    };
  },
  async run(x) {
    const args = {}, keep = {};
    for (const a of x.args) {
      const el = $('s-a-' + a.name);
      if (a.type === 'bool') { args[a.name] = keep[a.name] = el.checked; continue; }
      const v = el.value.trim();
      keep[a.name] = v;
      if (v !== '') args[a.name] = v;
    }
    const allow = !!(x.outputs && $('s-allow').checked);
    const wire = !!($('s-wire') && $('s-wire').checked);
    const r = await post('api/scripts', {op: 'run', script: x.id, args, allow_outputs: allow, wire});
    if (!r.ok) { $('s-formmsg').textContent = r.msg; return; }
    this.save(x.id, keep);
    $('s-form').innerHTML = ''; this.formFor = null;
    this.poll();
    $('s-runsec').scrollIntoView({behavior: 'smooth'});
  },

  // -- results: download what scripts wrote -------------------------------------
  // A plain link with the download attribute: the browser saves it, and behind the
  // portal's login it carries the same login as the page. Relative, like every URL here.
  link(f) {
    const href = 'api/results/' + f.name.split('/').map(encodeURIComponent).join('/');
    return `<a href="${esc(href)}" download="${esc(f.name.split('/').pop())}">${esc(f.name)}</a>`
      + ` <span class="note">(${this.size(f.bytes)})</span>`;
  },
  size(n) {
    return n < 1024 ? n + ' B' : n < 1048576 ? (n / 1024).toFixed(1) + ' KB' : (n / 1048576).toFixed(1) + ' MB';
  },
  showAll: false, onlyData: true,
  async loadResults() {
    const r = await post('api/scripts', {op: 'results'});
    this.results = r.ok ? r : null;
    this.renderResults();
  },
  renderResults() {
    const box = $('s-results'), R = this.results;
    if (!R) { box.innerHTML = '<p class="bad">Could not list the results folder.</p>'; return; }
    const all = R.files.filter(f => !this.onlyData || !f.name.endsWith('.log'));
    const shown = this.showAll ? all : all.slice(0, 15);
    const when = t => new Date(t * 1000).toLocaleString([], {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'});
    box.innerHTML = `<div class="row" style="margin-bottom:6px">
        <label><input type="checkbox" id="s-onlydata" ${this.onlyData ? 'checked' : ''}> hide run logs (.log)</label>
        <span class="grow"></span><button onclick="Scripts.loadResults()">Refresh</button></div>`
      + (shown.length
        ? '<table class="stable"><tr><th>File</th><th>Size</th><th>Written</th><th></th></tr>'
          + shown.map(f => `<tr><td><code>${esc(f.name)}</code></td><td>${this.size(f.bytes)}</td><td>${when(f.mtime)}</td>
            <td class="act"><a class="btn" href="${esc('api/results/' + f.name.split('/').map(encodeURIComponent).join('/'))}"
              download="${esc(f.name.split('/').pop())}">Download</a></td></tr>`).join('') + '</table>'
          + (all.length > shown.length ? `<p class="note"><a href="#" onclick="Scripts.showAll=true;Scripts.renderResults();return false">Show all ${all.length}</a></p>` : '')
        : `<p class="note">${R.files.length ? 'Only run logs so far — untick the box to see them.' : 'Nothing yet. A script’s files and each run’s log appear here.'}</p>`)
      + `<p class="note">Folder: <code>${esc(R.folder)}</code></p>`;
    $('s-onlydata').onchange = e => { this.onlyData = e.target.checked; this.renderResults(); };
  },

  // -- importing ---------------------------------------------------------------
  renderCatalog() {
    const C = this.data.catalog;
    $('s-catalog').innerHTML = C.length
      ? '<table class="stable"><tr><th>Script</th><th>Version</th><th></th><th></th></tr>' + C.map(c => {
        const act = c.installed_same ? 'imported'
          : `<button onclick="Scripts.inspectCatalog('${esc(c.id)}')">${c.installed_version == null ? 'Import'
              : `Update ${esc(c.installed_version)} → ${esc(c.version)}`}</button>`;
        return `<tr><td><b>${esc(c.name)}</b><div class="desc">${esc(c.description)}</div></td>
          <td>${esc(c.version || '?')}</td>
          <td>${c.outputs ? '<span class="tag out">can switch outputs on</span>' : '<span class="tag">no outputs</span>'}</td>
          <td class="act">${act}</td></tr>`;
      }).join('') + '</table>'
      : '<p class="note">No scripts ship with this copy.</p>';
  },
  async inspectCatalog(id) {
    const r = await post('api/scripts', {op: 'inspect_catalog', script: id});
    this.showInspect(r, {op: 'install_catalog', script: id});
  },
  inspectUpload() {
    const f = $('s-file').files[0];
    if (!f) { $('s-inspect').innerHTML = '<p class="bad">Choose a .py file first.</p>'; return; }
    const rd = new FileReader();
    rd.onload = async () => {
      const data = String(rd.result).split(',', 2)[1] || '';
      const r = await post('api/scripts', {op: 'inspect_upload', filename: f.name, data});
      this.showInspect(r, r.ok ? {op: 'install_upload', sha256: r.inspect.sha256} : null);
    };
    rd.readAsDataURL(f);
  },
  // Everything about the script, INCLUDING its full source, before anything is written.
  showInspect(r, confirmReq) {
    const box = $('s-inspect');
    if (!r.ok) { box.innerHTML = `<p class="bad">${esc(r.msg || 'refused')}</p>`; return; }
    const i = r.inspect, h = i.header || {};
    const args = (h.args || []).map(a => `${a.label || a.name} (${a.type}${a.default != null ? ', default ' + a.default : ''})`);
    box.innerHTML = `<div class="inspect">
      <dl><dt>Script</dt><dd><b>${esc(h.name || i.id)}</b> (${esc(i.id)}.py)</dd>
      <dt>Version</dt><dd>${esc(h.version || '?')}${i.replaces ? ` — replaces imported ${esc(i.replaces)}` : ''}</dd>
      <dt>What it does</dt><dd>${esc(h.description || '')}</dd>
      <dt>Needs</dt><dd>${esc((h.uses || []).join(', ') || 'nothing declared')}</dd>
      <dt>Outputs</dt><dd>${h.outputs ? '<b>YES — it can switch outputs on</b> (only on runs where you allow it)' : 'never'}</dd>
      <dt>Settings</dt><dd>${esc(args.join('; ') || 'none')}</dd>
      <dt>From</dt><dd>${i.source === 'catalog' ? 'this program’s catalog' : 'the file you chose'}</dd>
      <dt>Size</dt><dd>${i.lines} lines, ${i.bytes} bytes</dd>
      <dt>SHA-256</dt><dd><code>${esc(i.sha256)}</code></dd>
      <dt>Checks</dt><dd>${i.ok ? '<b>Pass</b>' : '<b class="bad">FAIL — it cannot be imported:</b><ul>'
        + i.problems.map(p => `<li>${esc(p)}</li>`).join('') + '</ul>'}</dd></dl>
      ${i.watch.length ? '<div class="warnbox"><b>Read these parts carefully — this script:</b><ul>'
        + i.watch.map(w => `<li>${esc(w)}</li>`).join('') + '</ul></div>' : ''}
      <details><summary>The whole script (${i.lines} lines) — read it before importing</summary>
        <pre class="srcview">${esc(i.text)}</pre></details>
      <p class="note">Importing copies it into this bench’s scripts folder. Running it runs this Python on this computer.</p>
      <div class="row"><button class="go" id="s-confirm" ${i.ok ? '' : 'disabled'}>Import it</button>
        <button onclick="$('s-inspect').innerHTML=''">Cancel</button></div></div>`;
    $('s-confirm').onclick = async () => {
      const res = await post('api/scripts', confirmReq);
      box.innerHTML = res.ok
        ? `<p>Imported ${esc(res.id)} ${esc(res.version)}.${res.backup ? ` The previous copy was kept as ${esc(res.backup)}.` : ''}</p>`
        : `<p class="bad">${esc(res.msg || 'refused')}</p>`;
      this.load();
    };
  },
};

// ---------------------------------------------------------------- simulator wiring
const SimWiring = {
  data: null,
  isOpen: () => $('simw').classList.contains('open'),
  async open() { $('simw').classList.add('open'); await this.load(); },
  close() { $('simw').classList.remove('open'); },
  async load() {
    const r = await post('api/sim', {op: 'bench'});
    if (!r.ok) { $('w-msg').textContent = r.msg; return; }
    this.render(r);
  },
  async op(body) {
    const r = await post('api/sim', body);
    $('w-msg').textContent = r.ok ? '' : r.msg;
    if (r.ok) this.render(r);
    refresh();
  },
  render(d) {
    this.data = d;
    const name = {};
    d.instruments.forEach(i => { name[i.id] = i.name; });
    const port = e => { const [i, p] = e.split(':'); return `<b>${esc(name[i] || i)}</b> <code>${esc(p)}</code>`; };
    $('w-wires').innerHTML = d.wires.length
      ? '<table class="stable">' + d.wires.map(([a, b]) => `<tr><td>${port(a)} &rarr; ${port(b)}</td>
          <td class="act"><button onclick="SimWiring.op({op:'unwire',end:'${esc(a)}'})">Disconnect</button></td></tr>`).join('') + '</table>'
      : '<p class="note">Nothing is wired: every input reads nothing.</p>';
    const opts = kind => d.instruments.flatMap(i => Object.entries(i[kind]).map(([p, lbl]) =>
      `<option value="${esc(i.id + ':' + p)}">${esc(i.name)} · ${esc(p)} — ${esc(lbl)}</option>`)).join('');
    $('w-out').innerHTML = opts('outputs');
    $('w-in').innerHTML = opts('inputs');
    $('w-insts').innerHTML = '<table class="stable"><tr><th>Instrument</th><th>Starting state</th><th></th></tr>'
      + d.instruments.map(i => `<tr><td><b>${esc(i.name)}</b> <code>${esc(i.id)}</code>
          ${i.on ? '<span class="tag">ON in the panel</span>' : ''}
          ${i.generic ? `<div class="bad">${esc(i.note || 'generic model')}</div>` : ''}</td>
        <td><select data-pre="${esc(i.id)}">${Object.entries(i.presets).map(([k, v]) =>
            `<option value="${esc(k)}" ${k === i.preset ? 'selected' : ''} title="${esc(v)}">${esc(k)} — ${esc(v)}</option>`).join('')}</select></td>
        <td class="act"><button onclick="SimWiring.op({op:'power_cycle',instrument:'${esc(i.id)}'})">Power-cycle</button>
          <label><input type="checkbox" ${i.offline ? 'checked' : ''} data-off="${esc(i.id)}"> not answering</label></td></tr>`).join('')
      + '</table>';
    $('w-insts').querySelectorAll('select[data-pre]').forEach(s =>
      s.onchange = () => this.op({op: 'preset', instrument: s.dataset.pre, preset: s.value}));
    $('w-insts').querySelectorAll('input[data-off]').forEach(c =>
      c.onchange = () => this.op({op: 'offline', instrument: c.dataset.off, value: c.checked}));
  },
  connect() { this.op({op: 'wire', a: $('w-out').value, b: $('w-in').value}); },
};

refresh();
setInterval(refresh, 400);
