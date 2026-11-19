// Small pieces shared by the four S3-F5 widgets (rhPvr, rhBehavior, rhWhatIf, rhStories).
// Lives in the rhPvr folder because the collision rules give F5 only its own type folders.
//  - useAccountScopes(): account → "personal" | "family", read from /api/rh/accounts (the route
//    carries `scope`; K5 resolved by data). Shares the one cached request with F4's rhAccounts.
//    If that route is off or failing we label nothing: no account name is hard-coded.
//  - RollNumber: React Bits <Counter> with the sign / ▲▼ / $ drawn beside it (Counter can only
//    draw unsigned digits, MODS.md M-Counter note) and the exact text for screen readers.
//  - Pills: a segmented toggle (aria-pressed buttons) for range / horizon selectors.
import { useMemo } from 'react';
import Counter from '../../reactbits/Counter/Counter.jsx';
import { useRhApi } from '../../lib/api.js';
import { isNum, nLabel, num } from '../../lib/fmt.js';

// Evil-data guards (Debugger: 1e308 / NaN / -0, 10k rows). Anything at or above SAFE_MAX is treated as
// junk: x100 or a sum would overflow to Infinity, and no real dollar / % / count here comes near it.
export const SAFE_MAX = 1e12;
/** finite and plausibly sized */
export const fin = v => isNum(v) && Math.abs(v) < SAFE_MAX;
/** "n=12"; junk n reads "n=—" (lib nLabel would print n=1e+308) */
export const nl = n => nLabel(fin(n) ? n : null);
/** "50 of 10,000 shown" */
export const capNote = (shown, total) => `${num(shown)} of ${num(total)} shown`;
/** Cap a list for rendering: {rows, total, capped} */
export function capList(list, max) {
  const all = Array.isArray(list) ? list : [];
  return { rows: all.length > max ? all.slice(0, max) : all, total: all.length, capped: all.length > max };
}

export function useAccountScopes() {
  const api = useRhApi('/api/rh/accounts');
  const data = api.status === 'ok' ? api.data : null;
  return useMemo(() => {
    const map = {};
    if (Array.isArray(data)) {
      for (const r of data) {
        if (r && typeof r.account === 'string' && typeof r.scope === 'string') map[r.account] = r.scope;
      }
    }
    return map;
  }, [data]);
}

export const isFamily = (name, scopes) => !!scopes && scopes[name] === 'family';
export const acctLabel = (name, scopes) => `${String(name)}${isFamily(name, scopes) ? ' (family)' : ''}`;

// Counter places for an unsigned value shown with `digits` decimals: [100, 10, 1, '.', 0.1, 0.01].
function placesFor(v, digits) {
  const intDigits = v >= 1 ? Math.floor(Math.log10(v)) + 1 : 1;
  const out = [];
  for (let p = Math.min(intDigits, 12) - 1; p >= 0; p--) out.push(10 ** p);
  if (digits > 0) {
    out.push('.');
    for (let d = 1; d <= digits; d++) out.push(Number((10 ** -d).toFixed(d)));
  }
  return out;
}

/**
 * <RollNumber value={2.6251} digits={2} before="▲ +" after="%" text="+2.63%" />
 * `value` is shown as |value| rounded to `digits`; sign/arrow/units go in before/after.
 * `text` is the exact accessible string (and what tests read). Non-numbers render `text` only.
 */
export function RollNumber({ value, digits = 2, before = '', after = '', text, fontSize = 20, className = '' }) {
  if (!fin(value)) {
    // never hand Counter a non-finite or huge value (Math.round(1e308 * 100) = Infinity)
    return <span className={`mui-roll ${className}`}>{text}</span>;
  }
  const f = 10 ** digits;
  const abs = Math.round(Math.abs(value) * f) / f;
  return (
    <span className={`mui-roll ${className}`}>
      <span className="mui-sr">{text}</span>
      <span className="mui-roll__vis" aria-hidden="true">
        {before ? <span className="mui-roll__aff">{before}</span> : null}
        <Counter
          value={abs}
          places={placesFor(abs, digits)}
          fontSize={fontSize}
          padding={2}
          gap={0}
          horizontalPadding={0}
          borderRadius={0}
          gradientHeight={0}
          gradientFrom="transparent"
          gradientTo="transparent"
          fontWeight={600}
        />
        {after ? <span className="mui-roll__aff">{after}</span> : null}
      </span>
    </span>
  );
}

/** Segmented toggle. options: [{value, label, title?}] */
export function Pills({ options, value, onChange, label }) {
  return (
    <div className="mui-f5pills" role="group" aria-label={label}>
      {options.map(o => (
        <button
          key={o.value}
          type="button"
          className={`mui-f5pill${o.value === value ? ' is-on' : ''}`}
          aria-pressed={o.value === value ? 'true' : 'false'}
          title={o.title}
          onClick={() => o.value !== value && onChange(o.value)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}
