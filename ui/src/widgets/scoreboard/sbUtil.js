// scoreboard helpers: clean the GET /api/learning/scoreboard answer (docs/fable/SCOREBOARD_API.md) into
// safe values, and the few plain-English formatters the widget needs. Pure functions, no React.
//  - Every string from the server stays a plain string (rendered as JSX text, so React escapes it).
//  - Every number is either finite or null; null prints as a dash.
//  - Nothing here rebuilds the server's sentences (headline, target, notes, table titles / notes): they
//    are passed through as they are.
import { DASH, isNum, money, num, pct } from '../../lib/fmt.js';

export const MAX_TEXT = 4000; // a runaway string is cut, never allowed to stretch the widget forever
export const MAX_ROWS = 60; // per table (the API sends at most 14)
export const MAX_SERIES = 2000; // trading days (about eight years)

export const isObj = v => !!v && typeof v === 'object' && !Array.isArray(v);
export const arr = v => (Array.isArray(v) ? v : []);
export const str = v => (typeof v === 'string' ? (v.length > MAX_TEXT ? `${v.slice(0, MAX_TEXT)}…` : v) : '');
export const n = v => (isNum(v) ? v : null);
export const bool = v => (v === true ? true : v === false ? false : null);
const count = v => (isNum(v) && v >= 0 ? Math.round(v) : null);
export const isYmd = v => typeof v === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(v) && !Number.isNaN(Date.parse(`${v}T00:00:00Z`));

export const PERIODS = [
  { key: 'since_start', label: 'Since it began' },
  { key: 'last_week', label: 'Last week' },
  { key: 'last_month', label: 'Last month' }
];
export const PERIOD_HINT = { since_start: 'every trading day so far', last_week: 'the last 5 trading days', last_month: 'the last 21 trading days' };

/** The four verdicts the API may send (anything else is shown as given, in grey). */
export const VERDICTS = {
  'leaning good': { tone: 'good', mark: '▲' },
  'leaning bad': { tone: 'bad', mark: '▼' },
  'no clear difference': { tone: 'none', mark: '–' },
  'too few to tell': { tone: 'few', mark: '…' }
};
export function verdictOf(v) {
  const text = str(v).trim();
  const known = VERDICTS[text];
  return known ? { text, ...known } : { text: text || DASH, tone: 'none', mark: '' };
}

function cleanPeriod(p) {
  if (!isObj(p)) return null;
  return {
    from: isYmd(p.from_day) ? p.from_day : null,
    to: isYmd(p.to_day) ? p.to_day : null,
    sessions: count(p.sessions),
    mason: n(p.mason_usd),
    spy: n(p.spy_usd),
    beat: n(p.beat_usd),
    atWork: n(p.at_work_usd),
    masonPct: n(p.mason_pct),
    spyPct: n(p.spy_pct),
    beatPct: n(p.beat_pct),
    whole: p.whole_history === true
  };
}

export function cleanRow(r) {
  if (!isObj(r)) return null;
  const share = n(r.one_stock_share);
  return {
    group: str(r.group) || DASH,
    n: count(r.n),
    calls: count(r.n_calls),
    days: count(r.days),
    weeks: count(r.weeks),
    avg: n(r.avg_pct),
    typical: n(r.typical_pct),
    low: n(r.low_pct),
    high: n(r.high_pct),
    share: share != null && share >= 0 && share <= 1 ? share : null,
    verdict: verdictOf(r.verdict)
  };
}

function cleanTable(t) {
  if (!isObj(t)) return null;
  const rows = arr(t.rows).slice(0, MAX_ROWS).map(cleanRow).filter(Boolean);
  if (!rows.length) return null; // "Tables with no rows should be hidden"
  return { key: str(t.key) || 'table', title: str(t.title), note: str(t.note), rows };
}

export function cleanSeries(list) {
  const out = [];
  for (const s of arr(list)) {
    if (out.length >= MAX_SERIES) break;
    if (!isObj(s) || !isYmd(s.day)) continue;
    const mason = n(s.mason_usd);
    const spy = n(s.spy_usd);
    if (mason == null && spy == null) continue;
    out.push({ day: s.day, mason, spy, atWork: n(s.at_work_usd) });
  }
  return out;
}

/** The whole answer → the widget's model, or null when it is not a usable scoreboard. */
export function cleanScoreboard(body) {
  if (!isObj(body) || body.ok === false) return null;
  const h = isObj(body.headline) ? body.headline : {};
  const t = isObj(body.target) ? body.target : {};
  const o = isObj(body.own_trades) ? body.own_trades : {};
  const w = isObj(body.what_worked) ? body.what_worked : {};
  const periodsIn = isObj(o.periods) ? o.periods : {};
  const tables = arr(w.tables).map(cleanTable).filter(Boolean);
  // the yardstick row always comes first (the API already sends it first; this only guards the order)
  tables.sort((a, b) => (a.key === 'comparison' ? -1 : b.key === 'comparison' ? 1 : 0));
  const leans = isObj(w.leans) ? { groups: count(w.leans.groups_with_enough_data), leaning: count(w.leans.leaning), luck: count(w.leans.expected_by_luck) } : null;
  const c = isObj(w.coverage) ? w.coverage : {};
  return {
    asOf: isYmd(body.as_of) ? body.as_of : null,
    generatedAt: str(body.generated_at),
    costPct: n(body.cost_pct),
    headline: { line1: str(h.line1), line2: str(h.line2), means: str(h.how_much_it_means) },
    target: { text: str(t.text), beating: bool(t.beating_sp500), pace: n(t.yearly_pace_pct), low: n(t.target_low_pct), high: n(t.target_high_pct) },
    own: {
      nTrades: count(o.n_trades),
      nClosed: count(o.n_closed),
      nOpen: count(o.n_open),
      firstDay: isYmd(o.first_trade_day) ? o.first_trade_day : null,
      daysRunning: count(o.days_running),
      periods: PERIODS.map(p => ({ ...p, data: cleanPeriod(periodsIn[p.key]) })),
      series: cleanSeries(o.series),
      notes: arr(o.notes).map(str).filter(s => s.trim()),
      perTrade: isObj(o.per_trade) ? cleanRow({ group: "Mason's own trades", ...o.per_trade }) : null
    },
    worked: {
      horizon: count(w.horizon_days),
      tables,
      leans: leans && leans.groups != null && leans.leaning != null ? leans : null,
      coverage: {
        inTables: count(c.in_tables),
        waiting: count(c.waiting),
        lowPrice: count(c.left_out_low_price),
        noMinute: count(c.left_out_no_minute_price),
        minPrice: n(c.min_entry_price),
        notGradable: count(c.not_gradable)
      }
    }
  };
}

