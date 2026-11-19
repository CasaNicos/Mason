# Mason UI — React Bits bundle (`ui/` → `static/ui/`)

The binding spec is `docs/ui/REACT_BITS_BUNDLE_DESIGN.md`. This README covers **how to add a
widget** without editing anyone else's files. Built by RHX S3-F0.

```
cd ~/mason/ui
npm ci                 # once (Node >= 22.12)
npm test               # vitest (also runs Debugger tests in ../Tests/ui and ../Tests/debug)
npm run lint           # eslint: react/no-danger, no-eval, no-new-func are ERRORS
npm run build          # banned-construct grep → eslint → vite → size budgets  (writes ../static/ui/)
npm run build:check    # two clean builds must be byte-identical (add -- --strict to also match the existing static/ui)
npm run rb:diff        # React Bits provenance: copied once, header present, every change in MODS.md
python3 ../Tests/ui/test_rhx_harness.py   # Playwright against http://127.0.0.1:5050/static/ui/harness.html?rhx_mock=1
```
Builds of `static/ui/` run **one at a time** (the orchestrator runs them). Never commit (Nico does).

## 1. Add a widget: one folder, nothing else

```
ui/src/widgets/<type>/
  index.jsx      default export: the React component (lazy, its own chunk chunks/w-<type>-<hash>.js)
  meta.js        default export: catalog metadata (eager, so MasonUI.catalog() needs no chunk)
  *.css          optional; import it from index.jsx; scope every rule under `.mui-root`
```
`<type>` is the folder name and **must be the contracts §6.1 name** (`rhAccounts`, `congressFeed`, …).
Folders starting with `_` are internal (demos/tests) and never appear in `MasonUI.catalog()`.

### meta.js
```js
export default {
  title: 'CONGRESS FEED', icon: 'ph ph-bank', group: 'Feeds',     // groups: Robinhood | Feeds | System
  multi: true,                                                    // contracts §6.1
  size: { w: 12, h: 9, minW: 6, minH: 4 },                        // 36-column Gridstack units (WM §4.2)
  defaults: { ticker: '', heldOnly: false },                      // merged under inst.config
  desc: 'Latest congressional disclosures',
  settings: [{ key: 'heldOnly', label: 'Held only', type: 'bool' }],   // WM §4.3 shape
  routes: ['/api/rh/congress'],                                   // informational (harness, docs)
  pages: [{ page: 'congress', x: 12, y: 0, w: 24, h: 12 }],       // page layout, see §3
  // catalog: false  → mountable via MasonUI.mount() but not a dashboard catalog entry (e.g. wm-* UI)
};
```

**Rule:** `defaults` and every settings value must be a flat string, number or bool (WM §5.2 rejects
anything else); never `null`, arrays or objects. Use `''` for "none".

### index.jsx
```jsx
export default function CongressFeed({ config, saveConfig, instanceId, type }) { … }
```
- `config` = `{...meta.defaults, ...inst.config}`. `saveConfig(next)` replaces the instance config
  (WM persists it) and re-renders. **All per-instance state lives in the component** (two instances
  never share state; module-level `let` state is a bug).
- One React root per instance is created for you by `MasonUI.mountWidget(type, body, inst)`; it is
  wrapped in `<MotionConfig reducedMotion="user">`, an error boundary, and `<div class="mui-root mui-widget">`
  (fills the body; lay out with flex and `min-height: 0`; content must reflow on Gridstack resize).
- Strings render through JSX only. `dangerouslySetInnerHTML`, `eval`, `new Function` fail the build.

## 2. Data: `useRhApi` (never call `fetch` yourself)
```jsx
import { useRhApi, postRh } from '../../lib/api.js';
import { RhState } from '../../lib/states.jsx';

const api = useRhApi('/api/rh/congress', { days: 45 }, { pollMs: 15000 });
return <RhState api={api} empty={d => !d.trades.length} emptyText="No disclosures yet.">{d => …}</RhState>;
```
- Returns `{status: 'loading'|'ok'|'off'|'error', data, asOf, stale, errors, error, flag, mock, refresh}`.
- Unwraps `{ok, as_of, data, stale, errors}`. `{enabled:false}` → `status:'off'`, and `<RhState>` shows
  **"Off — enable `rh_expansion.<x>` (and the master switch `robinhood.enabled`)"** (flag from the route;
  `/api/scanners` → `scanners`).
