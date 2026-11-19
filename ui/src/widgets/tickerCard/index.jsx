// tickerCard — the ticker lookup popup's content (header ticker search, Ctrl+K ticker rows, mv.ticker.open('NVDA')).
// Mounted by static/v2/ticker-lookup.js with MasonUI.mount(host, 'tickerCard', props) inside that file's modal shell
// (backdrop, focus trap, Esc, focus return live there, not here). Not a dashboard widget (meta.catalog: false).
//
// Props:
//   symbol            the ticker to show; changing it reloads the card and the chart
//   onClose()         the × button
//   onAddChart(sym, point)  optional "Add chart" (the template passes mv.wm.addChart); no prop → no button
//   follow            false hides "Follow" (default: shown; POST /api/following, same as the Following widget)
//   titleId           id of the <h2> the dialog's aria-labelledby points at (default 'mv-tkc-title')
//
// Data: GET /api/ticker_card?symbol= (useTickerCard) and the price chart (MasonChartHost → window.MasonChart →
// /api/bars). They load independently: a failing card never blanks the chart and the other way round. Any null
// field prints "—"; a whole empty section says so in one quiet line (ETFs have no financials: normal, not an error).
// {ok:false} shows the server's own sentence. Every API string renders as JSX text (escaped).
//
// React Bits: DecryptedText (the symbol), Counter through rhAccounts' MoneyCounter (the price's digits roll),
// SpotlightCard (every section). Segmented.jsx (range / chart type) is our own: React Bits has no segmented control.
import { useEffect, useId, useMemo, useState } from 'react';
import MasonChartHost from '../../lib/MasonChartHost.jsx';
import { normTicker } from '../../lib/bridge.js';
import Segmented from './Segmented.jsx';
import { useFollow, useTickerCard } from './useTickerCard.js';
import { DEFAULT_RANGE, RANGES, rangeOf, sessionsView } from './ranges.js';
import { Analyst, CardHeader, DayStrip, Dates, Financials, Footer, KeyStats, Overview, Section } from './parts.jsx';
import { S } from './tkFmt.js';
import './tickerCard.css';

const PREF_KEY = 'mv_tk_card_v1';
const CHART_TYPES = [
  { key: 'trend', label: 'Line', title: 'Line chart' },
  { key: 'candle', label: 'Candles', title: 'Candlestick chart' }
];
const RANGE_OPTS = RANGES.map(r => ({ key: r.key, label: r.key, title: r.title }));
const ADD_CHART_RE = /^[A-Za-z.]{1,6}$/; // what mv.wm.addChart accepts

/** Upper-cased, trimmed, ≤ 15 chars (the server decides whether it is real). */
export function cleanSym(v) {
  return String(v == null ? '' : v)
    .replace(/\s+/g, '')
    .toUpperCase()
    .slice(0, 15);
}

/** The bars / display spelling of a class share before the card answers: Yahoo's "BRK-B" → "BRK.B". */
export function dotted(s) {
  return String(s || '').replace(/^([A-Z]{1,5})-([A-Z])$/, '$1.$2');
}

function readPrefs() {
  try {
    const p = JSON.parse(localStorage.getItem(PREF_KEY) || '{}');
    return {
      range: RANGES.some(r => r.key === p.range) ? p.range : DEFAULT_RANGE,
      type: CHART_TYPES.some(t => t.key === p.type) ? p.type : 'trend'
    };
  } catch {
    return { range: DEFAULT_RANGE, type: 'trend' };
  }
}
function writePrefs(p) {
  try {
    localStorage.setItem(PREF_KEY, JSON.stringify(p));
  } catch {
    /* private mode: the choice just isn't remembered */
  }
}

function FollowButton({ fol, sym }) {
  const on = fol.state === 'on';
  const busy = fol.state === 'busy';
  return (
    <button
      type="button"
      className={`mui-tk-btn${on ? ' is-on' : ''}`}
      onClick={fol.follow}
      disabled={on || busy || fol.state === 'unknown'}
      title={on ? `You follow ${sym} (Following widget)` : `Follow ${sym} in the Following widget`}
    >
      <i className={on ? 'ph ph-check' : 'ph ph-star'} aria-hidden="true" />
      {on ? 'Following' : busy ? 'Following…' : 'Follow'}
    </button>
  );
}