// ── formatting ──────────────────────────────────────────────────────────────
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

/** '2026-11-13' → 'Nov 13' (with ', 2026' when the year differs from `refYmd`'s). Never shifted by time zone. */
export function shortDay(ymd, refYmd = null) {
  if (!isYmd(ymd)) return DASH;
  const [y, m, d] = ymd.split('-').map(Number);
  const year = isYmd(refYmd) && refYmd.slice(0, 4) !== ymd.slice(0, 4) ? `, ${y}` : '';
  return `${MONTHS[m - 1]} ${d}${year}`;
}
/** '2026-11-13' → 'Fri, Nov 13, 2026'. */
export function longDay(ymd) {
  if (!isYmd(ymd)) return DASH;
  const [y, m, d] = ymd.split('-').map(Number);
  return `${WEEKDAYS[new Date(Date.UTC(y, m - 1, d)).getUTCDay()]}, ${MONTHS[m - 1]} ${d}, ${y}`;
}
/** '2026-11-15T08:55:08' (server local time) → 'Nov 15, 8:55 am'. Read off the string: no zone shift. */
export function builtAt(iso) {
  const m = typeof iso === 'string' ? iso.match(/^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2})/) : null;
  if (!m || !isYmd(m[1]) || +m[2] > 23 || +m[3] > 59) return null;
  const h = +m[2];
  return `${shortDay(m[1])}, ${h % 12 || 12}:${m[3]} ${h < 12 ? 'am' : 'pm'}`;
}

/** Money with a sign: "+$12.30" / "-$41.23" / "$0.00" / "—". */
export const usd = v => money(v, { signed: true });
/** Money without a sign (amounts at work, drops): "$4,160.81". */
export const usdPlain = v => money(v);
/** A percent that is already in percent units: "-0.99%". */
export const pct2 = v => pct(v, { digits: 2 });
/** The likely range "-0.60% to -0.18%", or a dash when either end is unknown. */
export const rangeText = (lo, hi) => (isNum(lo) && isNum(hi) ? `${pct2(lo)} to ${pct2(hi)}` : DASH);
/** "5,075" or a dash. */
export const countText = v => (isNum(v) ? num(v) : DASH);

/** The status pill: ahead / behind / no trades yet (from target.beating_sp500). */
export function standing(beating) {
  if (beating === true) return { tone: 'up', mark: '▲', text: 'Ahead of the S&P 500' };
  if (beating === false) return { tone: 'down', mark: '▼', text: 'Behind the S&P 500' };
  return { tone: 'flat', mark: '', text: 'No trades to compare yet' };
}

/** Hover text for a table row: how many ideas, over how many days / weeks, and the biggest single stock. */
export function rowDetail(r) {
  const parts = [];
  if (r.n != null) parts.push(`${num(r.n)} ${r.n === 1 ? 'idea' : 'ideas'}`);
  if (r.calls != null && r.calls !== r.n) parts.push(`from ${num(r.calls)} calls`);
  if (r.days != null) parts.push(`on ${num(r.days)} ${r.days === 1 ? 'day' : 'days'}`);
  if (r.weeks != null) parts.push(`in ${num(r.weeks)} ${r.weeks === 1 ? 'week' : 'weeks'}`);
  let s = parts.join(' ');
  if (r.share != null && r.share > 0) s += `${s ? '; ' : ''}the most common stock is ${Math.round(r.share * 100)}% of them`;
  return s;
}

// ── the chart's scale (kept here so it can be tested without a browser) ─────
/** Y range covering every value and zero, padded so the lines never touch the edge. */
export function yDomain(series) {
  let lo = 0;
  let hi = 0;
  for (const s of series) {
    for (const v of [s.mason, s.spy]) {
      if (v == null) continue;
      if (v < lo) lo = v;
      if (v > hi) hi = v;
    }
  }
  if (hi === lo) return [lo - 1, hi + 1];
  const pad = (hi - lo) * 0.1;
  return [lo - (lo < 0 ? pad : 0), hi + (hi > 0 ? pad : 0)];
}

/** SVG path for one series; a null value breaks the line. Points: [{x, y|null}]. */
export function linePath(points) {
  let d = '';
  let pen = false;
  for (const p of points) {
    if (p.y == null) {
      pen = false;
      continue;
    }
    d += `${pen ? 'L' : 'M'}${p.x.toFixed(1)},${p.y.toFixed(1)}`;
    pen = true;
  }
  return d;
}