- `stale` = `envelope.stale || errors.length || last poll failed` → amber `<StaleBadge>` (text, not color only).
- 10 s timeout. **One in-flight request + a 15 s cache per URL** (route + sorted params), shared by every
  instance: two widgets on one route make one request. No polling while `document.hidden`.
- `pollMs` is the real refresh period, also below the TTL (`pollMs: 15000` refetches every 15 s; cache
  age counts from request start). Instances on the same URL still share each tick's single request.
- A failed poll after a good one keeps the data (status stays `ok`, `stale:true`, `error` set).
- `postRh(route, body, {mockSlug})` → `{ok, data, error}`: JSON POST, 10 s timeout, no optimistic write.
  Show `error` inline.
- `MasonUI.mountWidget(...).refresh()` force-refetches every `useRhApi` in that widget.
- **S3-F6, identical polls cost nothing:** a poll whose `data`, `stale` and `errors` are unchanged (and whose
  `as_of` is on the same UTC date) keeps the previous state object, so the widget does **not** re-render, and
  `data` keeps its identity (structural sharing). So `asOf` is "data as of": within one day it changes only
  when the data does.
- **S3-F6, off screen = paused:** `MasonUI.mountWidget` gives each widget an on-screen store (an
  IntersectionObserver on the widget body, 200 px margin; `body[data-mui-onscreen]` mirrors it). While a widget
  is off screen (its page is hidden, it's scrolled away, or it's collapsed), `useRhApi` stops polling and
  `useMotionAllowed()` is false. On return it refetches once, TTL-guarded. The first load always runs.
  `useOnScreen()` (`lib/visibility.js`) reads it; outside `mountWidget` it is always true.

- `postRh` error text: envelope `errors[]` first; otherwise a non-envelope body's `message`, `reason` or
  `error` string (trimmed, ≤ 300 chars) — e.g. "Watchlist is full: 25/25" — and only then `HTTP <n>`.
- **Write actions:** wrap them in `useInflight()` (`lib/useInflight.js`): `const inflight = useInflight();`
  then `onClick={() => inflight.run(() => postRh(...))}` and `disabled={inflight.busy()}`. `run(fn, key?)`
  checks a ref synchronously, so duplicate clicks / `requestSubmit()` in the same task call `fn` once; it
  returns `undefined` for a skipped call, else `fn`'s promise, and clears the key in `finally`. Key it per
  item (`run(fn, hit.id)`) when several rows can write independently.

Shared states for custom layouts: `Loading`, `Off`, `ErrorState`, `Empty`, `StaleBadge` (`lib/states.jsx`).

## 3. Pages and the harness
The harness (`/static/ui/harness.html?rhx_mock=1`) has tabs **Congress, Crowd, Scanners, Real Money**,
any extra page a widget declares, and **Dashboard sim** (every type mounted twice). A widget shows on a
page tab when its `meta.pages` has `{page, x, y, w, h, id?, config?}` for it (36-col units; `id`
defaults to the type, set it when one type appears twice on a page). **Don't edit `harness.html` or
`harness/*.js` to place a widget.** Flags: `?rhx_mock=1` fixtures, `?nochart=1` no chart stub, `#<page>`.
The same `pages` data is what F6 uses for the real pages' default layouts.

Playwright hooks: `window.__hx = {pages, types, show(page), ids(page), remove(id), grids, handles}`;
`MasonUI.__debug.snapshot()` → `{roots, rootNames, intervals, timeouts, rafPending, rafCalls, instrumented}`.
Timer/rAF counting is on with `?rhx_mock=1`, `?rhx_debug=1`, or `window.MASONUI_DEBUG = true` before the
bundle loads. The counters live in `static/ui/debug-boot.js`, a **classic** script the harness loads before
the module (vendor chunks capture `requestAnimationFrame` when they evaluate, so only a script that runs
first sees everything). On any other page, add `<script src="/static/ui/debug-boot.js">` before
`mason-ui.js` to get complete counts; without it the bundle installs the counters late (misses vendor rAF). gsap's ticker idles ~60 frames after its last tween before sleeping: count leaks after
~1.5 s or call `gsap.ticker.sleep()` in vitest.

