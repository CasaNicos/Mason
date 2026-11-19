// scannerList — one Robinhood scan as a live table (SCANNER_INTAKE_DESIGN §5; page #scanners only).
//
//  - Columns come straight from columns_json (scan.columns) in Robinhood's order, then Mason's:
//    First seen · qwen (note + why-now in the tooltip) · Watch · Action (Propose / Promote).
//  - Rows are a React Bits AnimatedList keyed by hit id: a 15 s poll updates rows in place and
//    only new hits play the entrance animation.
//  - Link channel (glyph + letter + color): the current row (clicked, Enter, mason:go focus, or the
//    top active hit by default) is announced on the channel; charts on that channel follow.
//  - SC-D5 "You own X" advice: a distinct full-width line labeled "advice only", text only.
//  - Actions (SC-D2: a suggestion is never a watch, never a trade):
//      Propose on an unwatched hit → POST /api/watchlist/<t>/propose {source:'scanner', scan_id, hit_id}
//        (S3 K2, finalization T-D8D9): Nico's suggestion, not watched until promoted; after the
//        refresh the row reads "proposed" and offers Promote.
//      Promote on a proposed hit → POST /api/watchlist/<t>/promote (existing route).
//    One write per hit at a time (synchronous guard); the server's message/reason shows inline as text.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import AnimatedList from '../../reactbits/AnimatedList/AnimatedList.jsx';
import { useRhApi, postRh } from '../../lib/api.js';
import { RhState, StaleBadge } from '../../lib/states.jsx';
import { arrow, dirClass } from '../../lib/fmt.js';
import { normTicker, onGo } from '../../lib/bridge.js';
import { prefersReducedMotion } from '../../lib/motion.js';
import LinkPicker from '../scannerShared/LinkPicker.jsx';
import { announce, channel, onHello } from '../scannerShared/link.js';
import {
  SCANNERS_ROUTE,
  POLL_MS,
  scansOf,
  resolveScan,
  hhmm,
  cellText,
  changeValue,
  qwenView,
  watchWord,
  isDisconnected,
  statusOf,
  errText
} from '../scannerShared/scan.js';
import '../scannerShared/scanner.css';
import './scannerList.css';

// rem widths: ticker, each RH column, then first seen · qwen · watch · action
const W = { tk: 6, col: 3.6, seen: 3, qwen: 4.6, watch: 4.2, act: 4.6 };

function ActButton({ hit, state, onPromote, onPropose }) {
  // SC-D5 (SCANNER_INTAKE_DESIGN §9): a hit on a ticker Nico holds is advice to Nico only. Its row
  // carries the "advice only" note and no controls at all (round 2).
  if (hit.advice) return null;
  const t = normTicker(hit.ticker);
  const busy = !!(state && state.busy);
  if (hit.watch_status === 'proposed') {
    if (state && state.ok && state.kind === 'promote') return null;
    return (
      <button
        type="button"
        className="sc-act"
        disabled={!t || busy}
        title={t ? `Promote ${t} from suggestion to your watchlist (POST /api/watchlist/${t}/promote)` : 'Unreadable ticker'}
        onClick={e => {
          e.stopPropagation();
          onPromote(hit);
        }}
      >
        {busy ? 'Promoting…' : 'Promote'}
      </button>
    );
  }
  if (hit.watch_status == null || hit.watch_status === '') {
    if (state && state.ok && state.kind === 'propose') return null; // done; the refresh brings Promote
    const ids = hit.id != null && hit.id !== '' && typeof hit.scan_id === 'string' && hit.scan_id !== '';
    return (
      <button
        type="button"
        className="sc-act"
        disabled={!t || !ids || busy}
        title={
          t && ids
            ? `Suggest ${t} for your watchlist — not watched until you promote it (POST /api/watchlist/${t}/propose)`
            : 'Unreadable hit'
        }
        onClick={e => {
          e.stopPropagation();
          onPropose(hit);
        }}
      >
        {busy && state.kind === 'propose' ? 'Proposing…' : 'Propose'}
      </button>
    );
  }
  return null;
}

