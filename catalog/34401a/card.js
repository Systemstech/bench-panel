// 34401A card. No outputs, so nothing here needs a two-click arm.
BenchPanel.register('34401a', {
  mount(card) {
    card.head.innerHTML = '<span class="pill" data-p="rng">--</span>';
    card.k('reset').onclick = () => card.act('reset_stats');
    card.k('errors').onclick = () => card.act('clear_errors');
  },

  update(card, d) {
    const k = card.k, f = card.fmt;
    k('val').textContent = f(d.reading, 6);
    k('unit').textContent = d.unit || '';
    k('min').textContent = f(d.min, 5);
    k('max').textContent = f(d.max, 5);
    k('avg').textContent = f(d.avg, 5);
    k('n').textContent = d.samples ?? 0;
    card.head.querySelector('[data-p="rng"]').textContent =
      'RANGE ' + (d.range ?? '--') + (d.nplc != null ? ` · NPLC ${d.nplc}` : '');
    if (d.functions) card.buttons(k('funcs'), d.functions, d.function,
      v => card.act('function', {value: v}));
    card.buttons(k('ranges'), ['AUTO', '0.1', '1', '10', '100', '1000'], d.range,
      v => card.act('range', {value: v}));
    if (d.nplc_choices) card.buttons(k('nplc'), d.nplc_choices, d.nplc,
      v => card.act('nplc', {value: v}));
  },
});