## 4. Mock fixtures (`?rhx_mock=1`)
`ui/fixtures/<slug>.json` is copied to `static/ui/fixtures/` by the build. It is the **full API response**
(the §6 envelope, or `{"enabled": false}`). **Slug rule:** drop the query string, drop a leading `/api/`,
lowercase, every run of non-`[a-z0-9]` becomes `_`, trim `_`:

| route | slug / file |
|---|---|
| `/api/rh/alerts/log?days=30` | `rh_alerts_log.json` |
| `/api/rh/history/stories` | `rh_history_stories.json` |
| `/api/kb/priors?family=pead` | `kb_priors.json` |
| `/api/scanners` | `scanners.json` |

Params are ignored in mock mode (one fixture per route). `postRh` in mock mode GETs
`<mockSlug or slug + "_post">.json` and never writes. Real S1/S2 fixtures land in
`Tests/fixtures/api/<slug>.json`; copy them into `ui/fixtures/` (same name) when `FIXTURES_READY` appears.
`routeSlug()` in `lib/api.js` is the implementation.

## 5. React Bits
- Shared set (bundle §7) lives in `src/reactbits/<Name>/`: AnimatedList, Counter, SpotlightCard, Stepper,
  Stack, GlassSurface, Dock, ElectricBorder, PixelTransition, StarBorder, LogoLoop, ClickSpark, CountUp,
  ShinyText, DecryptedText. Import them relatively: `import AnimatedList from '../../reactbits/AnimatedList/AnimatedList.jsx'`.
- Need another one? Check the folder first (never copy twice), copy JS+CSS from
  `~/gitpulls/react-bits/src/content/<Cat>/<Name>/` with the header
  `// React Bits: <Cat>/<Name> @ c49d6978d2496660f0f0c5a3b3ca77a059566a93`, log every change in
  `src/reactbits/MODS.md` as `<Name>/<File>` + a `MASON MOD <id>` comment, run `npm run rb:diff`.