// AnimatedList handles Enter on its listbox (and preventDefaults it). A native listener on this
// always-rendered cell keeps Enter/Space for the button inside it (it fires before the list's).
function ActCell(props) {
  const ref = useRef(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return undefined;
    const stop = e => {
      if ((e.key === 'Enter' || e.key === ' ') && e.target && e.target.tagName === 'BUTTON') e.stopPropagation();
    };
    el.addEventListener('keydown', stop);
    return () => el.removeEventListener('keydown', stop);
  }, []);
  return (
    <span ref={ref} className="sc-c sc-actc">
      <ActButton {...props} />
    </span>
  );
}

// Compact mode shows the Robinhood cells as wrapping label/value chips; past this many, "+N more".
const MAX_CHIPS = 12;

function CellValue({ col, v }) {
  const n = changeValue(col, v);
  return n != null ? <span className={dirClass(n)}>{`${arrow(n)} ${cellText(v, col)}`}</span> : cellText(v, col);
}

function Row({ hit, cols, tpl, charted, glyph, act, onPromote, onPropose, compact }) {
  const q = qwenView(hit.triage);
  const cells = hit.cells && typeof hit.cells === 'object' ? hit.cells : {};
  return (
    <div
      className={`sc-row${compact ? ' sc-row--compact' : ''}${hit.active === false ? ' is-exited' : ''}${charted ? ' is-charted' : ''}`}
      style={compact ? undefined : { gridTemplateColumns: tpl }}
      data-hit-id={String(hit.id)}
    >
      <span className="sc-c sc-tk">
        {charted ? (
          <span className="sc-on" title="Shown on the linked chart" aria-label="charted">
            {glyph}
          </span>
        ) : null}
        <b>{cellText(hit.ticker)}</b>
        {hit.active === false ? (
          <span className="sc-exit" title={`Exited the scan at ${hhmm(hit.exited_at)}`}>
            exited
          </span>
        ) : null}
        {Number(hit.reentries) > 0 ? (
          <span className="sc-re" title={`Re-entered the scan ${Number(hit.reentries)}× this session`}>
            ↺{Number(hit.reentries)}
          </span>
        ) : null}
      </span>
      {compact ? null : cols.map(c => (
        <span key={c} className="sc-c sc-cell">
          <CellValue col={c} v={cells[c]} />
        </span>
      ))}
      {compact ? null : (
        <span className="sc-c sc-seen" title={`First seen ${cellText(hit.first_seen)}`}>
          {hhmm(hit.first_seen)}
        </span>
      )}
      <span className={`sc-c sc-qwen sc-qwen--${q.state}`} title={q.title}>
        <b>{q.score}</b>
        {q.word ? (
          <span className={dirClass(q.dir)}>
            {` ${arrow(q.dir) || '–'}`}
            <span className="sc-qword">{` ${q.word}`}</span>
          </span>
        ) : null}
      </span>
      <span className={`sc-c sc-watch sc-watch--${hit.watch_status || 'none'}`}>{watchWord(hit.watch_status)}</span>
      <ActCell hit={hit} state={act} onPromote={onPromote} onPropose={onPropose} />
      {compact ? (
        <div className="sc-chips">
          <span className="sc-chip">
            <i>First seen</i> {hhmm(hit.first_seen)}
          </span>
          {cols.slice(0, MAX_CHIPS).map(c => (
            <span key={c} className="sc-chip">
              <i>{c}</i> <CellValue col={c} v={cells[c]} />
            </span>
          ))}
          {cols.length > MAX_CHIPS ? (
            <span className="sc-chip sc-chip--more" title={cols.slice(MAX_CHIPS).map(c => `${c}: ${cellText(cells[c], c)}`).join(' · ')}>
              +{cols.length - MAX_CHIPS} more
            </span>
          ) : null}
        </div>
      ) : null}
      {act && act.text ? (
        <div className={`sc-actmsg${act.ok ? ' is-ok' : ' is-err'}`} role="status">
          <span aria-hidden="true">{act.ok ? '✓' : '!'}</span> {act.text}
        </div>
      ) : null}
      {hit.advice ? (
        <div className="sc-advice" role="note">
          <span className="sc-advice-tag">advice only</span>
          <span className="sc-advice-text">{cellText(hit.advice)}</span>
        </div>
      ) : null}
    </div>
  );
}

