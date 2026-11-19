// Data for the ticker card: GET /api/ticker_card?symbol=<S> (top-level fields, not the RH envelope, so not useRhApi).
//  - One request per symbol (and per refresh()). A symbol change drops the old request: its answer is aborted and
//    ignored, so a slow reply for the previous ticker can never paint over the new one.
//  - 12 s timeout. {ok:true} → 'ok' with the cleaned card (tkFmt.cleanCard). {ok:false, error, code} → 'error' with
//    the server's own sentence (shown as text). No JSON / no network / timeout → 'error' with our own sentence.
//  - refresh() keeps the card on screen while it reloads (refreshing: true); a failed refresh keeps the old card
//    and sets `failed` (with the reason) instead of blanking it.
//  - Mock mode (?rhx_mock=1, the harness) reads <static/ui>/fixtures/ticker_card.json.
// Also here: useFollow(symbol) — the "Follow" action, the same POST /api/following the Following widget makes.
import { useCallback, useEffect, useRef, useState } from 'react';
import { fixtureUrl, isMockMode, postRh } from '../../lib/api.js';
import { cleanCard, text } from './tkFmt.js';

export const ROUTE = '/api/ticker_card';
const TIMEOUT_MS = 12000;

const NET_ERROR = 'Couldn’t reach Mason for this card. Check the connection and try again.';
const TIMEOUT_ERROR = 'Mason took too long to answer for this card. Try again in a bit.';
const odd = status => `Mason sent back something unexpected for this card (HTTP ${status}).`;

/** One request → {kind:'ok', card} | {kind:'error', error, code}. Never throws. */
export async function fetchCard(symbol, signal) {
  const url = isMockMode() ? fixtureUrl('ticker_card') : `${ROUTE}?symbol=${encodeURIComponent(symbol)}`;
  let res;
  try {
    res = await fetch(url, { credentials: 'same-origin', headers: { Accept: 'application/json' }, signal });
  } catch (e) {
    return { kind: 'error', error: e && e.name === 'AbortError' ? TIMEOUT_ERROR : NET_ERROR, code: 'network' };
  }
  let body = null;
  try {
    body = await res.json();
  } catch {
    body = null;
  }
  if (body && typeof body === 'object' && !Array.isArray(body)) {
    if (body.ok === true && res.ok !== false) return { kind: 'ok', card: cleanCard(body) };
    if (body.ok === false) {
      return {
        kind: 'error',
        error: text(body.error, 300) || odd(res.status),
        code: typeof body.code === 'string' ? body.code.slice(0, 40) : 'server_error'
      };
    }
  }
  return { kind: 'error', error: odd(res.status || 0), code: 'server_error' };
}

const LOADING = { status: 'loading', card: null, error: null, code: null, refreshing: false, failed: null };

/** → {status: 'loading'|'ok'|'error', card, error, code, refreshing, failed, refresh()} for `symbol`. */
export function useTickerCard(symbol) {
  const [state, setState] = useState(LOADING);
  const [nonce, setNonce] = useState(0);
  const lastSym = useRef(null);

  useEffect(() => {
    if (!symbol) {
      setState({ ...LOADING, status: 'error', error: 'That doesn’t look like a ticker symbol.', code: 'bad_symbol' });
      return undefined;
    }
    const same = lastSym.current === symbol;
    lastSym.current = symbol;
    setState(s => (same && s.status === 'ok' ? { ...s, refreshing: true, failed: null } : LOADING));
    const ctl = typeof AbortController !== 'undefined' ? new AbortController() : null;
    let alive = true;
    const timer = setTimeout(() => ctl && ctl.abort(), TIMEOUT_MS);
    fetchCard(symbol, ctl ? ctl.signal : undefined).then(r => {
      clearTimeout(timer);
      if (!alive) return;
      if (r.kind === 'ok') setState({ status: 'ok', card: r.card, error: null, code: null, refreshing: false, failed: null });
      else
        setState(s =>
          same && s.status === 'ok' && s.card
            ? { ...s, refreshing: false, failed: r.error }
            : { ...LOADING, status: 'error', error: r.error, code: r.code }
        );
    });
    return () => {
      alive = false;
      clearTimeout(timer);
      if (ctl) ctl.abort();
    };
  }, [symbol, nonce]);

  const refresh = useCallback(() => setNonce(n => n + 1), []);
  return { ...state, refresh };
}

/**
 * Follow state + action for `symbol`: GET /api/following ({tickers:[...]}) once per symbol, POST {ticker} to follow
 * (the route validates the ticker against the live feed and answers 400 {error} when it doesn't resolve).
 * → {state: 'unknown'|'idle'|'on'|'busy', error, follow()}
 */
export function useFollow(symbol, enabled = true) {
  const [st, setSt] = useState({ state: 'unknown', error: null });
  const busy = useRef(false);
  const cur = useRef(symbol);
  cur.current = symbol;
  useEffect(() => {
    if (!enabled || !symbol) return undefined;
    let alive = true;
    setSt({ state: 'unknown', error: null });
    const url = isMockMode() ? fixtureUrl('following') : '/api/following';
    fetch(url, { credentials: 'same-origin', headers: { Accept: 'application/json' } })
      .then(r => r.json())
      .then(d => {
        if (!alive) return;
        const list = d && Array.isArray(d.tickers) ? d.tickers.map(t => String(t).toUpperCase()) : null;
        setSt({ state: list && list.includes(symbol) ? 'on' : 'idle', error: null });
      })
      .catch(() => alive && setSt({ state: 'idle', error: null }));
    return () => {
      alive = false;
    };
  }, [symbol, enabled]);

  const follow = useCallback(async () => {
    if (!symbol || busy.current) return;
    busy.current = true;
    setSt({ state: 'busy', error: null });
    const r = await postRh('/api/following', { ticker: symbol });
    busy.current = false;
    if (cur.current !== symbol) return; // the card moved on to another ticker meanwhile
    setSt(r.ok ? { state: 'on', error: null } : { state: 'idle', error: text(r.error, 200) || 'Couldn’t follow it just now.' });
  }, [symbol]);

  return { ...st, follow };
}
