// Money on the React Bits Counter (rolling digits, one-shot per value change, jumps under reduced
// motion via M-Counter-1). Counter can't draw a sign, "$" or thousands separators (MODS.md note),
// so this wrapper renders the sign and "$" itself and gives each 3-digit group its own Counter,
// with "," and "." between them. The exact value is always present as text for screen readers
// and tests (.mui-sr); the rolling digits are aria-hidden.
import Counter from '../../reactbits/Counter/Counter.jsx';
import { money, DASH } from '../../lib/fmt.js';
import { fin, MONEY_MAX } from './guard.js';

const PLACES = { 1: [1], 2: [10, 1], 3: [100, 10, 1] };

export function splitMoney(v) {
  const cents = Math.round(Math.abs(v) * 100);
  const int = Math.floor(cents / 100);
  const frac = cents % 100;
  const s = String(int);
  const groups = [];
  for (let end = s.length; end > 0; end -= 3) groups.unshift(s.slice(Math.max(0, end - 3), end));
  return { neg: v < 0 && cents !== 0, groups: groups.map(g => ({ value: Number(g), len: g.length })), frac };
}

export default function MoneyCounter({ value: raw, fontSize = 15, signed = false, className = '' }) {
  // Out-of-range money (> $1T, ±Infinity, NaN) never reaches Counter: it would split into
  // exponent strings / Infinity digits. Shown as "—" with a reason for screen readers.
  const value = fin(raw, MONEY_MAX);
  if (value == null) {
    return (
      <span className={`rha-money ${className}`} title="value missing or out of range">
        {DASH}
      </span>
    );
  }
  const { neg, groups, frac } = splitMoney(value);
  const sign = neg ? '-' : signed && Math.round(Math.abs(value) * 100) !== 0 ? '+' : '';
  const common = {
    fontSize,
    padding: 0,
    gap: 0,
    horizontalPadding: 0,
    borderRadius: 0,
    gradientHeight: 0,
    gradientFrom: 'transparent',
    textColor: 'inherit',
    fontWeight: 'inherit'
  };
  return (
    <span className={`rha-money ${className}`}>
      <span className="mui-sr">{money(value, { signed })}</span>
      <span className="rha-money__roll" aria-hidden="true">
        {sign}$
        {groups.map((g, i) => (
          <span key={`${groups.length}-${i}`} className="rha-money__grp">
            {i > 0 ? ',' : ''}
            <Counter value={g.value} places={i === 0 ? PLACES[g.len] : PLACES[3]} {...common} />
          </span>
        ))}
        .
        <Counter value={frac} places={PLACES[2]} {...common} />
      </span>
    </span>
  );
}
