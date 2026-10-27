// 6063B card. Every control goes through card.act(); the server re-checks every
// limit, every confirmation and the "input off first" rules, so this is convenience.
BenchPanel.register('6063b', {
  mount(card) {
    const k = card.k, L = card.local;
    L.slot = 1;
    card.head.innerHTML = '<span class="pill" data-p="state">--</span><span class="pill" data-p="inp">--</span>';
    ['CC', 'CR', 'CV'].forEach(m => { k('m-' + m).onclick = () => card.act('set_mode', {value: m}); });
    ['LOW', 'HIGH'].forEach(r => { k('ir-' + r).onclick = () => card.act('set_i_range', {value: r}); });
    ['LOW', 'MID', 'HIGH'].forEach(r => { k('rr-' + r).onclick = () => card.act('set_r_range', {value: r}); });
    k('set-i').onclick = () => card.act('set_i', {value: +k('in-i').value});
    k('set-r').onclick = () => card.act('set_r', {value: +k('in-r').value});
    k('set-v').onclick = () => card.act('set_v', {value: +k('in-v').value});
    k('set-ocp').onclick = () => card.act('set_ocp', {value: +k('in-ocp').value});
    k('set-del').onclick = () => card.act('set_ocp_delay', {value: +k('in-del').value});
    k('ocp-on').onclick = () => card.act('ocp_state', {value: true});
    k('ocp-off').onclick = () => card.act('ocp_state', {value: false});
    // Sinking current is the thing that can hurt what is connected: two clicks.
    k('in-on').onclick = e => card.arm(e.target, 'CONFIRM — INPUT ON',
      () => card.act('input', {value: true, confirmed: true}));
    k('in-off').onclick = () => card.act('input', {value: false});
    // Clearing protection re-enables an input that was on when it tripped.
    k('clear').onclick = e => card.arm(e.target, 'CONFIRM — CLEAR (may turn input ON)',
      () => card.act('protection_clear', {confirmed: true}));
    k('errors').onclick = () => card.act('clear_errors');
    k('save').onclick = () => card.act('save_state', {slot: L.slot});
    k('recall').onclick = e => card.arm(e.target, `CONFIRM — RECALL ${L.slot}`,
      () => card.act('recall_state', {slot: L.slot, confirmed: true}));
  },

  update(card, p) {
    const k = card.k, L = card.local, f = card.fmt;
    k('v').textContent = p.overload ? 'OVLD' : f(p.v_meas, 2);
    k('i').textContent = f(p.i_meas, 3);
    k('p').textContent = f(p.p_meas, 1);
    k('iset').textContent = f(p.i_set, 3);
    k('rset').textContent = f(p.r_set, 1);
    k('vset').textContent = f(p.v_set, 2);
    k('irng').textContent = p.i_range ? `(${p.i_range === 'LOW' ? '1' : '10'} A rng)` : '';
    k('rrng').textContent = p.r_range ? `(${p.r_range})` : '';
    k('ocp').textContent = p.ocp_state ? `${f(p.ocp, 2)} A / ${f(p.ocp_delay, 1)} s` : 'off';
    // INP? stays 1 through a protection shutdown, so the pill says what is really happening.
    const inp = card.head.querySelector('[data-p="inp"]');
    const sinking = p.input && !p.shutdown;
    inp.textContent = p.input == null ? '--' : p.shutdown ? 'INPUT SHUT DOWN' : (p.input ? 'INPUT ON' : 'INPUT OFF');
    inp.className = 'pill ' + (p.shutdown ? 'trip' : sinking ? 'on' : 'off');
    const st = card.head.querySelector('[data-p="state"]');
    const faults = (p.faults || []).join(' ');
    st.textContent = p.shutdown ? `PROTECTION ${faults || 'PS'}` : faults ? faults
      : p.unregulated ? `${p.mode || ''} UNREG` : (p.mode || '—');
    st.className = 'pill ' + (p.shutdown || faults ? 'trip' : p.unregulated ? 'cc' : p.mode ? 'cv' : 'off');
    const warn = p.v_sense_suspect
      ? 'Sinking current but reading ~0 V: is the rear LCL/RMT sense switch on RMT with no sense leads?'
      : p.shutdown ? 'Protection has turned the input off. Remove the cause, then Clear protection.' : '';
    k('warn').style.display = warn ? '' : 'none';
    k('warntext').textContent = warn;
    ['CC', 'CR', 'CV'].forEach(m => k('m-' + m).classList.toggle('sel', p.mode === m));
    ['LOW', 'HIGH'].forEach(r => k('ir-' + r).classList.toggle('sel', p.i_range === r));
    ['LOW', 'MID', 'HIGH'].forEach(r => k('rr-' + r).classList.toggle('sel', p.r_range === r));
    k('in-i').max = p.i_range === 'LOW' ? 1 : 10;
    card.buttons(k('slots'), [1, 2, 3, 4, 5, 6].map(n => ({label: String(n), value: n})),
      L.slot, n => { L.slot = n; card.repaint(); });
    [...k('slots').children].forEach(b => b.classList.toggle('sel', +b.dataset.v === L.slot));
    k('save').textContent = `Save to ${L.slot}`;
    if (!card.isArmed(k('recall'))) k('recall').textContent = `Recall ${L.slot}`;
  },
});
