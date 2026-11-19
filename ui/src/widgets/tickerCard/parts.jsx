// tickerCard sections. Every API string is rendered as JSX text (escaped); numbers go through tkFmt (null → "—").
// Direction is never color alone: ▲/▼ or a sign always sits next to the up/down color (lib/fmt arrow/dirClass).
import { useLayoutEffect, useRef, useState } from 'react';
import { motion } from 'motion/react';
import SpotlightCard from '../../reactbits/SpotlightCard/SpotlightCard.jsx';
import DecryptedText from '../../reactbits/DecryptedText/DecryptedText.jsx';
import MoneyCounter from '../rhAccounts/MoneyCounter.jsx';
import { arrow, dirClass } from '../../lib/fmt.js';
import * as F from './tkFmt.js';

const SPOT = 'rgba(74, 158, 255, 0.13)';
const DASH = F.DASH;

export function Skel({ lines = 3, className = '' }) {
  return (
    <div className={`mui-tk-skel ${className}`} aria-hidden="true">
      {Array.from({ length: lines }, (_, i) => (
        <div key={i} className="mui-skel" style={{ width: `${88 - ((i * 17) % 46)}%` }} />
      ))}
    </div>
  );
}

export function Quiet({ children }) {
  return <p className="mui-tk-quiet">{children}</p>;
}

/** A card section: React Bits SpotlightCard, a heading, a fade/rise entrance (reduced motion: none, MotionConfig). */
export function Section({ id, title, icon, aside, className = '', delay = 0, busy = false, children }) {
  return (
    <motion.section
      className={`mui-tk-cell ${className}`}
      aria-labelledby={id}
      aria-busy={busy ? 'true' : undefined}
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.28, delay, ease: [0.22, 1, 0.36, 1] }}
    >
      <SpotlightCard className="mui-tk-sec" spotlightColor={SPOT}>
        <div className="mui-tk-sec__head">
          <h3 id={id} className="mui-tk-sec__title">
            {icon ? <i className={icon} aria-hidden="true" /> : null}
            {title}
          </h3>
          {aside ? <div className="mui-tk-sec__aside">{aside}</div> : null}
        </div>
        {children}
      </SpotlightCard>
    </motion.section>
  );
}

// ── header ───────────────────────────────────────────────────────────────────────────────────────────────
export function CardHeader({ sym, card, status, refreshing, failed, onRefresh, onClose, titleId, actions }) {
  const p = card ? card.profile : null;
  const q = card ? card.quote : null;
  const loading = status === 'loading';
  const name = F.S(p, 'name', 120);
  const exch = F.S(p, 'exchange', 32);
  const px = F.N(q, 'price');
  const ch = F.N(q, 'change');
  const cp = F.N(q, 'change_pct');
  const dir = cp != null ? cp : ch;
  const qTime = F.asOf(q && typeof q.ts === 'string' ? q.ts : card ? card.asOf : null);
  const qSrc = F.S(q, 'source', 24);
  return (
    <header className="mui-tk-head">
      <div className="mui-tk-id">
        <h2 id={titleId} className="mui-tk-sym">
          <span className="mui-sr">{sym}</span>
          <span aria-hidden="true">
            <DecryptedText
              key={sym}
              text={sym || DASH}
              animateOn="view"
              sequential
              revealDirection="start"
              speed={38}
              characters="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789$#%"
              className="mui-tk-sym__on"
              encryptedClassName="mui-tk-sym__enc"
            />
          </span>
          {name ? <span className="mui-sr">{`, ${name}`}</span> : null}
        </h2>
        <div className="mui-tk-name">
          {loading ? (
            <span className="mui-skel mui-tk-name__skel" aria-hidden="true" />
          ) : (
            <span className="mui-tk-name__txt" title={name || undefined}>
              {name || (status === 'error' ? 'No company details' : DASH)}
            </span>
          )}
          {exch ? <span className="mui-badge mui-tk-chip">{exch}</span> : null}
        </div>
      </div>

      {status === 'error' && !card ? (
        <div className="mui-tk-quote" />
      ) : (
      <div className="mui-tk-quote" aria-live="polite">
        {loading ? (
          <>
            <span className="mui-skel mui-tk-px__skel" aria-hidden="true" />
            <span className="mui-sr">Loading the quote…</span>
          </>
        ) : (
          <>
            <div className="mui-tk-px">
              {px != null && Math.abs(px) >= 1 && Math.abs(px) < 1e9 ? <MoneyCounter value={px} fontSize={28} /> : F.price(px)}
            </div>
            {ch == null && cp == null ? (
              <div className="mui-tk-chg mui-flat" title="No change data for today">
                {DASH}
              </div>
            ) : (
              <div className={`mui-tk-chg ${dirClass(dir)}`}>
                {arrow(dir) ? <span aria-hidden="true">{arrow(dir)} </span> : null}
                <span>{F.change(ch)}</span> <span>({F.pctSigned(cp)})</span>
                <span className="mui-tk-chg__lbl"> today</span>
              </div>
            )}
          </>
        )}
        <div className="mui-tk-asof">
          {qTime ? <span>as of {qTime}</span> : null}
          {qSrc ? <span className="mui-tk-asof__src">{qSrc}</span> : null}
          {failed ? (
            <span className="mui-badge mui-badge--warn" title={failed}>
              ! refresh failed
            </span>
          ) : null}
          {status !== 'loading' ? (
            <button
              type="button"
              className={`mui-tk-iconbtn mui-tk-refresh${refreshing ? ' is-spin' : ''}`}
              onClick={onRefresh}
              aria-label="Refresh the card"
              title="Refresh"
              disabled={refreshing}
            >
              <i className="ph ph-arrow-clockwise" aria-hidden="true" />
            </button>
          ) : null}
        </div>
      </div>
      )}

      <div className="mui-tk-actions">{actions}</div>
      <button type="button" className="mui-tk-iconbtn mui-tk-close" onClick={onClose} aria-label="Close the ticker card" title="Close (Esc)">
        <i className="ph ph-x" aria-hidden="true" />
      </button>
    </header>
  );
}

