// E3641A card. Every control goes through card.act(); the server re-checks
// every limit and every confirmation, so this file is convenience, not safety.
BenchPanel.register('e3641a', {
  mount(card) {
    const k = card.k, L = card.local;
    L.slot = 1;
    // Header pills: regulation mode and output state.
    card.head.innerHTML = '<span class="pill" data-p="mode">--</span><span class="pill" data-p="out">--</span>';
    ['LOW', 'HIGH'].forEach(r => { k('r-' + r).onclick = () => card.act('set_range', {value: r}); });
    k('set-v').onclick = () => card.act('set_v', {value: +k('in-v').value});
    k('set-i').onclick = () => card.act('set_i', {value: +k('in-i').value});
    k('set-ovp').onclick = () => card.act('set_ovp', {value: +k('in-ovp').value});
    k('ovp-on').onclick = () => card.act('ovp_state', {value: true});
    k('ovp-off').onclick = () => card.act('ovp_state', {value: false});
    k('ovp-clear').onclick = () => card.act('ovp_clear');
    // An output is the one thing on a bench that can damage something: two clicks.
    k('out-on').onclick = e => card.arm(e.target, 'CONFIRM — OUTPUT ON',
      () => card.act('output', {value: true, confirmed: true}));
    k('out-off').onclick = () => card.act('output', {value: false});
    k('errors').onclick = () => card.act('clear_errors');
    // Saved setups. The stored setup INCLUDES the output state, so the server
    // refuses Save while the output is on, and Recall takes two clicks.
    k('save').onclick = async () => {
      const r = await card.act('save_state', {slot: L.slot, name: k('slotname').value.trim()});
      if (r.ok) k('slotname').value = '';
    };
    k('recall').onclick = e => card.arm(e.target, `CONFIRM — RECALL ${L.slot}`,
      () => card.act('recall_state', {slot: L.slot, confirmed: true}));
  },

  update(card, p) {
    const k = card.k, L = card.local, f = card.fmt;
    k('v').textContent = f(p.v_meas, 3);
    k('i').textContent = f(p.i_meas, 4);
    k('vset').textContent = f(p.v_set, 3);
    k('iset').textContent = f(p.i_set, 3);
    k('ovp').textContent = f(p.ovp, 1);
    const out = card.head.querySelector('[data-p="out"]');
    out.textContent = p.output == null ? '--' : (p.output ? 'OUTPUT ON' : 'OUTPUT OFF');
    out.className = 'pill ' + (p.output ? 'on' : 'off');
    // With the output off the supply is regulating nothing; claiming CV would be
    // inventing a state. Show a dash instead.
    const mode = card.head.querySelector('[data-p="mode"]');
    const m = p.ovp_tripped ? 'OVP TRIPPED' : (p.mode || '—');
    mode.textContent = m;
    mode.className = 'pill ' + (p.ovp_tripped ? 'trip' : m === 'CC' ? 'cc' : m === 'CV' ? 'cv' : 'off');
    ['LOW', 'HIGH'].forEach(r => k('r-' + r).classList.toggle('sel', p.range === r));
    k('in-v').max = p.vmax ?? 60; k('in-i').max = p.imax ?? 0.8;
    const names = p.slot_names || ['', '', '', '', ''];
    card.buttons(k('slots'), names.map((nm, i) => ({label: `${i + 1}${nm ? ' · ' + nm : ''}`, value: i + 1})),
      L.slot, n => { L.slot = n; card.repaint(); });
    [...k('slots').children].forEach(b => b.classList.toggle('sel', +b.dataset.v === L.slot));
    k('save').textContent = `Save to ${L.slot}`;
    if (!card.isArmed(k('recall'))) k('recall').textContent = `Recall ${L.slot}`;
  },
});
