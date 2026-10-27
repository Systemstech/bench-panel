// 8648A card. The level shown and entered is ABSOLUTE dBm at the connector; the
// driver converts to and from reference mode. Reference mode itself is read-only.
BenchPanel.register('8648a', {
  mount(card) {
    const k = card.k;
    card.head.innerHTML = '<span class="pill" data-p="ref">--</span><span class="pill" data-p="out">--</span>';
    k('set-f').onclick = () => card.act('set_freq', {value: +k('in-f').value});
    k('set-a').onclick = () => card.act('set_ampl', {value: +k('in-a').value});
    // RF is an output too: two clicks, and the server refuses it without the flag.
    k('rf-on').onclick = e => card.arm(e.target, 'CONFIRM — RF ON',
      () => card.act('output', {value: true, confirmed: true}));
    k('rf-off').onclick = () => card.act('output', {value: false});
    k('errors').onclick = () => card.act('clear_errors');
  },

  update(card, g) {
    const k = card.k, f = card.fmt;
    // Showing the held delta as dBm is the mistake that cost a day on 2026-09-15.
    k('ampl').textContent = f(g.ampl_abs, 2);
    k('freq').textContent = g.freq_hz != null ? (g.freq_hz / 1e6).toFixed(6) : '---.------';
    k('entered').textContent = g.ampl_entered != null
      ? f(g.ampl_entered, 2) + (g.ref_on ? ' dB (delta)' : ' dBm') : '--';
    k('refval').textContent = g.ref_dbm != null ? f(g.ref_dbm, 2) + ' dBm' : '--';
    k('mod').textContent = (g.mod && g.mod.length) ? g.mod.join('+') : 'none';
    const out = card.head.querySelector('[data-p="out"]');
    out.textContent = g.output == null ? '--' : (g.output ? 'RF ON' : 'RF OFF');
    out.className = 'pill ' + (g.output ? 'on' : 'off');
    const ref = card.head.querySelector('[data-p="ref"]');
    ref.textContent = g.ref_on == null ? '--' : (g.ref_on ? 'REF MODE' : 'ABSOLUTE');
    ref.className = 'pill ' + (g.ref_on ? 'cc' : 'off');
    const b = k('banner');
    b.style.display = g.warn ? '' : 'none';
    b.textContent = g.warn || '';
    if (g.ampl_min != null) { k('in-a').min = g.ampl_min; k('in-a').max = g.ampl_max; }
  },
});
