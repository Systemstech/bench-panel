// Bench panel -- the script editor. Loaded after app.js; uses its $, esc, post, arm.
//
// A plain <textarea> over a highlighted <pre>: no editor library, nothing fetched
// from the internet, so it works on an offline bench PC. What it knows about each
// instrument comes from that plugin's manifest (its scripting dictionary): the
// reference pane, completion and the lint all read the same source.
'use strict';

const PY_KW = 'def|class|return|if|elif|else|for|while|try|except|finally|with|as|import|from|raise|pass|'
  + 'break|continue|in|not|and|or|is|lambda|yield|global|nonlocal|assert|del|True|False|None';
const PY_RE = new RegExp(
  '(#[^\\n]*)'                                                    // 1 comment
  + '|("""[\\s\\S]*?(?:"""|$)|\'\'\'[\\s\\S]*?(?:\'\'\'|$))'      // 2 triple string
  + '|([rbfuRBFU]{0,2}"(?:[^"\\\\\\n]|\\\\.)*"?|[rbfuRBFU]{0,2}\'(?:[^\'\\\\\\n]|\\\\.)*\'?)'  // 3 string
  + `|\\b(${PY_KW})\\b`                                            // 4 keyword
  + '|\\b(\\d+\\.?\\d*(?:[eE][-+]?\\d+)?)\\b'                      // 5 number
  + '|\\b(BENCHPANEL_SCRIPT|bench|Bench|BenchError)\\b', 'g');      // 6 panel names
const PY_CLS = [null, 'c', 's', 's', 'k', 'n', 'b'];

function highlight(text) {
  let out = '', last = 0;
  text.replace(PY_RE, (m, ...g) => {
    const at = g[g.length - 2];
    out += esc(text.slice(last, at));
    const i = g.slice(0, 6).findIndex(x => x !== undefined) + 1;
    out += `<span class="${PY_CLS[i]}">${esc(m)}</span>`;
    last = at + m.length;
    return m;
  });
  return out + esc(text.slice(last)) + '\n ';
}