// ── chart's day strip: open / high / low / prev close (today's quote) ────────────────────────────────────
export function DayStrip({ quote, loading }) {
  const cells = [
    ['Open', F.price(F.N(quote, 'open'))],
    ['High', F.price(F.N(quote, 'day_high'))],
    ['Low', F.price(F.N(quote, 'day_low'))],
    ['Prev close', F.price(F.N(quote, 'prev_close'))]
  ];
  return (
    <dl className="mui-tk-day">
      {cells.map(([k, v]) => (
        <div key={k} className="mui-tk-day__c">
          <dt>{k}</dt>
          <dd>{loading ? <span className="mui-skel mui-tk-day__skel" aria-hidden="true" /> : v}</dd>
        </div>
      ))}
    </dl>
  );
}

// ── key stats ────────────────────────────────────────────────────────────────────────────────────────────
const METRIC_KEYS = ['market_cap', 'pe_ratio', 'forward_pe', 'eps_ttm', 'dividend_yield', 'beta', 'high_52w', 'low_52w', 'avg_volume', 'shares_outstanding', 'float', 'short_pct_float'];

function Stat({ label, value, sub, wide = false, title }) {
  return (
    <div className={`mui-tk-stat${wide ? ' is-wide' : ''}`} title={title || undefined}>
      <dt className="mui-tk-stat__k">{label}</dt>
      <dd className="mui-tk-stat__v">{value}</dd>
      {sub ? <dd className="mui-tk-stat__s">{sub}</dd> : null}
    </div>
  );
}

function RangeBar({ lo, hi, at, loLabel, hiLabel, label }) {
  const pos = F.rangePos(lo, hi, at);
  return (
    <div className="mui-tk-range" role="img" aria-label={label}>
      <span className="mui-tk-range__end">{loLabel}</span>
      <span className="mui-tk-range__track" aria-hidden="true">
        <span className="mui-tk-range__fill" style={{ width: pos == null ? '0%' : `${(pos * 100).toFixed(1)}%` }} />
        {pos != null ? <span className="mui-tk-range__dot" style={{ left: `${(pos * 100).toFixed(1)}%` }} /> : null}
      </span>
      <span className="mui-tk-range__end">{hiLabel}</span>
    </div>
  );
}

