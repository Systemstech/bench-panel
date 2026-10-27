// 33522A card: one set of controls, pointed at CH1 or CH2.
BenchPanel.register('33522a', {
  mount(card) {
    const k = card.k, L = card.local;
    L.ch = 1; L.slot = 1;
    card.head.innerHTML = '<span class="pill" data-p="o1">CH1 --</span><span class="pill" data-p="o2">CH2 --</span>';
    [1, 2].forEach(n => { k('sel-' + n).onclick = () => { L.ch = n; card.repaint(); }; });
    k('set-f').onclick = () => card.act('set_freq', {ch: L.ch, value: +k('in-f').value});
    k('set-a').onclick = () => card.act('set_ampl', {ch: L.ch, value: +k('in-a').value});
    k('set-o').onclick = () => card.act('set_offset', {ch: L.ch, value: +k('in-o').value});
    k('ld-50').onclick = () => card.act('set_load', {ch: L.ch, value: '50'});
    k('ld-INF').onclick = () => card.act('set_load', {ch: L.ch, value: 'INF'});
    k('out-on').onclick = e => card.arm(e.target, `CONFIRM — CH${L.ch} ON`,
      () => card.act('output', {ch: L.ch, value: true, confirmed: true}));
    k('out-off').onclick = () => card.act('output', {ch: L.ch, value: false});
    k('errors').onclick = () => card.act('clear_errors');
    // Save asks twice only when the location already holds a state: the server
    // checks MEM:STAT:VAL? and answers {confirm: true} instead of overwriting.
    k('save').onclick = async e => {
      const b = e.target;
      if (card.isArmed(b)) { card.disarm(b); L.saveConfirmed = true; }
      const r = await card.act('save_state',
        {slot: L.slot, name: k('name').value.trim(), confirmed: !!L.saveConfirmed});
      L.saveConfirmed = false;
      if (r.confirm) card.arm(b, `CONFIRM — OVERWRITE ${L.slot}`, () => {});
      else if (r.ok) k('name').value = '';
    };
    k('recall').onclick = e => card.arm(e.target, `CONFIRM — RECALL ${L.slot}`,
      () => card.act('recall_state', {slot: L.slot, confirmed: true}));
    k('ar-1').onclick = () => card.act('auto_recall', {value: true});
    k('ar-0').onclick = () => card.act('auto_recall', {value: false});
  },

  update(card, a) {
    const k = card.k, L = card.local, f = card.fmt;
    [1, 2].forEach(n => {
      const c = a['ch' + n] || {};
      const [fv, fu] = c.func === 'DC' ? ['DC', ''] : card.fmtHz(c.freq_hz);
      k(`${n}-f`).textContent = fv; k(`${n}-fu`).textContent = fu;
      k(`${n}-fn`).textContent = c.func || '--';
      k(`${n}-a`).textContent = c.ampl != null ? `${f(c.ampl, 4)} ${c.unit || ''}` : '--';
      k(`${n}-o`).textContent = c.offset != null ? f(c.offset, 4) + ' V' : '--';
      k(`${n}-l`).textContent = c.load || '--';
      const o = card.head.querySelector(`[data-p="o${n}"]`);
      o.textContent = `CH${n} ` + (c.output == null ? '--' : (c.output ? 'ON' : 'OFF'));
      o.className = 'pill ' + (c.output ? 'on' : 'off');
      k('sel-' + n).classList.toggle('sel', n === L.ch);
    });
    if (!card.isArmed(k('out-on'))) k('out-on').textContent = `CH${L.ch} OUTPUT ON`;
    k('out-off').textContent = `CH${L.ch} OUTPUT OFF`;
    const cc = a['ch' + L.ch] || {};
    if (a.funcs) card.buttons(k('funcs'), a.funcs, cc.func,
      v => card.act('set_func', {ch: L.ch, value: v}));
    k('ld-50').classList.toggle('sel', !!cc.load && cc.load !== 'HighZ');
    k('ld-INF').classList.toggle('sel', cc.load === 'HighZ');
    const sl = a.slots || [1, 2, 3, 4].map(n => ({name: 'STATE_' + n, valid: null}));
    card.buttons(k('slots'),
      sl.map((x, i) => ({label: `${i + 1} · ${x.name}${x.valid === false ? ' (empty)' : ''}`, value: i + 1})),
      L.slot, n => { L.slot = n; card.disarm(k('save')); card.repaint(); });
    if (!card.isArmed(k('save'))) k('save').textContent = `Save to ${L.slot}`;
    if (!card.isArmed(k('recall'))) k('recall').textContent = `Recall ${L.slot}`;
    k('ar-1').classList.toggle('sel', a.auto_recall === true);
    k('ar-0').classList.toggle('sel', a.auto_recall === false);
  },
});
