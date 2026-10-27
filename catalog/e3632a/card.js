// E3632A card. Every control goes through card.act(); the server re-checks
// every limit and every confirmation, so this file is convenience, not safety.
BenchPanel.register('e3632a', {
  mount(card) {
    const k = card.k, L = card.local;
    L.slot = 1;
    card.head.innerHTML = '<span class="pill" data-p="mode">--</span><span class="pill" data-p="out">--</span>';
    ['LOW', 'HIGH'].forEach(r => { k('r-' + r).onclick = () => card.act('set_range', {value: r}); });
    k('set-v').onclick = () => card.act('set_v', {value: +k('in-v').value});
    k('set-i').onclick = () => card.act('set_i', {value: +k('in-i').value});
    k('set-ovp').onclick = () => card.act('set_ovp', {value: +k('in-ovp').value});
    k('ovp-on').onclick = () => card.act('ovp_state', {value: true});
    k('ovp-off').onclick = () => card.act('ovp_state', {value: false});
    k('ovp-clear').onclick = () => card.act('ovp_clear');
    k('set-ocp').onclick = () => card.act('set_ocp', {value: +k('in-ocp').value});
    k('ocp-on').onclick = () => card.act('ocp_state', {value: true});
    k('ocp-off').onclick = () => card.act('ocp_state', {value: false});
    k('ocp-clear').onclick = () => card.act('ocp_clear');
    k('out-on').onclick = e => card.arm(e.target, 'CONFIRM — OUTPUT ON',
      () => card.act('output', {value: true, confirmed: true}));
    k('out-off').onclick = () => card.act('output', {value: false});
    k('errors').onclick = () => card.act('clear_errors');
    // A setup INCLUDES the output state: Save is refused while on, Recall is two clicks.
    k('save').onclick = () => card.act('save_state', {slot: L.slot});
    k('recall').onclick = e => card.arm(e.target, `CONFIRM — RECALL ${L.slot}`,
      () => card.act('recall_state', {slot: L.slot, confirmed: true}));
  },

  update(card, p) {
    const k = card.k, L = card.local, f = card.fmt;
    k('v').textContent = f(p.v_meas, 3);
    k('i').textContent = f(p.i_meas, 4);
    // A readback that contradicts the regulation mode is not a measurement: dim it
    // and say so, rather than show a confident wrong number.
    const bad = p.readback_ok === false;
    k('v').style.opacity = k('i').style.opacity = bad ? 0.35 : '';
    k('rbnote').style.display = bad ? '' : 'none';
    k('vset').textContent = f(p.v_set, 3);
    k('iset').textContent = f(p.i_set, 3);
    k('ovp').textContent = f(p.ovp, 1);
    k('ocp').textContent = f(p.ocp, 2);
    const out = card.head.querySelector('[data-p="out"]');
    out.textContent = p.output == null ? '--' : (p.output ? 'OUTPUT ON' : 'OUTPUT OFF');
    out.className = 'pill ' + (p.output ? 'on' : 'off');
    const mode = card.head.querySelector('[data-p="mode"]');
    const trip = p.ovp_tripped ? 'OVP TRIPPED' : p.ocp_tripped ? 'OCP TRIPPED' : null;
    const m = trip || (bad ? 'READBACK?' : p.mode || '—');
    mode.textContent = m;
    mode.className = 'pill ' + (trip || bad || m === 'FAIL' ? 'trip' : m === 'CC' ? 'cc' : m === 'CV' ? 'cv' : 'off');
    ['LOW', 'HIGH'].forEach(r => k('r-' + r).classList.toggle('sel', p.range === r));
    k('in-v').max = p.vmax ?? 30; k('in-i').max = p.imax ?? 7;
    card.buttons(k('slots'), [1, 2, 3].map(n => ({label: String(n), value: n})),
      L.slot, n => { L.slot = n; card.repaint(); });
    [...k('slots').children].forEach(b => b.classList.toggle('sel', +b.dataset.v === L.slot));
    k('save').textContent = `Save to ${L.slot}`;
    if (!card.isArmed(k('recall'))) k('recall').textContent = `Recall ${L.slot}`;
  },
});