export function KeyStats({ metrics, quote, loading }) {
  if (loading) {
    return (
      <dl className="mui-tk-stats" aria-hidden="true">
        {Array.from({ length: 12 }, (_, i) => (
          <div key={i} className={`mui-tk-stat${i === 4 ? ' is-wide' : ''}`}>
            <span className="mui-skel mui-tk-stat__skel1" />
            <span className="mui-skel mui-tk-stat__skel2" />
          </div>
        ))}
      </dl>
    );
  }
  const m = metrics;
  const px = F.N(quote, 'price');
  if (!m || F.allNull(m, METRIC_KEYS)) return <Quiet>No key stats on file for this one.</Quiet>;
  const lo = F.N(m, 'low_52w');
  const hi = F.N(m, 'high_52w');
  const pos = F.rangePos(lo, hi, px);
  let rangeSub = null;
  if (pos != null && hi) {
    const off = ((px - hi) / hi) * 100;
    rangeSub = off >= -0.05 ? 'at the 52-week high' : `${F.pctPlain(Math.abs(off), 1)} below the high`;
  }
  const vol = F.N(quote, 'volume');
  const avg = F.N(m, 'avg_volume');
  const ratio = vol != null && avg ? vol / avg : null;
  return (
    <dl className="mui-tk-stats">
      <Stat label="Market cap" value={F.compactMoney(F.N(m, 'market_cap'))} />
      <Stat label="P/E (TTM)" value={F.ratio(F.N(m, 'pe_ratio'))} />
      <Stat label="Forward P/E" value={F.ratio(F.N(m, 'forward_pe'))} />
      <Stat label="EPS (TTM)" value={F.perShare(F.N(m, 'eps_ttm'))} />
      <Stat
        wide
        label="52-week range"
        value={
          <RangeBar
            lo={lo}
            hi={hi}
            at={px}
            loLabel={F.price(lo)}
            hiLabel={F.price(hi)}
            label={`52-week low ${F.price(lo)}, high ${F.price(hi)}, now ${F.price(px)}`}
          />
        }
        sub={rangeSub}
      />
      <Stat
        label="Volume"
        value={F.compact(vol)}
        sub={ratio != null ? `${ratio.toFixed(2)}× avg (${F.compact(avg)})` : avg != null ? `avg ${F.compact(avg)}` : null}
      />
      <Stat label="Dividend yield" value={F.pctPlain(F.N(m, 'dividend_yield'))} />
      <Stat label="Beta" value={F.ratio(F.N(m, 'beta'))} />
      <Stat label="Shares out" value={F.compact(F.N(m, 'shares_outstanding'))} />
      <Stat label="Float" value={F.compact(F.N(m, 'float'))} />
      <Stat label="Short % of float" value={F.pctPlain(F.N(m, 'short_pct_float'))} />
    </dl>
  );
}

// ── overview ─────────────────────────────────────────────────────────────────────────────────────────────
function Clamp({ children, lines = 4 }) {
  const ref = useRef(null);
  const [open, setOpen] = useState(false);
  const [over, setOver] = useState(false);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el || open) return;
    setOver(el.scrollHeight > el.clientHeight + 2);
  }, [children, open]);
  return (
    <div className="mui-tk-clamp">
      <p ref={ref} className={`mui-tk-desc${open ? ' is-open' : ''}`} style={{ '--tk-lines': lines }}>
        {children}
      </p>
      {over || open ? (
        <button type="button" className="mui-tk-more" aria-expanded={open} onClick={() => setOpen(o => !o)}>
          {open ? 'less' : 'more'}
        </button>
      ) : null}
    </div>
  );
}

export function Overview({ profile, loading }) {
  if (loading) return <Skel lines={6} />;
  const p = profile;
  const sector = F.S(p, 'sector', 60);
  const industry = F.S(p, 'industry', 80);
  const desc = F.S(p, 'description', 2400);
  const site = F.safeUrl(p ? p.website : null);
  const facts = [
    ['CEO', F.S(p, 'ceo', 80)],
    ['Employees', F.N(p, 'employees') != null ? F.count(F.N(p, 'employees')) : null],
    ['Headquarters', F.S(p, 'hq', 80)],
    ['Founded', F.S(p, 'founded', 20)]
  ].filter(f => f[1]);
  if (!sector && !industry && !desc && !site && !facts.length) return <Quiet>No company profile on file.</Quiet>;
  return (
    <>
      {sector || industry ? (
        <div className="mui-tk-chips">
          {sector ? <span className="mui-badge mui-tk-chip mui-tk-chip--acc">{sector}</span> : null}
          {industry ? <span className="mui-badge mui-tk-chip">{industry}</span> : null}
        </div>
      ) : null}
      {desc ? <Clamp>{desc}</Clamp> : null}
      {facts.length || site ? (
        <dl className="mui-tk-facts">
          {facts.map(([k, v]) => (
            <div key={k} className="mui-tk-fact">
              <dt>{k}</dt>
              <dd>{v}</dd>
            </div>
          ))}
          {site ? (
            <div className="mui-tk-fact">
              <dt>Website</dt>
              <dd>
                <a href={site.href} target="_blank" rel="noopener noreferrer">
                  {site.label}
                  <i className="ph ph-arrow-square-out" aria-hidden="true" />
                </a>
              </dd>
            </div>
          ) : null}
        </dl>
      ) : null}
    </>
  );
}

