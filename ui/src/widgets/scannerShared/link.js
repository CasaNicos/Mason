// Scanner link channels (SCANNER_INTAKE_DESIGN §5: the TradingView/Bloomberg pattern).
// A channel is a color + a glyph + a letter, so it never relies on color alone. A list emits its
// current ticker on its channel through the standard bridge (`mason:chart-symbol`, chartId
// `scanlink-<id>`); every chart on that channel follows. Any number of lists can share a channel,
// so one chart can follow two lists. No module-level state: a chart that mounts late asks the
// lists on its channel to re-announce with a `mason:scanner-link-hello` window event.
// This folder has no index.jsx, so the registry never treats it as a widget type.
import { emitChartSymbol } from '../../lib/bridge.js';

// Colors (Nocturne tokens) live in scanner.css as .sc-link--<id>; validated with the dataviz
// validator (all-pairs CVD ≥ 11.5, normal-vision ≥ 15.3; see the F3 packet).
export const CHANNELS = Object.freeze([
  Object.freeze({ id: 'a', label: 'A', glyph: '◆' }),
  Object.freeze({ id: 'b', label: 'B', glyph: '✦' }),
  Object.freeze({ id: 'c', label: 'C', glyph: '●' }),
  Object.freeze({ id: 'd', label: 'D', glyph: '■' })
]);

/** The channel for an id; anything unknown falls back to A (configs are user-editable). */
export function channel(id) {
  return CHANNELS.find(c => c.id === id) || CHANNELS[0];
}

export const linkChartId = id => `scanlink-${channel(id).id}`;

export const HELLO_EVENT = 'mason:scanner-link-hello';

/** A chart asks the lists on its channel to announce their current ticker again. */
export function sayHello(id) {
  window.dispatchEvent(new CustomEvent(HELLO_EVENT, { detail: { link: channel(id).id } }));
}

/** fn() whenever a chart on channel `id` says hello. Returns unsubscribe. */
export function onHello(id, fn) {
  const want = channel(id).id;
  const h = e => {
    if (e && e.detail && e.detail.link === want) fn();
  };
  window.addEventListener(HELLO_EVENT, h);
  return () => window.removeEventListener(HELLO_EVENT, h);
}

/** A list announces its ticker on its channel. */
export function announce(id, ticker) {
  return emitChartSymbol(linkChartId(id), ticker);
}