const Editor = {
  id: null, sha: null, dirty: false, check: null, ref: {}, uses: [], timer: null, pop: null,

  isOpen: () => $('ed').classList.contains('open'),

  // -- open / new / close -------------------------------------------------------
  async open(id) {
    const r = await post('api/scripts', {op: 'source', script: id});
    if (!r.ok) { alert(r.msg); return; }
    this.show(id, r.text, r.sha256);
  },
  async newScript() {
    this.showFrame();
    $('ed-new').hidden = false;
    $('ed-main').hidden = true;
    $('ed-title').textContent = 'New script';
    const ref = await post('api/scripts', {op: 'reference', uses: []});
    this.ref = ref.ok ? ref.plugins : {};
    $('ed-new-uses').innerHTML = Object.entries(this.ref).map(([pid, p]) =>
      `<label class="chk"><input type="checkbox" value="${esc(pid)}"> ${esc(p.model)}
        <span class="note">${esc(pid)}${p.has_output ? ' · has an output' : ''}</span></label>`).join('')
      || '<span class="note">No plugins installed.</span>';
    $('ed-new-id').value = ''; $('ed-new-name').value = ''; $('ed-new-msg').textContent = '';
    $('ed-new-id').focus();
  },
  async create() {
    const id = $('ed-new-id').value.trim().toLowerCase().replace(/\.py$/, '').replace(/\s+/g, '_');
    if (!/^[a-z0-9][a-z0-9_-]{1,59}$/.test(id)) {
      $('ed-new-msg').textContent = 'File name: 2-60 of a-z 0-9 _ - (it becomes <name>.py).'; return;
    }
    if ((Scripts.data?.installed || []).some(s => s.id === id)) {
      $('ed-new-msg').textContent = `${id}.py already exists — open it with Edit instead.`; return;
    }
    const uses = [...$('ed-new-uses').querySelectorAll('input:checked')].map(i => i.value);
    const r = await post('api/scripts', {op: 'template', name: $('ed-new-name').value || id,
                                         uses, outputs: $('ed-new-out').checked});
    if (!r.ok) { $('ed-new-msg').textContent = r.msg; return; }
    this.show(id, r.text, null);
    this.dirty = true; this.paintState();
  },
  showFrame() {
    $('ed').classList.add('open');
    document.body.style.overflow = 'hidden';
  },
  show(id, text, sha) {
    this.showFrame();
    $('ed-new').hidden = true; $('ed-main').hidden = false;
    this.id = id; this.sha = sha; this.dirty = false; this.check = null;
    $('ed-title').textContent = id + '.py';
    const ta = $('ed-ta');
    ta.value = text;
    ta.scrollTop = ta.scrollLeft = 0;
    this.paint(); this.paintState();
    this.runCheck();
    ta.focus(); ta.setSelectionRange(0, 0);
  },
  close() {
    if (this.dirty && !confirm('Close without saving? Your changes will be lost.')) return;
    $('ed').classList.remove('open');
    document.body.style.overflow = '';
    this.hidePop();
    this.dirty = false;
  },

  // -- painting ---------------------------------------------------------------------
  paint() {
    const ta = $('ed-ta');
    $('ed-hl').innerHTML = highlight(ta.value);
    const n = ta.value.split('\n').length;
    const marks = {};
    for (const f of (this.check?.lint || [])) marks[f.line] = (marks[f.line] || []).concat(f.msg);
    for (const p of (this.check?.problems || [])) {
      const m = /line (\d+)/.exec(p);
      if (m) marks[+m[1]] = (marks[+m[1]] || []).concat(p);
    }
    let g = '';
    for (let i = 1; i <= n; i++)
      g += marks[i] ? `<div class="bad" title="${esc(marks[i].join('\n'))}">${i}</div>` : `<div>${i}</div>`;
    $('ed-gutter').innerHTML = g;
    this.sync();
  },
  sync() {
    const ta = $('ed-ta');
    $('ed-hl').style.transform = `translate(${-ta.scrollLeft}px,${-ta.scrollTop}px)`;
    $('ed-gutter').style.transform = `translateY(${-ta.scrollTop}px)`;
  },
  paintState() {
    const s = $('ed-state');
    s.textContent = this.dirty ? 'unsaved' : (this.sha ? 'saved' : 'new');
    s.className = 'pill ' + (this.dirty ? 'cc' : 'on');
  },

  // -- checking -------------------------------------------------------------------------
  scheduleCheck() {
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.runCheck(), 700);
  },
  async runCheck() {
    const text = $('ed-ta').value;
    const r = await post('api/scripts', {op: 'check', text});
    if (!r.ok || $('ed-ta').value !== text) return;       // typed on meanwhile: the next one wins
    this.check = r;
    const uses = (r.header && r.header.uses) || [];
    if (JSON.stringify(uses) !== JSON.stringify(this.uses) || !Object.keys(this.ref).length) {
      this.uses = uses;
      const ref = await post('api/scripts', {op: 'reference', uses});
      this.ref = ref.ok ? ref.plugins : {};
      this.renderRef();
    }
    this.renderChecks();
    this.paint();
  },
  renderChecks() {
    const c = this.check, box = $('ed-checks');
    if (!c) { box.innerHTML = ''; return; }
    const item = (cls, line, msg) =>
      `<li class="${cls}" ${line ? `data-line="${line}"` : ''}>${line ? `<b>line ${line}</b> ` : ''}${esc(msg)}</li>`;
    const h = c.header || {};
    box.innerHTML =
      (c.ok ? `<p class="okline">✓ Passes the script checks — it can be run.</p>`
            : `<p class="bad"><b>Can't be run yet:</b></p><ul class="finds">${c.problems.map(p =>
                item('bad', (/line (\d+)/.exec(p) || [])[1], p)).join('')}</ul>`)
      + (c.lint.length ? `<p class="warnline"><b>${c.lint.length} thing${c.lint.length > 1 ? 's' : ''} the instruments won't understand:</b></p>
          <ul class="finds">${c.lint.map(f => item('warn', f.line, f.msg)).join('')}</ul>` : (c.ok ? '<p class="note">No unknown actions or state fields.</p>' : ''))
      + (c.watch.length ? `<p class="warnline"><b>Read twice — this script:</b></p><ul class="finds">${c.watch.map(w => item('warn', 0, w)).join('')}</ul>` : '')
      + (h.name ? `<p class="note">${esc(h.name)} ${esc(h.version || '')} · uses ${esc((h.uses || []).join(', ') || 'nothing')}
          · ${h.outputs ? 'can switch outputs on' : 'no outputs'}${(h.wiring || []).length ? ' · wiring ' + esc(h.wiring.map(w => w.join(' → ')).join(', ')) : ''}</p>` : '');
    box.querySelectorAll('li[data-line]').forEach(li => li.onclick = () => this.gotoLine(+li.dataset.line));
  },
  gotoLine(n) {
    const ta = $('ed-ta'), lines = ta.value.split('\n');
    const at = lines.slice(0, n - 1).reduce((a, l) => a + l.length + 1, 0);
    ta.focus(); ta.setSelectionRange(at, at + (lines[n - 1] || '').length);
    const lh = parseFloat(getComputedStyle(ta).lineHeight) || 18;
    ta.scrollTop = Math.max(0, (n - 5) * lh);
    this.sync();
  },

  // -- the reference pane: the plugins' own dictionaries ------------------------------------
  renderRef() {
    const box = $('ed-ref');
    const plugs = Object.entries(this.ref);
    const argTxt = a => `${a.type}${a.unit ? ' ' + a.unit : ''}${a.choices ? ' ∈ ' + a.choices.join('|') : ''}`
      + `${a.min != null ? ' ≥' + a.min : ''}${a.max != null ? ' ≤' + a.max : ''}`;
    const stateRows = (st, pre = '') => Object.entries(st).map(([k, f]) =>
      `<tr><td><code>${esc(pre + k)}</code></td><td class="note">${esc(f.type)}${f.unit ? ' · ' + esc(f.unit) : ''}${f.nullable ? ' · or null' : ''}</td><td>${esc(f.doc)}</td></tr>`
      + (f.fields ? stateRows(f.fields, pre + k + '.') : '')).join('');
    box.innerHTML = (plugs.length ? '' : `<p class="note">List the plugins the script needs in <code>"uses"</code>
        and their actions and state appear here.</p>`)
      + plugs.map(([pid, p]) => `<details class="refp" open><summary><b>${esc(p.model)}</b> <code>${esc(pid)}</code>
          ${p.instances.length ? `<span class="note">on this bench as ${esc(p.instances.join(', '))}</span>` : ''}</summary>
        ${p.manual ? `<p class="note">Manual: ${esc(p.manual.title)}${p.manual.part ? ', ' + esc(p.manual.part) : ''}${p.manual.edition ? ' (' + esc(p.manual.edition) + ')' : ''}</p>` : ''}
        ${Object.keys(p.ports.outputs).length + Object.keys(p.ports.inputs).length ? `<p class="note">Wiring ports:
          ${Object.entries(p.ports.outputs).map(([k, v]) => `<code>${esc(pid)}:${esc(k)}</code> ${esc(v)} (out)`).join(', ')}
          ${Object.entries(p.ports.inputs).map(([k, v]) => `<code>${esc(pid)}:${esc(k)}</code> ${esc(v)} (in)`).join(', ')}</p>` : ''}
        <h5>Actions — <code>bench.act(id, "name", …)</code></h5>
        ${Object.entries(p.actions).map(([a, s]) => `<div class="refa">
          <div class="row"><code class="an">${esc(a)}</code>${s.confirm ? '<span class="tag out">needs confirmed=True</span>' : ''}
            <span class="grow"></span><button data-ins="${esc(pid)}|${esc(a)}">Insert</button></div>
          <div>${esc(s.doc || '')}</div>
          ${Object.keys(s.args || {}).length ? `<div class="note">${Object.entries(s.args).map(([n, x]) =>
            `<code>${esc(n)}</code>: ${esc(argTxt(x))}${x.doc ? ' — ' + esc(x.doc) : ''}`).join('<br>')}</div>` : ''}
          ${s.scpi ? `<div class="note">sends <code>${esc(s.scpi)}</code>${s.ref ? ' · manual ' + esc(s.ref) : ''}</div>` : ''}
        </div>`).join('')}
        <h5>State — <code>bench.read(id)["field"]</code></h5>
        <table class="reft">${stateRows(p.state)}</table></details>`).join('')
      + `<details class="refp"><summary><b>The client</b> <code>from benchpanel import Bench</code></summary>
        <table class="reft">
        <tr><td><code>Bench()</code></td><td>the panel that started the script (leave the URL out)</td></tr>
        <tr><td><code>session(*ids)</code></td><td><code>with bench.session(a, b):</code> ON for the block, OFF + released after, even on an error</td></tr>
        <tr><td><code>act(id, action, **args)</code></td><td>one control; raises BenchError if refused (<code>check=False</code> returns the reply)</td></tr>
        <tr><td><code>read(id)</code></td><td>state from a poll that began AFTER this call — never stale</td></tr>
        <tr><td><code>get(id)</code></td><td>the latest state, possibly from before your last change</td></tr>
        <tr><td><code>wait_for(id, test, timeout=30)</code></td><td>poll until <code>test(state)</code> is true, e.g. <code>lambda s: s["samples"] >= 5</code></td></tr>
        <tr><td><code>all_outputs_off()</code></td><td>the red button</td></tr>
        </table>
        <p class="note">Header: <code>"wiring": [["e3641a:out", "34401a:in"]]</code> tells the simulator how to wire the bench;
          <code>"53181a:ch{channel}"</code> follows the script's <code>channel</code> setting.</p></details>`;
    box.querySelectorAll('button[data-ins]').forEach(b => b.onclick = () => {
      const [pid, a] = b.dataset.ins.split('|');
      this.insertAction(pid, a);
    });
  },
  insertAction(pid, a) {
    const s = this.ref[pid].actions[a], ta = $('ed-ta');
    const v = 'id_' + pid.replace(/-/g, '_');
    const id = new RegExp('\\ba\\.' + v + '\\b').test(ta.value) ? 'a.' + v : `"${pid}-1"`;
    const ex = s.example || {};
    const kws = Object.keys(s.args || {}).map(n => `${n}=${JSON.stringify(n in ex ? ex[n] : null)
      .replace(/^true$/, 'True').replace(/^false$/, 'False').replace(/^null$/, 'None')}`);
    if (s.confirm) kws.push('confirmed=True');
    this.insert(`bench.act(${id}, "${a}"${kws.length ? ', ' + kws.join(', ') : ''})`);
  },
  insert(text) {
    const ta = $('ed-ta');
    ta.focus();
    ta.setRangeText(text, ta.selectionStart, ta.selectionEnd, 'end');
    this.changed();
  },

  // -- typing -----------------------------------------------------------------------------
  changed() {
    if (!this.dirty) { this.dirty = true; this.paintState(); }
    this.paint(); this.scheduleCheck(); this.suggest(); this.hint();
  },
  key(e) {
    const ta = e.target;
    if (this.pop && !this.pop.hidden) {
      const items = [...this.pop.querySelectorAll('li')];
      let i = items.findIndex(li => li.classList.contains('on'));
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        i = (i + (e.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
        items.forEach((li, k) => li.classList.toggle('on', k === i));
        return;
      }
      if ((e.key === 'Enter' || e.key === 'Tab') && i >= 0) { e.preventDefault(); this.accept(items[i].dataset.v); return; }
      if (e.key === 'Escape') { e.preventDefault(); this.hidePop(); return; }
    }
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') { e.preventDefault(); this.save(); return; }
    const {selectionStart: a, selectionEnd: b, value} = ta;
    if (e.key === 'Tab') {
      e.preventDefault();
      const ls = value.lastIndexOf('\n', a - 1) + 1;
      if (a === b && !e.shiftKey) { ta.setRangeText('    ', a, b, 'end'); this.changed(); return; }
      // indent / dedent every line the selection touches
      const le = value.indexOf('\n', b - (b > a && value[b - 1] === '\n' ? 1 : 0));
      const end = le < 0 ? value.length : le;
      const block = value.slice(ls, end).split('\n')
        .map(l => e.shiftKey ? l.replace(/^ {1,4}/, '') : '    ' + l).join('\n');
      ta.setRangeText(block, ls, end, 'select');
      this.changed();
      return;
    }
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      const ls = value.lastIndexOf('\n', a - 1) + 1;
      const line = value.slice(ls, a);
      let ind = /^ */.exec(line)[0];
      if (/:\s*(#.*)?$/.test(line)) ind += '    ';
      ta.setRangeText('\n' + ind, a, b, 'end');
      this.changed();
      return;
    }
    if (e.key === 'Escape') { this.close(); }
  },

  // -- completion: action names inside act(...,"…  and state fields inside ["…  -------------
  context() {
    const ta = $('ed-ta');
    const before = ta.value.slice(Math.max(0, ta.selectionStart - 200), ta.selectionStart);
    let m = /\.act\([^()\n]*?,\s*["']([a-z0-9_]*)$/.exec(before);
    if (m) {
      const seen = {};
      for (const [pid, p] of Object.entries(this.ref))
        for (const [a, s] of Object.entries(p.actions)) (seen[a] = seen[a] || []).push(`${p.model}: ${s.doc || ''}`);
      return {prefix: m[1], items: Object.entries(seen).map(([v, d]) => ({v, d: d.join(' / ')}))};
    }
    m = /(?:\[\s*|\.get\(\s*)["']([a-z0-9_]*)$/.exec(before);
    if (m) {
      const seen = {};
      for (const p of Object.values(this.ref))
        for (const [k, f] of Object.entries(p.state)) seen[k] = seen[k] || `${p.model}: ${f.doc}${f.unit ? ' (' + f.unit + ')' : ''}`;
      return {prefix: m[1], items: Object.entries(seen).map(([v, d]) => ({v, d}))};
    }
    return null;
  },
  suggest() {
    const c = this.context();
    const pop = this.pop = $('ed-pop');
    if (!c) { this.hidePop(); return; }
    const items = c.items.filter(x => x.v.startsWith(c.prefix) && x.v !== c.prefix).slice(0, 12);
    if (!items.length) { this.hidePop(); return; }
    pop.innerHTML = '<ul>' + items.map((x, i) =>
      `<li data-v="${esc(x.v)}" class="${i ? '' : 'on'}"><code>${esc(x.v)}</code> <span>${esc(x.d)}</span></li>`).join('') + '</ul>';
    pop.querySelectorAll('li').forEach(li => li.onmousedown = e => { e.preventDefault(); this.accept(li.dataset.v); });
    // Monospace and no wrapping: the caret's position is line x column.
    const ta = $('ed-ta'), cs = getComputedStyle(ta);
    const lines = ta.value.slice(0, ta.selectionStart).split('\n');
    const lh = parseFloat(cs.lineHeight) || 18, cw = this.charWidth();
    pop.hidden = false;
    // Below the line if it fits, else above it (the last lines of a long file).
    const below = parseFloat(cs.paddingTop) + lines.length * lh - ta.scrollTop + 2;
    const room = ta.clientHeight;
    pop.style.top = (below + pop.offsetHeight <= room ? below : Math.max(0, below - lh - pop.offsetHeight - 4)) + 'px';
    pop.style.left = Math.max(0, Math.min(ta.clientWidth - pop.offsetWidth,
      parseFloat(cs.paddingLeft) + lines[lines.length - 1].length * cw - ta.scrollLeft)) + 'px';
  },
  accept(v) {
    const c = this.context(), ta = $('ed-ta');
    if (!c) return;
    ta.setRangeText(v.slice(c.prefix.length), ta.selectionStart, ta.selectionStart, 'end');
    this.hidePop();
    this.changed();
    this.hidePop();
  },
  hidePop() { if ($('ed-pop')) $('ed-pop').hidden = true; },
  charWidth() {
    if (!this._cw) {
      const s = document.createElement('span');
      s.textContent = 'x'.repeat(100);
      s.style.cssText = 'position:absolute;visibility:hidden;white-space:pre;font:' + getComputedStyle($('ed-ta')).font;
      document.body.appendChild(s);
      this._cw = s.getBoundingClientRect().width / 100;
      s.remove();
    }
    return this._cw;
  },
  // The signature of the action the caret is in, under the code.
  hint() {
    const ta = $('ed-ta');
    const before = ta.value.slice(Math.max(0, ta.selectionStart - 300), ta.selectionStart);
    const m = /\.act\([^()\n]*?,\s*["']([a-z0-9_]+)["'][^()\n]*$/.exec(before);
    let txt = '';
    if (m) {
      for (const p of Object.values(this.ref)) {
        const s = p.actions[m[1]];
        if (!s) continue;
        const args = Object.entries(s.args || {}).map(([n, a]) =>
          `${n}=<${a.type}${a.unit ? ' ' + a.unit : ''}${a.choices ? ' ' + a.choices.join('|') : ''}>`);
        if (s.confirm) args.push('confirmed=True');
        txt = `${p.model} · act(id, "${m[1]}"${args.length ? ', ' + args.join(', ') : ''}) — ${s.doc || ''}`;
        break;
      }
    }
    $('ed-hint').textContent = txt;
  },

  // -- saving ------------------------------------------------------------------------------------
  async save(then) {
    const btn = $('ed-save');
    btn.disabled = true;
    const r = await post('api/scripts', {op: 'save', script: this.id, text: $('ed-ta').value, sha256: this.sha});
    btn.disabled = false;
    if (!r.ok) { $('ed-msg').textContent = r.msg; $('ed-msg').className = 'bad'; return false; }
    this.sha = r.sha256; this.dirty = false; this.paintState();
    $('ed-msg').className = 'note';
    $('ed-msg').textContent = r.unchanged ? 'No changes to save.'
      : `Saved ${r.id}.py${r.backup ? ` — the previous copy was kept as ${r.backup}` : ''}.`
        + (r.check.ok ? '' : ' It does not pass the checks yet, so it cannot be run.');
    this.check = {...r.check}; this.renderChecks(); this.paint();
    if (Scripts.isOpen()) Scripts.load();
    return r.check.ok;
  },
  async saveAndSimulate() {
    const ok = await this.save();
    if (ok === false) {
      if (!$('ed-msg').textContent.startsWith('Saved') && !$('ed-msg').textContent.startsWith('No changes')) return;
      $('ed-msg').textContent += ' Fix the checks, then simulate.';
      return;
    }
    if (window.SIMULATED) {
      this.close();
      await Scripts.open();
      Scripts.form(this.id);
    } else {
      window.open('sim/#run=' + encodeURIComponent(this.id), 'benchsim');
    }
  },
};

(function wire() {
  const ta = $('ed-ta');
  ta.addEventListener('input', () => Editor.changed());
  ta.addEventListener('keydown', e => Editor.key(e));
  ta.addEventListener('scroll', () => { Editor.sync(); Editor.hidePop(); });
  ta.addEventListener('click', () => { Editor.hidePop(); Editor.hint(); });
  ta.addEventListener('keyup', e => { if (/^Arrow|Home|End/.test(e.key)) Editor.hint(); });
  ta.addEventListener('blur', () => setTimeout(() => Editor.hidePop(), 150));
  $('ed-save').onclick = () => Editor.save();
  $('ed-sim').onclick = () => Editor.saveAndSimulate();
  $('ed-close').onclick = () => Editor.close();
  $('ed-create').onclick = () => Editor.create();
  document.querySelectorAll('#ed .tabs button').forEach(b => b.onclick = () => {
    document.querySelectorAll('#ed .tabs button').forEach(x => x.classList.toggle('sel', x === b));
    $('ed-checks').hidden = b.dataset.tab !== 'checks';
    $('ed-ref').hidden = b.dataset.tab !== 'ref';
  });
  window.addEventListener('beforeunload', e => { if (Editor.isOpen() && Editor.dirty) { e.preventDefault(); e.returnValue = ''; } });
})();