// ── dates + analyst ──────────────────────────────────────────────────────────────────────────────────────
function DateRow({ label, value, rel, extra }) {
  return (
    <div className="mui-tk-date">
      <dt>{label}</dt>
      <dd>
        <span className="mui-tk-date__v">{value}</span>
        {rel ? <span className="mui-tk-date__rel">{rel}</span> : null}
        {extra}
      </dd>
    </div>
  );
}

export function Dates({ dates, loading }) {
  if (loading) return <Skel lines={4} />;
  const d = dates;
  const next = d ? d.next : null;
  const last = d ? d.last : null;
  const nextDate = next && typeof next.date === 'string' ? next.date : null;
  const lastDate = last && typeof last.date === 'string' ? last.date : null;
  const any = nextDate || lastDate || (d && (d.exDividend || d.dividendPay)) || F.N(last, 'eps_actual') != null;
  if (!d || !any) {
    return <Quiet>No earnings or dividend dates on file for this one.</Quiet>;
  }
  const t = next ? F.timing(next.timing) : null;
  const conf = next ? next.confirmed : null;
  const actual = F.N(last, 'eps_actual');
  const est = F.N(last, 'eps_estimate');
  const surprise = F.N(last, 'surprise_pct');
  return (
    <dl className="mui-tk-dates">
      <DateRow
        label="Next earnings"
        value={F.day(nextDate)}
        rel={F.relDay(nextDate)}
        extra={
          nextDate ? (
            <span className="mui-tk-date__x">
              {t ? <span>{t}</span> : <span className="mui-muted">time not set</span>}
              {conf === true ? <span className="mui-badge mui-tk-chip mui-tk-chip--ok">confirmed</span> : null}
              {conf === false ? <span className="mui-badge mui-tk-chip">estimated</span> : null}
            </span>
          ) : null
        }
      />
      <DateRow
        label="Last earnings"
        value={F.day(lastDate)}
        rel={F.relDay(lastDate)}
        extra={
          actual != null || est != null ? (
            <span className="mui-tk-date__x">
              <span>
                EPS {F.perShare(actual)} vs {F.perShare(est)} est.
              </span>
              {surprise != null ? (
                <span className={`mui-tk-surprise ${dirClass(surprise)}`}>
                  {arrow(surprise) ? <span aria-hidden="true">{arrow(surprise)} </span> : null}
                  {F.pctSigned(surprise, 1)} {surprise > 0 ? 'beat' : surprise < 0 ? 'miss' : 'in line'}
                </span>
              ) : null}
            </span>
          ) : null
        }
      />
      <DateRow label="Ex-dividend" value={F.day(d.exDividend)} rel={F.relDay(d.exDividend)} />
      <DateRow label="Dividend paid" value={F.day(d.dividendPay)} rel={F.relDay(d.dividendPay)} />
    </dl>
  );
}