export default function TickerCard({ symbol, onClose, onAddChart, follow = true, titleId = 'mv-tkc-title' }) {
  const sym = cleanSym(symbol); // what was asked for (the type-ahead may say "BRK-B")
  const uid = useId().replace(/[^A-Za-z0-9_-]/g, '');
  const data = useTickerCard(sym);
  const card = data.card;
  // The card answers with the canonical spelling ("BRK.B": what /api/bars, the dashboard chart and the header use).
  // Until it does, Yahoo's "-X" class suffix is shown and charted as ".X".
  const shown = (card && cleanSym(card.symbol)) || dotted(sym);
  const fol = useFollow(shown, follow !== false);
  const [prefs, setPrefs] = useState(readPrefs);
  const range = rangeOf(prefs.range);
  const view = useMemo(() => sessionsView(range.sessions), [range.sessions]);
  const loading = data.status === 'loading';
  const isErr = data.status === 'error';
  const unknown = isErr && (data.code === 'bad_symbol' || data.code === 'not_found'); // no Follow / Add chart for those
  const chartSym = normTicker(shown) || '';

  useEffect(() => writePrefs(prefs), [prefs]);

  const canAdd = typeof onAddChart === 'function' && ADD_CHART_RE.test(shown);
  const actions = (
    <>
      {follow !== false && shown && !unknown ? <FollowButton fol={fol} sym={shown} /> : null}
      {canAdd && !unknown ? (
        <button
          type="button"
          className="mui-tk-btn"
          title={`Add a ${shown} chart to the dashboard`}
          aria-label={`Add a ${shown} chart to the dashboard`}
          onClick={e => {
            const r = e.currentTarget.getBoundingClientRect();
            onAddChart(shown, r.width ? { x: r.left + r.width / 2, y: r.top + r.height / 2 } : null);
          }}
        >
          <i className="ph ph-chart-line-up" aria-hidden="true" />
          Add chart
        </button>
      ) : null}
    </>
  );

  const fRaw = card && card.financials ? S(card.financials, 'periodType', 20) : null;
  const fType = fRaw ? fRaw.charAt(0).toUpperCase() + fRaw.slice(1) : null;
  const sid = k => `tk-${uid}-${k}`;

  return (
    <div className="mui-tk" data-symbol={sym} data-shown={shown} data-status={data.status}>
      <CardHeader
        sym={shown}
        card={card}
        status={data.status}
        refreshing={data.refreshing}
        failed={data.failed}
        onRefresh={data.refresh}
        onClose={onClose}
        titleId={titleId}
        actions={actions}
      />
      {fol.error ? (
        <p className="mui-tk-note mui-tk-note--err" role="alert">
          {fol.error}
        </p>
      ) : null}
      {isErr ? (
        <div className="mui-tk-err" role="alert">
          <i className="ph ph-warning-circle" aria-hidden="true" />
          <span className="mui-tk-err__txt">{data.error}</span>
          {data.code === 'server_error' || data.code === 'network' ? (
            <button type="button" className="mui-tk-btn" onClick={data.refresh}>
              Try again
            </button>
          ) : null}
        </div>
      ) : null}

      <div className={`mui-tk-body${isErr ? ' is-err' : ''}`}>
        <div className="mui-tk-main">
          <Section
            id={sid('chart')}
            title="Price"
            icon="ph ph-chart-line"
            className="mui-tk-cell--chart"
            aside={<Segmented options={RANGE_OPTS} value={range.key} onChange={k => setPrefs(p => ({ ...p, range: k }))} label="Chart range" />}
          >
            <div className="mui-tk-chart">
              <MasonChartHost
                chartId="ticker-card"
                symbol={chartSym}
                tf={range.tf}
                type={prefs.type}
                range={range.key}
                limit={range.limit}
                view={view}
                persist={false}
                follow={false}
              />
            </div>
            <div className="mui-tk-chartbar">
              <DayStrip quote={card ? card.quote : null} loading={loading} />
              <Segmented
                className="mui-tk-seg--sm"
                options={CHART_TYPES}
                value={prefs.type}
                onChange={k => setPrefs(p => ({ ...p, type: k }))}
                label="Chart type"
              />
            </div>
          </Section>
          {!isErr ? (
            <Section id={sid('stats')} title="Key stats" icon="ph ph-gauge" className="mui-tk-cell--stats" delay={0.04} busy={loading}>
              <KeyStats metrics={card ? card.metrics : null} quote={card ? card.quote : null} loading={loading} />
            </Section>
          ) : null}
          {!isErr ? (
            <Section
              id={sid('fin')}
              title="Financials"
              icon="ph ph-table"
              className="mui-tk-cell--fin"
              delay={0.08}
              busy={loading}
              aside={fType ? <span className="mui-badge mui-tk-chip">{fType}</span> : null}
            >
              <Financials financials={card ? card.financials : null} loading={loading} />
            </Section>
          ) : null}
        </div>
        {!isErr ? (
          <div className="mui-tk-side">
            <Section id={sid('about')} title="About" icon="ph ph-buildings" className="mui-tk-cell--about" delay={0.06} busy={loading}>
              <Overview profile={card ? card.profile : null} loading={loading} />
            </Section>
            <Section id={sid('dates')} title="Dates" icon="ph ph-calendar-blank" className="mui-tk-cell--dates" delay={0.1} busy={loading}>
              <Dates dates={card ? card.dates : null} loading={loading} />
            </Section>
            <Section id={sid('an')} title="Analysts" icon="ph ph-users-three" className="mui-tk-cell--an" delay={0.12} busy={loading}>
              <Analyst analyst={card ? card.analyst : null} quote={card ? card.quote : null} loading={loading} />
            </Section>
          </div>
        ) : null}
      </div>
      <Footer card={card} />
    </div>
  );
}
