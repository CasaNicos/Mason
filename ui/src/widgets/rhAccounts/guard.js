// F4 number + list guards (Debugger class of bug from F1/F2: 10k-row lists froze the page;
// 1e308 ×100 or summed became Infinity and printed "NaN"/"∞"/309-digit strings).
// Every F4 widget cleans its payload through these at the boundary, so render code only ever
// sees finite, plausible numbers (or null → "—") and bounded lists.
import { isNum } from '../../lib/fmt.js';

export const MONEY_MAX = 1e12; // $1T per account / alert level: anything above is junk
export const PCT_MAX = 1e4; // percent fields (exposure %, surprise %, cash %, implied move)
export const COUNT_MAX = 1e12; // rows, fires, positions, hits
export const DAYS_MAX = 36500; // idle days

/** v when it is a finite number with |v| ≤ max, else null. -0 becomes 0. */
export function fin(v, max = MONEY_MAX) {
  if (!isNum(v) || Math.abs(v) > max) return null;
  return v === 0 ? 0 : v;
}

/** Sum of the fin() values; null when there is no valid term (never a fabricated 0) or the sum
 *  leaves the range (never Infinity). DEBUG_F4 F4-2. */
export function finSum(values, max = MONEY_MAX) {
  let s = 0;
  let n = 0;
  for (const v of values) {
    const f = fin(v, max);
    if (f != null) {
      s += f;
      n += 1;
    }
  }
  return n ? fin(s, max) : null;
}

export const count = n => (isNum(n) ? Math.round(n).toLocaleString('en-US') : '—');

/** "+9,950 more accounts" */
export const moreText = (n, noun) => `+${count(n)} more ${noun}`;

export const arr = v => (Array.isArray(v) ? v : []);
export const objs = v => arr(v).filter(x => x && typeof x === 'object' && !Array.isArray(x));