export function Analyst({ analyst, quote, loading }) {
  if (loading) return <Skel lines={3} />;
  const a = analyst;
  const buy = F.N(a, 'buy');
  const hold = F.N(a, 'hold');
  const sell = F.N(a, 'sell');
  const tMean = F.N(a, 'target_mean');
  const tHi = F.N(a, 'target_high');
  const tLo = F.N(a, 'target_low');
  if (!a || F.allNull(a, ['buy', 'hold', 'sell', 'target_mean', 'target_high', 'target_low'])) {
    return <Quiet>No analyst ratings on file.</Quiet>;
  }
  const px = F.N(quote, 'price');
  const total = (buy || 0) + (hold || 0) + (sell || 0);
  const w = v => (total > 0 && v ? `${((v / total) * 100).toFixed(1)}%` : '0%');
  const up = tMean != null && px ? ((tMean - px) / px) * 100 : null;
  return (
    <div className="mui-tk-an">
      {total > 0 ? (
        <>
          <div className="mui-tk-an__bar" role="img" aria-label={`${buy || 0} buy, ${hold || 0} hold, ${sell || 0} sell`}>
            <span className="is-buy" style={{ width: w(buy) }} />
            <span className="is-hold" style={{ width: w(hold) }} />
            <span className="is-sell" style={{ width: w(sell) }} />
          </div>
          <div className="mui-tk-an__counts" aria-hidden="true">
            <span className="mui-up">{F.count(buy || 0)} buy</span>
            <span className="mui-muted">{F.count(hold || 0)} hold</span>
            <span className="mui-down">{F.count(sell || 0)} sell</span>
          </div>
        </>
      ) : null}
      {tMean != null || tHi != null || tLo != null ? (
        <div className="mui-tk-an__tgt">
          <div className="mui-tk-an__tgtrow">
            <span>
              Target <b>{F.price(tMean)}</b>
            </span>
            {up != null ? (
              <span className={dirClass(up)}>
                {arrow(up) ? <span aria-hidden="true">{arrow(up)} </span> : null}
                {F.pctSigned(up, 1)} vs now
              </span>
            ) : null}
          </div>
          <RangeBar lo={tLo} hi={tHi} at={px} loLabel={F.price(tLo)} hiLabel={F.price(tHi)} label={`Price targets from ${F.price(tLo)} to ${F.price(tHi)}, now ${F.price(px)}`} />
        </div>
      ) : null}
    </div>
  );
}

// ── financials ───────────────────────────────────────────────────────────────────────────────────────────
const FIN_ROWS = [
  ['Revenue', 'revenue', F.compactMoney],
  ['Gross profit', 'gross_profit', F.compactMoney],
  ['Operating income', 'operating_income', F.compactMoney],
  ['Net income', 'net_income', F.compactMoney],
  ['EPS', 'eps', F.perShare],
  ['Free cash flow', 'free_cash_flow', F.compactMoney]
];

export function Financials({ financials, loading }) {
  if (loading) return <Skel lines={5} />;
  const f = financials;
  const periods = f ? f.periods.slice(0, 5) : [];
  if (!periods.length) return <Quiet>No financials on file for this one.</Quiet>;
  return (
    <div className="mui-tk-finwrap" tabIndex={0} role="region" aria-label="Financials table (scrolls sideways on small screens)">
      <table className="mui-tk-fin">
        <thead>
          <tr>
            <th scope="col" className="mui-tk-fin__corner">
              <span className="mui-sr">Line item</span>
            </th>
            {periods.map((p, i) => (
              <th key={i} scope="col">
                <span className="mui-tk-fin__p">{F.S(p, 'period', 24) || DASH}</span>
                <span className="mui-tk-fin__d">{typeof p.end_date === 'string' ? F.day(p.end_date) : ''}</span>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {FIN_ROWS.map(([label, key, fmt]) => (
            <tr key={key}>
              <th scope="row">{label}</th>
              {periods.map((p, i) => {
                const v = F.N(p, key);
                return (
                  <td key={i} className={v != null && v < 0 ? 'mui-down' : undefined}>
                    {fmt(v)}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ── footer: data sources + as of ─────────────────────────────────────────────────────────────────────────
export function Footer({ card }) {
  if (!card) return null;
  const groups = F.sourceGroups(card.sources);
  const when = F.asOf(card.asOf);
  const miss = card.missing.length;
  return (
    <footer className="mui-tk-foot">
      <span className="mui-tk-foot__src">
        data:{' '}
        {groups.length
          ? groups.map((g, i) => (
              <span key={g.source}>
                {i ? ' · ' : ''}
                <b>{g.source}</b> ({g.parts.join(', ')})
              </span>
            ))
          : 'sources not given'}
      </span>
      {when ? <span>as of {when}</span> : null}
      {miss ? (
        <span className="mui-tk-foot__miss" title={card.missing.join(', ')}>
          {miss} field{miss === 1 ? '' : 's'} not available
        </span>
      ) : null}
    </footer>
  );
}
