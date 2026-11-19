// Chart ranges for the ticker card. A range is a bar size MasonChart already knows (TF_LIST), how many bars to
// fetch from /api/bars (limit ≤ 1000), and how many trading sessions to show. The visible window is counted in
// sessions (US/Eastern calendar days that have bars), so 1D is the latest session's bars whatever the bar count
// (pre/post-market bars included when the IEX feed has them), and 1Y is the last 252 daily bars.
export const RANGES = [
  { key: '1D', tf: '5m', limit: 200, sessions: 1, title: 'Latest session, 5-minute bars' },
  { key: '1W', tf: '30m', limit: 300, sessions: 5, title: 'Last 5 sessions, 30-minute bars' },
  { key: '1M', tf: '1h', limit: 400, sessions: 21, title: 'Last 21 sessions, hourly bars' },
  { key: '3M', tf: '1d', limit: 300, sessions: 63, title: 'Last 63 sessions, daily bars' },
  { key: '1Y', tf: '1d', limit: 300, sessions: 252, title: 'Last 252 sessions, daily bars' }
];
export const DEFAULT_RANGE = '1D';
export const rangeOf = key => RANGES.find(r => r.key === key) || RANGES[0];

let etFmt = null;
try {
  etFmt = new Intl.DateTimeFormat('en-CA', { timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit' });
} catch {
  etFmt = null;
}
/** Bar time (ms) → its US/Eastern calendar day ("2026-11-18"); UTC day when the zone is unavailable. */
export function etDay(t) {
  const d = new Date(t);
  if (Number.isNaN(d.getTime())) return '';
  return etFmt ? etFmt.format(d) : d.toISOString().slice(0, 10);
}

/** view(bars) for MasonChart.setWindow: how many of the newest bars fall in the last `n` sessions (≥ 2). */
export function sessionsView(n) {
  const want = Math.max(1, Math.floor(n) || 1);
  return function view(bars) {
    if (!Array.isArray(bars) || !bars.length) return 60;
    let days = 0;
    let last = null;
    let count = 0;
    for (let i = bars.length - 1; i >= 0; i--) {
      const b = bars[i];
      const d = b && b.t != null ? etDay(b.t) : '';
      if (d !== last) {
        days += 1;
        last = d;
        if (days > want) break;
      }
      count += 1;
    }
    return Math.max(2, Math.min(count, bars.length));
  };
}