- Their CSS is auto-scoped under `.mui-root` at build time; theme overrides live in `src/theme.css`.
- Mason additions you will want (see MODS.md): **AnimatedList** `renderItem`, `itemKey`, `maxItems` (200),
  keyboard on the focused list only, an optional controlled `selectedIndex` (numeric; the parent drives the
highlight, e.g. `MasonUI.mount(el,'AnimatedList',p).update({...p, selectedIndex})`, rows don't remount), and the cap is never silent: past `maxItems` the list ends with
  "`200 of N shown`" (`capLabel(shown, total)` to reword, `capLabel={false}` to hide) and calls
  `onCap({shown, total})` so a feed can say "200 of N" in its header; **ClickSpark** `burst`/`onIdle`; **PixelTransition** `mode`.
  **Counter** cannot draw a sign or `$`: pass `Math.abs(v)` and render sign/▲▼ next to it.
  **CountUp / Counter** render "—" for non-finite values or |v| > 1e15 (no NaN/∞), and CountUp settles at its
  `duration` (default 2 s; pass ≤ 1 for snappy counts). The AnimatedList listbox has a `:focus-visible` ring
  in theme.css: don't add local copies.
- Motion budget per page: at most **two continuously animating effects** and one ElectricBorder.
  Continuous loops must stop when hidden or under reduced motion: use `useMotionAllowed()` /
  `useDocumentVisible()` from `lib/motion.js` / `lib/visibility.js`.

## 6. Formatting (`lib/fmt.js`)
`money(v, {signed, compact})` (sign before `$`, cents always), `pct(v, {digits, signed})` (signed by
default), `arrow(v)` ▲/▼, `dirClass(v)` → `mui-up|mui-down|mui-flat`, `signed`, `num`, `date`, `dateTime`,
`lagDays`, `nLabel(n)` → `n=12`. All return `—` for null/NaN/junk and never print `-0`.
**Direction is never color alone:** always pair `dirClass` with `arrow()` or a sign. Every statistic shows its `n=`.

## 7. Charts and cross-widget events
- Candles: `<MasonChartHost chartId="congress-main" symbol={t} tf="1d" />` wraps `window.MasonChart`
  (the template's engine) and follows `symbol`/`tf`/`type`. Use a **stable, unique `chartId`** (MasonChart
  persists state per id). If MasonChart is absent it renders a placeholder. In the harness a labelled
  stub stands in (`harness/masonchart-stub.js`; calls logged in `window.__chartCalls`).
  **MasonChart has no marker API** (e.g. disclosure dates): list them under the chart instead.
- `lib/bridge.js`:
  - `emitChartSymbol(chartId, ticker)` → `mason:chart-symbol {chartId, ticker}`; `<MasonChartHost chartId>`
    follows events for its id. `onChartSymbol(fn, {chartId})`, `useChartSymbol(chartId, initial)`.
  - `emitGo(page, focus)` → `mason:go {page, focus}` (the template router opens the page). `onGo(fn, {replay})`:
    with `{replay:true}` a subscriber also receives the last go event if it is ≤ `GO_REPLAY_MS` (10 s) old, once,
    on a microtask. Use it in page widgets: the go event is usually what mounts the page, so they subscribe
    after it fired (scannerList keeps the hit focus pending until its data loads; congress page widgets take a
    ticker focus as their filter/chart pick). `lastGo()` returns the last one seen.
  - chartId `'main'` (rhExposure tiles, rhAlertsLog fires): on #realmoney the page chart (`realmoneyChart`)
    follows it; on the dashboard the template sends it to the topmost `chart` widget.
  - Tickers are normalized (upper-case, ≤ 32 chars, `[A-Z0-9 .-/^]`); junk is dropped.

## 8. Tests
- Your widget tests go in `ui/test/<type>.test.jsx` (vitest + @testing-library/react, jsdom).
  `test/setup.js` gives you `window.__test.setReducedMotion(on)` and `window.__test.setHidden(on)`.
- The Debugger's component tests can live outside `ui/`: vitest also runs
  `../Tests/ui/**/*.test.{js,jsx}` and `../Tests/debug/**/*.test.{js,jsx}`; bare imports (`react`,
  `@testing-library/react`, `motion`, `gsap`) resolve from `ui/node_modules` via `resolve.dedupe`.
  Import UI code relatively, e.g. `../../ui/src/lib/api.js`.

## 9. Template wiring (F6 only)
```html
<link rel="stylesheet" href="/static/ui/mason-ui.css">
<script>window.MasonUI = window.MasonUI || { ready: new Promise(function (r) { window.addEventListener('masonui:ready', function () { r(); }, { once: true }); }) };</script>
<script type="module" src="/static/ui/mason-ui.js"></script>
```
The pre-shim lets vanilla code `await MasonUI.ready` before the module runs; the entry keeps that promise.
`mountWidget`/`mount` keep **one root per element**: mounting again on the same body destroys the previous
root first, and every `destroy()`/`unmount()` is idempotent. Effects accept real `Element`s only, and
`electrify` stops by itself once its node leaves the DOM. `electrify(node, {fadeMs})`: fade default 400 ms,
clamped to [0, 1000] (150 under reduced motion); the returned `stop()` fades, `stop(true)` removes at once (even
mid-fade), and a new `electrify()` removes any border still fading, so only one is ever in the DOM.
Catalog `mount(body, inst)` → `return MasonUI.mountWidget(type, body, inst)`; metadata → `MasonUI.catalog()`.

### 9.1 What F6 wired (template `templates/mason_v2.html`, all behind `rh_expansion.ui.enabled`)
The template renders the F6 blocks only when the Jinja variable `rhx_ui_enabled` is true (contract for the
app: docs/robinhood/expansion/S3/HANDOFF_F6.md §5). Flag off, the page is byte-identical to the pre-F6 one.
- `WIDGET_CATALOG` gets one entry per `MasonUI.catalog()` type (the 12 contracts §6.1 types) once the bundle is
  ready; `mount` → `MasonUI.mountWidget(type, body, {id: iid, config, saveConfig})`; config saves go to the
  dashboard doc. `meta.size.minH` is enforced by Gridstack (collapse still shrinks to the header).
- Pages `#realmoney #scanners #congress #crowd`: one MasonGrid each (`mv_gs3_<page>`), default layout = every
  widget's `meta.pages` (36-col units, no migration), page widget config in localStorage `mv_rhx_cfg_v1`.
- `mason:go` → `mv.go(page)` for the 4 pages and the dashboard. Hidden RH page charts (`mui-chart-*`) skip the
  15 s `MasonChart.refreshAll` and refresh when their page is shown again.
