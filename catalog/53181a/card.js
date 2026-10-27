// 53181A card. No outputs: nothing here needs a two-click arm.
BenchPanel.register('53181a', {
  mount(card) {
    const k = card.k;
    card.head.innerHTML = '<span class="pill" data-p="st">--</span><span class="pill" data-p="ref">--</span>';
    k('set-gate').onclick = () => card.act('set_gate', {value: +k('in-gate').value});
    ['50', '1E6'].forEach(v => { k('imp-' + v).onclick = () => card.act('set_imp', {value: v}); });
    ['AC', 'DC'].forEach(v => { k('coup-' + v).onclick = () => card.act('set_coup', {value: v}); });
    ['1', '10'].forEach(v => { k('att-' + v).onclick = () => card.act('set_att', {value: v}); });
    k('filt').onclick = () => card.act('set_filter', {value: !card.state.ch1.filter});
    k('save').onclick = () => card.act('save_state', {slot: +k('slot').value});
    k('recall').onclick = () => card.act('recall_state', {slot: +k('slot').value});
    k('reset').onclick = () => card.act('reset_stats');
    k('errors').onclick = () => card.act('clear_errors');
  },

  update(card, c) {
    const k = card.k;
    // Show exactly the digits the counter measured to -- no more, no fewer.
    const shown = c.shown == null ? null
      : (c.digits ? Number(c.shown).toPrecision(Math.max(1, Math.min(15, c.digits))) : card.fmt(c.shown, 6));
    k('val').textContent = shown ?? (c.status === 'no signal' ? 'NO SIGNAL' : '------');
    k('unit').textContent = shown == null ? '' : (c.shown_unit || '');
    const f = v => v == null ? '--' : Number(v).toPrecision(10);
    k('min').textContent = f(c.min); k('max').textContent = f(c.max);
    k('avg').textContent = f(c.avg); k('n').textContent = c.samples ?? 0;

    const st = card.head.querySelector('[data-p="st"]');
    st.textContent = (c.status || '--').toUpperCase();
    st.className = 'pill ' + (c.status === 'ok' ? 'on' : c.status === 'no signal' ? 'trip' : 'off');
    const ref = card.head.querySelector('[data-p="ref"]');
    ref.textContent = c.ref ? `REF ${c.ref}${c.ref_auto ? ' (auto)' : ''}` : 'REF --';
    ref.className = 'pill ' + (c.ref === 'EXT' ? 'cv' : 'off');

    const labels = {'FREQ 1': 'Freq Ch1', 'FREQ 2': 'Freq Ch2', 'PER 1': 'Period Ch1',
                    'PER 2': 'Period Ch2', 'RAT 1,2': 'Ratio 1/2', 'RAT 2,1': 'Ratio 2/1'};
    if (c.functions) card.buttons(k('funcs'), c.functions.map(v => ({label: labels[v] || v, value: v})),
      c.function, v => card.act('set_func', {value: v}));
    card.buttons(k('gates'), [0.01, 0.1, 1, 10].map(g => ({label: g + ' s', value: g})),
      c.gate_s, v => card.act('set_gate', {value: v}));
    const c1 = c.ch1 || {};
    k('imp-50').classList.toggle('sel', c1.imp === 50);
    k('imp-1E6').classList.toggle('sel', c1.imp === 1e6);
    ['AC', 'DC'].forEach(v => k('coup-' + v).classList.toggle('sel', c1.coup === v));
    k('att-1').classList.toggle('sel', c1.att === 1);
    k('att-10').classList.toggle('sel', c1.att === 10);
    k('filt').classList.toggle('sel', !!c1.filter);
    const c2 = c.ch2 || {};
    k('ch2').textContent = `Channel 2: ${c2.imp === 50 ? '50 Ω' : (c2.imp ?? '?')} ${c2.coup || ''}, fixed (Option 030).`
      + (c.gate_s != null ? `  Gate ${c.gate_s} s.` : '')
      + (c.reading_age_s != null && c.status !== 'ok' ? `  Last good reading ${c.reading_age_s} s ago.` : '');
  },
});
