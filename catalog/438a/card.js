// 438A card. Every control goes through card.act(); the server re-checks every
// limit and confirmation, and refuses everything while a ZERO or CAL ADJ runs.
const MODES_438A = ['A', 'B', 'A/B', 'B/A', 'A-B', 'B-A'];

// Watts in engineering form: 1.000 mW, 12.34 uW. Other units as sent.
function read438a(v, unit) {
  if (v === null || v === undefined) return ['--.--', unit || ''];
  if (unit !== 'W') return [v.toFixed(unit === '%' ? 2 : 3), unit];
  const a = Math.abs(v);
  const [d, p] = a >= 1 ? [1, 'W'] : a >= 1e-3 ? [1e-3, 'mW'] : a >= 1e-6 ? [1e-6, 'uW'] : [1e-9, 'nW'];
  return [(v / d).toPrecision(4), p];
}

function extras438a(s) {
  if (!s) return '';
  const r = s.range_auto ? `auto ${s.range ?? ''}` : `rng ${s.range}`;
  const f = s.filter_auto ? 'auto' : `f${s.filter}`;
  return `${s.offset ? (s.offset > 0 ? '+' : '') + s.offset.toFixed(2) + ' dB · ' : ''}${r} · ${f}`;
}

BenchPanel.register('438a', {
  mount(card) {
    const k = card.k, L = card.local;
    L.ch = 'A';
    card.head.innerHTML = '<span class="pill" data-p="mode">--</span><span class="pill" data-p="osc">--</span>';
    k('u-W').onclick = () => card.act('units', {value: 'W'});
    k('u-dBm').onclick = () => card.act('units', {value: 'dBm'});
    k('rel-on').onclick = () => card.act('rel', {value: true});
    k('rel-off').onclick = () => card.act('rel', {value: false});
    ['A', 'B'].forEach(c => { k('ch-' + c).onclick = () => { L.ch = c; card.repaint(); }; });
    k('set-cf').onclick = () => card.act('cal_factor', {channel: L.ch, value: +k('in-cf').value});
    k('set-os').onclick = () => card.act('offset', {channel: L.ch, value: +k('in-os').value});
    // ZERO with RF on the sensor becomes an offset in every later reading, and CAL ADJ
    // replaces the channel's calibration: two clicks each.
    k('zero').onclick = e => card.arm(e.target, `CONFIRM — ZERO ${L.ch} (no RF on it)`,
      () => card.act('zero', {channel: L.ch, confirmed: true}));
    k('cal').onclick = e => card.arm(e.target, `CONFIRM — CAL ${L.ch} (sensor on POWER REF)`,
      () => card.act('cal_adj', {channel: L.ch, ref_cf: +k('in-ref').value, confirmed: true}));
    k('osc-on').onclick = e => card.arm(e.target, 'CONFIRM — REF ON',
      () => card.act('osc', {value: true, confirmed: true}));
    k('osc-off').onclick = () => card.act('osc', {value: false});
  },

  update(card, p) {
    const k = card.k, L = card.local;
    const [txt, unit] = read438a(p.reading, p.unit);
    k('rd').textContent = p.busy ? '- - - - - -' : txt;
    k('unit').textContent = p.busy ? '' : unit;
    k('mode').textContent = (p.mode || '--') + (p.rel ? ' REL' : '');
    const sa = p.sensor_a || {}, sb = p.sensor_b || {};
    k('cfa').textContent = sa.cal_factor != null ? sa.cal_factor.toFixed(1) : '--';
    k('cfb').textContent = sb.cal_factor != null ? sb.cal_factor.toFixed(1) : '--';
    k('xa').textContent = extras438a(p.sensor_a);
    k('xb').textContent = extras438a(p.sensor_b);

    const mp = card.head.querySelector('[data-p="mode"]');
    mp.textContent = p.busy ? p.busy.toUpperCase() : p.meas_error ? `ERROR ${String(p.meas_error).padStart(2, '0')}`
      : p.mode ? `${p.mode} · ${p.units}` : '--';
    mp.className = 'pill ' + (p.busy ? 'cc' : p.meas_error ? 'trip' : p.mode ? 'cv' : 'off');
    const op = card.head.querySelector('[data-p="osc"]');
    op.textContent = p.osc == null ? '--' : p.osc ? 'POWER REF ON' : 'REF off';
    op.className = 'pill ' + (p.osc ? 'on' : 'off');

    const warn = [p.meas_error_text, p.entry_error_text].filter(Boolean).join(' · ');
    k('warn').style.display = warn ? '' : 'none';
    k('warntext').textContent = warn;
    const note = p.busy ? `${p.busy}: nothing else is sent until it finishes (any command would abort it)`
      : p.last_op || '';
    k('note').style.display = note ? '' : 'none';
    k('notetext').textContent = note;

    card.buttons(k('modes'), MODES_438A, p.mode, m => card.act('measure', {value: m}));
    ['W', 'dBm'].forEach(u => k('u-' + u).classList.toggle('sel', p.units === u));
    k('rel-on').classList.toggle('sel', !!p.rel);
    ['A', 'B'].forEach(c => k('ch-' + c).classList.toggle('sel', L.ch === c));
    k('chname').textContent = L.ch;
    const s = L.ch === 'A' ? sa : sb;
    const rcur = s.range_auto ? 'auto' : s.range != null ? String(s.range) : null;
    const fcur = s.filter_auto ? 'auto' : s.filter != null ? String(s.filter) : null;
    card.buttons(k('ranges'), ['auto', 'hold', '1', '2', '3', '4', '5'], rcur,
      v => card.act('range', {channel: L.ch, value: v}));
    card.buttons(k('filters'), ['auto', 'hold', '0', '1', '2', '3', '4', '5', '6', '7', '8', '9'], fcur,
      v => card.act('filter', {channel: L.ch, value: v}));
    if (!card.isArmed(k('zero'))) k('zero').textContent = `ZERO ${L.ch}`;
    if (!card.isArmed(k('cal'))) k('cal').textContent = `CAL ADJ ${L.ch}`;
  },
});