export default function ScannerList({ config, saveConfig }) {
  const api = useRhApi(SCANNERS_ROUTE, null, { pollMs: POLL_MS });
  const ch = channel(config.link);
  const link = ch.id;
  const data = api.status === 'ok' ? api.data : null;
  const scans = useMemo(() => scansOf(data), [data]);
  const { scan, missing } = resolveScan(scans, config.scan_id, config.slot);
  const hits = scan ? scan.hits : [];
  const cols = scan ? scan.columns : [];
  const status = statusOf(data);

  // Narrower than the full table → compact rows (ticker + Mason columns on line 1, RH cells as
  // wrapping chips on line 2): every value stays visible at the widget's minimum size, no h-scroll.
  const [rootEl, setRootEl] = useState(null);
  const [width, setWidth] = useState(null);
  useEffect(() => {
    if (!rootEl || typeof ResizeObserver === 'undefined') return undefined;
    const ro = new ResizeObserver(() => setWidth(rootEl.clientWidth));
    ro.observe(rootEl);
    setWidth(rootEl.clientWidth || null);
    return () => ro.disconnect();
  }, [rootEl]);

  const [cursorId, setCursorId] = useState(null);
  const [pickId, setPickId] = useState(null);
  const [acts, setActs] = useState({}); // hit id → {kind, busy} | {kind, ok, text}; kind 'promote' | 'propose'
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const pick = hits.find(h => h.id === pickId) || null;
  const top = hits.find(h => h.active !== false) || hits[0] || null;
  const currentHit = pick || top;
  const current = currentHit ? normTicker(currentHit.ticker) : null;

  // announce the current ticker on the link channel; re-announce when a chart says hello
  useEffect(() => {
    if (current) announce(link, current);
  }, [current, link]);
  const currentRef = useRef(current);
  currentRef.current = current;
  useEffect(
    () =>
      onHello(link, () => {
        if (currentRef.current) announce(link, currentRef.current);
      }),
    [link]
  );

  // #scanners opened from a scannerFeed row: mason:go {page:'scanners', focus:hitId}. S3-F6: the event
  // usually mounts this page, so it is replayed to late subscribers (bridge onGo {replay}) and kept as
  // a pending focus until this list's data has loaded; a hit this list does not carry is dropped.
  const [goFocus, setGoFocus] = useState(null);
  useEffect(
    () =>
      onGo(
        ({ page, focus }) => {
          if (page === 'scanners' && focus) setGoFocus({ id: String(focus) });
        },
        { replay: true }
      ),
    []
  );
  const loadedOk = api.status === 'ok';
  useEffect(() => {
    if (!goFocus || !loadedOk) return;
    const h = hits.find(x => String(x.id) === goFocus.id);
    setGoFocus(null);
    if (!h) return;
    setPickId(h.id);
    setCursorId(h.id);
    if (rootEl && typeof rootEl.scrollIntoView === 'function') {
      try {
        rootEl.scrollIntoView({ block: 'nearest', behavior: prefersReducedMotion() ? 'auto' : 'smooth' });
      } catch {
        /* old browsers */
      }
    }
  }, [goFocus, loadedOk, hits, rootEl]);

  const cursorIdx = hits.findIndex(h => h.id === (cursorId != null ? cursorId : currentHit && currentHit.id));
  const selectedIndex = cursorIdx >= 0 ? cursorIdx : currentHit ? hits.indexOf(currentHit) : -1;

  const onSelect = useCallback(h => {
    if (!h) return;
    setCursorId(h.id);
    setPickId(h.id);
  }, []);

  const onKey = e => {
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
    if (!e.target || e.target.getAttribute('role') !== 'listbox' || !hits.length) return;
    const from = selectedIndex < 0 ? -1 : selectedIndex;
    const j = Math.max(0, Math.min(hits.length - 1, from + (e.key === 'ArrowDown' ? 1 : -1)));
    setCursorId(hits[j].id);
  };

  const refresh = api.refresh;
  // DEBUG_F3 F3-1: a synchronous per-hit in-flight guard. The disabled button only exists after a
  // re-render, so several clicks in one task (scripted / assistive input) must be stopped here.
  const inflight = useRef(new Set());
  const onPromote = useCallback(
    async hit => {
      const t = normTicker(hit.ticker);
      if (!t || inflight.current.has(hit.id)) return;
      inflight.current.add(hit.id);
      try {
        setActs(a => ({ ...a, [hit.id]: { kind: 'promote', busy: true } }));
        const r = await postRh(`/api/watchlist/${encodeURIComponent(t)}/promote`, {}, { mockSlug: 'scanners_promote_post' });
        if (!alive.current) return;
        setActs(a => ({
          ...a,
          [hit.id]: r.ok
            ? { kind: 'promote', ok: true, text: `Promoted — watching ${t}` }
            : { kind: 'promote', ok: false, text: `Couldn’t promote ${t}: ${r.error || 'unknown error'}` }
        }));
        if (r.ok) refresh();
      } finally {
        inflight.current.delete(hit.id);
      }
    },
    [refresh]
  );
  // Propose (K2): same per-hit guard as Promote, so the two can never race on one row.
  const onPropose = useCallback(
    async hit => {
      const t = normTicker(hit.ticker);
      if (!t || inflight.current.has(hit.id)) return;
      inflight.current.add(hit.id);
      try {
        setActs(a => ({ ...a, [hit.id]: { kind: 'propose', busy: true } }));
        const r = await postRh(
          `/api/watchlist/${encodeURIComponent(t)}/propose`,
          { source: 'scanner', scan_id: hit.scan_id, hit_id: String(hit.id) },
          { mockSlug: 'scanners_propose_post' }
        );
        if (!alive.current) return;
        setActs(a => ({
          ...a,
          [hit.id]: r.ok
            ? { kind: 'propose', ok: true, text: r.message || `Proposed ${t} — a suggestion, not watched until you promote it` }
            : { kind: 'propose', ok: false, text: r.error && /^Couldn[’']t propose/.test(r.error) ? r.error : `Couldn’t propose ${t}: ${r.error || 'unknown error'}` }
        }));
        if (r.ok) refresh();
      } finally {
        inflight.current.delete(hit.id);
      }
    },
    [refresh]
  );

  if (api.status !== 'ok') {
    return (
      <div className={`sc-list sc-link--${link}`}>
        <RhState api={api}>{() => null}</RhState>
      </div>
    );
  }

  const tpl = [`minmax(${W.tk}rem, 0.9fr)`, cols.length ? `repeat(${cols.length}, minmax(${W.col}rem, 1fr))` : '', `${W.seen}rem ${W.qwen}rem ${W.watch}rem ${W.act}rem`]
    .filter(Boolean)
    .join(' ');
  const minW = W.tk + cols.length * W.col + W.seen + W.qwen + W.watch + W.act + 1.5;
  const rem = typeof window !== 'undefined' ? parseFloat(window.getComputedStyle(document.documentElement).fontSize) || 16 : 16;
  const compact = width != null && width > 0 && width < minW * rem;
  const nActive = hits.filter(h => h.active !== false).length;
  const sort = scan && scan.sorting && typeof scan.sorting.column === 'string' ? scan.sorting : null;

  return (
    <div ref={setRootEl} className={`sc-list sc-link--${link}${compact ? ' is-compact' : ''}`}>
      <div className="sc-bar">
        <LinkPicker value={link} what="list" onChange={id => saveConfig({ ...config, link: id })} />
        <select
          className="sc-scan-pick"
          aria-label="Scan shown in this list"
          title={scan ? [scan.title, sort ? `sorted by ${sort.column} ${sort.direction === 'asc' ? '↑' : '↓'}` : '', scan.last_run ? `run ${hhmm(scan.last_run)}` : ''].filter(Boolean).join(' · ') : 'Pick a scan'}
          value={scan ? scan.scan_id : ''}
          onChange={e => saveConfig({ ...config, scan_id: e.target.value })}
        >
          {!scan ? (
            <option value="" disabled>
              Pick a scan…
            </option>
          ) : null}
          {scans.map(s => (
            <option key={s.scan_id} value={s.scan_id}>
              {s.title || s.scan_id}
            </option>
          ))}
        </select>
        {sort ? (
          <span className="sc-sort" title="Robinhood sort order">
            {sort.column} {sort.direction === 'asc' ? '↑' : '↓'}
          </span>
        ) : null}
        {scan ? (
          <span className="sc-count">
            {nActive} active · {hits.length - nActive} exited
          </span>
        ) : null}
        {scan && scan.last_run ? <span className="sc-run">run {hhmm(scan.last_run)}</span> : null}
        <StaleBadge stale={api.stale} errors={api.errors} error={api.error} asOf={api.asOf} />
      </div>
      {isDisconnected(status) ? (
        <div className="sc-reconnect" role="status">
          <span aria-hidden="true">⚠</span> Robinhood: reconnect — scanner polling is paused
          {status.message ? ` (${errText(status.message)})` : ''}; last poll {hhmm(status.last_poll)}.
        </div>
      ) : null}
      {scan && scan.last_error ? (
        <div className="sc-reconnect" role="status">
          <span aria-hidden="true">⚠</span> Last run failed: {errText(scan.last_error)}
        </div>
      ) : null}
      {missing ? (
        <div className="mui-state mui-state--empty">
          Scan <code className="mui-code">{missing.slice(0, 8)}</code> isn’t in Robinhood’s scan list anymore — pick one above.
        </div>
      ) : !scan ? (
        <div className="mui-state mui-state--empty">No scan in this slot yet.</div>
      ) : !hits.length ? (
        <div className="mui-state mui-state--empty">No hits yet this session.</div>
      ) : (
        <div className={`sc-table${compact ? ' is-compact' : ''}`} onKeyDown={onKey}>
          <div className="sc-inner" style={compact ? undefined : { minWidth: `${minW}rem` }}>
            {compact ? null : (
            <div className="sc-row sc-head" style={{ gridTemplateColumns: tpl }} aria-hidden="true">
              <span className="sc-c">Ticker</span>
              {cols.map(c => (
                <span key={c} className="sc-c" title={c}>
                  {c}
                </span>
              ))}
              <span className="sc-c">First seen</span>
              <span className="sc-c" title="qwen triage score 0–1 and direction; hover a score for the note">
                qwen
              </span>
              <span className="sc-c">Watch</span>
              <span className="sc-c">Action</span>
            </div>
            )}
            <AnimatedList
              items={hits}
              itemKey={h => String(h.id)}
              selectedIndex={selectedIndex}
              onItemSelect={onSelect}
              ariaLabel={`${scan.title || 'Scan'} hits — Enter charts the row on link ${ch.label}`}
              className="sc-alist"
              renderItem={h => (
                <Row
                  hit={h}
                  cols={cols}
                  tpl={tpl}
                  charted={currentHit != null && h.id === currentHit.id}
                  glyph={ch.glyph}
                  act={acts[h.id]}
                  onPromote={onPromote}
                  onPropose={onPropose}
                  compact={compact}
                />
              )}
            />
          </div>
        </div>
      )}
    </div>
  );
}
