// rhStatus — "Is the Robinhood data healthy?" (S3 §3 F4.5, contracts §6 /api/rh/status).
// React Bits GlassSurface pills: connection, last sync, backfill, rate-limit hits, xcheck
// mismatches — every state in words (never color alone). Per-module lines underneath.
// Backfill progress is not in the route (K6): the pill says "not reported" and shows the row
// count we do have.
import GlassSurface from '../../reactbits/GlassSurface/GlassSurface.jsx';
import { useRhApi } from '../../lib/api.js';
import { RhState } from '../../lib/states.jsx';
import { dateTime, num, DASH } from '../../lib/fmt.js';
import { fin, objs, moreText, COUNT_MAX } from '../rhAccounts/guard.js';
import './rhStatus.css';

const MOD_CAP = 50; // module lines drawn; the rest are counted
const ERR_TITLE = 5; // errors listed in a hover title
const mods = d => objs(d && d.modules);
/** Sum of a count field; null ("—") when any module reports a junk value, when NO module reports
 *  one (never a fabricated 0, DEBUG_F4 F4-2), or when the sum leaves range. */
const total = (ms, k) => {
  let s = 0;
  let n = 0;
  for (const m of ms) {
    if (m[k] == null) continue;
    const v = fin(m[k], COUNT_MAX);
    if (v == null || v < 0) return null;
    s += v;
    n += 1;
  }
  return n ? fin(s, COUNT_MAX) : null;
};
const ts = v => {
  const t = typeof v === 'string' ? Date.parse(v) : NaN;
  return Number.isFinite(t) ? t : null;
};

export function summarize(d) {
  const ms = mods(d);
  let last = null;
  for (const m of ms) {
    const t = ts(m.last_run);
    if (t != null && (last == null || t > last.t)) last = { t, iso: m.last_run };
  }
  return {
    ms,
    connected: d && d.connected === true ? 'yes' : d && d.connected === false ? 'no' : 'unknown',
    last,
    rows: total(ms, 'rows'),
    rate: total(ms, 'rate_hits'),
    xcheck: total(ms, 'xcheck_mismatch'),
    truncated: ms.filter(m => m.truncated === true).length,
    errored: ms.filter(m => Array.isArray(m.errors) && m.errors.length).length
  };
}

function Pill({ k, label, value, sub, tone = '', title }) {
  return (
    <div className={`rhs-pill${tone ? ` is-${tone}` : ''}`} data-testid={`pill-${k}`} title={title}>
      <GlassSurface width="100%" height="100%" borderRadius={12} brightness={20} opacity={0.6} blur={8} backgroundOpacity={0.08}>
        <span className="rhs-pill__in">
          <span className="rhs-pill__label">{label}</span>
          <span className="rhs-pill__value">{value}</span>
          {sub ? <span className="rhs-pill__sub">{sub}</span> : null}
        </span>
      </GlassSurface>
    </div>
  );
}

function Health({ d }) {
  const s = summarize(d);
  const conn = { yes: ['● Connected', 'ok'], no: ['○ Disconnected', 'bad'], unknown: ['? Unknown', 'warn'] }[s.connected];
  return (
    <>
      <div className="rhs-pills">
        <Pill k="connection" label="Robinhood" value={conn[0]} tone={conn[1]} />
        <Pill
          k="sync"
          label="Last sync"
          value={s.last ? dateTime(s.last.iso) : 'never'}
          sub={`${num(s.ms.length)} module${s.ms.length === 1 ? '' : 's'}`}
          title={s.last ? String(s.last.iso) : 'no module has run yet'}
        />
        <Pill
          k="backfill"
          label="Backfill"
          value="not reported"
          sub={`${num(s.rows)} rows${s.truncated ? ` · ${num(s.truncated)} truncated` : ''}`}
          title="/api/rh/status has no backfill-progress field yet (K6); rows = sum of module row counts"
        />
        <Pill k="rate" label="Rate-limit hits" value={num(s.rate)} tone={s.rate == null || s.rate > 0 ? 'warn' : ''} />
        <Pill
          k="xcheck"
          label="Xcheck mismatches"
          value={s.xcheck > 0 ? `⚠ ${num(s.xcheck)}` : num(s.xcheck)}
          tone={s.xcheck > 0 ? 'warn' : ''}
          title="price-cache cross-check mismatches (sum over modules)"
        />
      </div>
      {s.ms.length ? (
        <ul className="rhs-mods" aria-label="Robinhood data modules">
          {s.ms.slice(0, MOD_CAP).map((m, i) => {
            const errs = Array.isArray(m.errors) ? m.errors.map(String) : [];
            return (
              <li key={`${m.name}-${i}`} className={errs.length ? 'has-err' : ''}>
                <b>{String(m.name == null ? DASH : m.name)}</b>
                <span className={m.enabled ? 'rhs-on' : 'rhs-off'}>{m.enabled ? 'on' : 'off'}</span>
                <span className="mui-n">{m.last_run ? dateTime(m.last_run) : 'never run'}</span>
                <span className="mui-n">{num(fin(m.rows, COUNT_MAX))} rows</span>
                {errs.length ? (
                  <span
                    className="rhs-err"
                    title={`${errs.slice(0, ERR_TITLE).map(e => e.slice(0, 100)).join('\n')}${errs.length > ERR_TITLE ? `\n${moreText(errs.length - ERR_TITLE, 'errors')}` : ''}`}
                  >
                    ! {errs[0]}
                    {errs.length > 1 ? ` (${moreText(errs.length - 1, 'errors')})` : ''}
                  </span>
                ) : null}
              </li>
            );
          })}
        </ul>
      ) : null}
      {s.ms.length > MOD_CAP ? <div className="mui-list-cap">{moreText(s.ms.length - MOD_CAP, 'modules')}</div> : null}
      {!s.ms.length ? (
        <div className="mui-state mui-state--empty">No modules reporting yet.</div>
      ) : null}
    </>
  );
}

export default function RhStatus() {
  const api = useRhApi('/api/rh/status', null, { pollMs: 30000 });
  return (
    <div className="rhs">
      <RhState api={api} empty={d => !d || typeof d !== 'object'} emptyText="No status yet.">
        {d => <Health d={d} />}
      </RhState>
    </div>
  );
}
