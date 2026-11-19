// scannerFeed — "Scanner Hits" catalog entry (contracts §6.1; SC §5 compact version).
// One line per hit this session: ticker · scan · first seen · qwen score, newest first, as a React
// Bits AnimatedList keyed by hit id (a 15 s poll updates rows in place; only new hits animate).
// Click / Enter → mason:go {page:'scanners', focus:hitId}. {scan_id} ('' = all scans) is chosen by id.
import { useCallback, useMemo } from 'react';
import AnimatedList from '../../reactbits/AnimatedList/AnimatedList.jsx';
import { useRhApi } from '../../lib/api.js';
import { RhState, StaleBadge } from '../../lib/states.jsx';
import { arrow, dirClass } from '../../lib/fmt.js';
import { emitGo } from '../../lib/bridge.js';
import { SCANNERS_ROUTE, POLL_MS, scansOf, hhmm, cellText, qwenView, isDisconnected, statusOf } from '../scannerShared/scan.js';
import '../scannerShared/scanner.css';
import './scannerFeed.css';

const t = iso => {
  const v = Date.parse(iso);
  return Number.isFinite(v) ? v : 0;
};

function FeedRow({ hit }) {
  const q = qwenView(hit.triage);
  return (
    <div className={`scf-row${hit.active === false ? ' is-exited' : ''}`} data-hit-id={String(hit.id)} data-first-seen={cellText(hit.first_seen)}>
      <b className="scf-tk">{cellText(hit.ticker)}</b>
      <span className="scf-scan" title={hit._scan}>
        {hit._scan}
      </span>
      <span className="scf-seen" title={`First seen ${cellText(hit.first_seen)}`}>
        {hhmm(hit.first_seen)}
      </span>
      <span className={`sc-qwen sc-qwen--${q.state}`} title={q.title}>
        <span className="scf-q-l">qwen </span>
        <b>{q.score}</b>
        {q.word ? <span className={dirClass(q.dir)}>{` ${arrow(q.dir) || '–'}`}</span> : null}
      </span>
      {hit.active === false || hit.advice ? (
        <span className="scf-tags">
          {hit.active === false ? <span className="sc-exit">exited {hhmm(hit.exited_at)}</span> : null}
          {hit.advice ? (
            <span className="scf-own" title={cellText(hit.advice)}>
              you own · advice only
            </span>
          ) : null}
        </span>
      ) : null}
    </div>
  );
}

export default function ScannerFeed({ config, saveConfig }) {
  const api = useRhApi(SCANNERS_ROUTE, null, { pollMs: POLL_MS });
  const data = api.status === 'ok' ? api.data : null;
  const scans = useMemo(() => scansOf(data), [data]);
  const want = typeof config.scan_id === 'string' ? config.scan_id : '';
  const hits = useMemo(() => {
    const out = [];
    for (const s of scans) {
      if (want && s.scan_id !== want) continue;
      for (const h of s.hits) out.push({ ...h, _scan: cellText(h.title || s.title) });
    }
    // newest first; stable, so ties keep Robinhood's scan order
    return out.map((h, i) => [h, i]).sort((a, b) => t(b[0].first_seen) - t(a[0].first_seen) || a[1] - b[1]).map(x => x[0]);
  }, [scans, want]);
  const onSelect = useCallback(h => {
    if (h && h.id != null) emitGo('scanners', String(h.id));
  }, []);

  if (api.status !== 'ok') {
    return (
      <div className="scf">
        <RhState api={api}>{() => null}</RhState>
      </div>
    );
  }
  const status = statusOf(data);
  const active = hits.filter(h => h.active !== false).length;
  return (
    <div className="scf">
      <div className="sc-bar">
        <select className="sc-scan-pick" aria-label="Scan filter" value={want} onChange={e => saveConfig({ ...config, scan_id: e.target.value })}>
          <option value="">All scans</option>
          {scans.map(s => (
            <option key={s.scan_id} value={s.scan_id}>
              {s.title || s.scan_id}
            </option>
          ))}
        </select>
        <span className="sc-count">
          {hits.length} hits · {active} active
        </span>
        <StaleBadge stale={api.stale} errors={api.errors} error={api.error} asOf={api.asOf} />
      </div>
      {isDisconnected(status) ? (
        <div className="sc-reconnect" role="status">
          <span aria-hidden="true">⚠</span> Robinhood: reconnect — last poll {hhmm(status.last_poll)}
        </div>
      ) : null}
      {hits.length ? (
        <div className="scf-list">
          <AnimatedList
            items={hits}
            itemKey={h => String(h.id)}
            onItemSelect={onSelect}
            ariaLabel="Scanner hits — Enter opens the hit on the Scanners page"
            renderItem={h => <FeedRow hit={h} />}
          />
        </div>
      ) : (
        <div className="mui-state mui-state--empty">{want ? 'No hits for this scan this session.' : 'No hits yet this session.'}</div>
      )}
    </div>
  );
}
