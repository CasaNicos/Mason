# React Bits — modification log

Every file in `ui/src/reactbits/` is copied from Nico's pull of React Bits,
`~/gitpulls/react-bits/src/content/<Cat>/<Name>/`, at git sha
**`c49d6978d2496660f0f0c5a3b3ca77a059566a93`** (bundle design §5). Each component is copied
**once** (check this folder before copying; `npm run rb:diff` fails on duplicates).

`npm run rb:diff` diffs every file against upstream (ignoring the header line) and fails when a
modified file is not named below as `<Name>/<File>`. In code, every changed spot carries a
`MASON MOD <id>` comment that matches an id below.

## Global (all files)

| id | What | Why |
|---|---|---|
| M-ALL-1 | Line 1 of every copied file is the provenance header `// React Bits: <Cat>/<Name> @ <sha>` (`/* … */` in CSS). | Bundle design §5 provenance rule. Not counted as a modification by `rb:diff`. |
| M-ALL-2 | **Build-time CSS scoping, no file change:** `vite.config.js` (`scopeReactBitsCss`) prefixes every selector in `src/reactbits/**/*.css` with `:where(.mui-root)`. | Upstream CSS uses global generic class names (`.item`, `.card`, `.bottom-gradient`, `.outer-container`, `.back-button`, `.inner-content`…) that would restyle the vanilla template. `:where()` adds no specificity, so upstream rules behave exactly as before inside a MasonUI root and `theme.css` overrides still win. |
| M-ALL-3 | **Theming, no file change:** colors come from Nocturne tokens through `src/theme.css` overrides and component props. | Bundle design §5 "Theming". |

## Per file

### AnimatedList/AnimatedList.jsx
- **M-AnimatedList-1** — new optional props `renderItem(item, index, selected)`, `itemKey(item, index)`,
  `maxItems` (default **200**), `ariaLabel`. `items` is sliced to `maxItems`. Without `renderItem` the
  upstream `<p className="item-text">{item}</p>` is rendered unchanged.
  *Why:* the feed widgets need rich rows and stable keys (index keys re-animate every row when a
  new item is prepended), and an uncapped list mounts one IntersectionObserver per row (10k rows
  froze the page — S3 §4 Debugger check).
- **M-AnimatedList-2** — keyboard handling moved from `window` to the list element (which gets
  `tabIndex=0`, `role="listbox"`, `aria-label`; rows get `role="option"` + `aria-selected`), and Tab
  is no longer intercepted (only ArrowUp/ArrowDown/Enter).
  *Why:* upstream listens on `window`, so every list on the dashboard reacted to every arrow key,
  and swallowing Tab trapped keyboard users inside the list (mason-frontend accessibility rule).

- **M-AnimatedList-3** (round 2) — the cap is never silent: when `items.length > maxItems` the list ends
  with a `.mui-list-cap` note "`200 of N shown`" (override with `capLabel(shown, total)`, hide with
  `capLabel={false}`), and `onCap({shown, total})` lets a feed show it in its own header.
  (Round 3: the note sits right after the listbox, not inside it, and uses thousands separators,
  e.g. "200 of 10,000 shown".)
- **M-AnimatedList-4** (round 3) — optional controlled `selectedIndex` prop (a number). When given it
  overrides the internal hover/arrow state, the scroll-into-view effect follows it, and rows are not
  re-keyed, so `MasonUI.mount(...).update({selectedIndex})` moves the highlight without replaying the
  entrance animation. *Why:* WM §7.1 — the palette owns arrow-key handling (`mv.cmdHi`) and drives the
  picker's highlight; upstream only has `initialSelectedIndex`.

- **M-AnimatedList-5** (calendar widget, 2026-11-15) — optional `isSeparator(item, index)` prop. A row it
  returns true for is a heading inside the list: its wrapper gets `data-sep="1"` and
  `role="presentation"` (no `role="option"`, no `aria-selected`), it has no hover / click selection, the
  arrow keys skip it, and its content is wrapped in `.item-sep` instead of the `.item` card. Without the
  prop nothing changes (same arrow-key maths as before).
  *Why:* the calendar agenda groups events by day with a sticky day header inside the one scrolling
  list; upstream has no notion of a non-selectable row, so a header would be announced and selected as
  an event.

- **M-AnimatedList-6** (ticker lookup, 2026-11-18) — two optional props: `listId` (an `id` on the
  `role="listbox"` element) and `optionId(item, index)` (an `id` on each `role="option"` row; separator
  rows get none). Without them nothing changes.
  *Why:* the header ticker search is an ARIA combobox: focus stays in the text input, whose
  `aria-controls` must name the listbox and whose `aria-activedescendant` must name the highlighted
  option. Upstream rows and list have no ids to point at.

### ClickSpark/ClickSpark.jsx
- **M-ClickSpark-1** — the rAF loop runs only while sparks are alive: it starts on a click/burst and
  stops itself when the last spark expires (upstream ran `requestAnimationFrame` forever, even idle).
  *Why:* a permanent loop per mounted ClickSpark; the Debugger asserts no rAF activity when idle or hidden.
- **M-ClickSpark-2** — new props `burst={x, y}` (fire programmatically; used by
  `MasonUI.effects.spark`) and `onIdle()` (called when the last spark finishes). Click and burst
  share `addSparks()`, which does nothing while `document.hidden`.

### CountUp/CountUp.jsx
- **M-CountUp-1** — `prefers-reduced-motion`: jump straight to the final value (no spring, no timers)
  and call `onEnd`. *Why:* bundle design §5 motion rule; `MotionConfig reducedMotion="user"` does not
  reach `useSpring` driven text.

- **M-CountUp-2** (round 2) — when the displayed text already equals the target's text, `jump()` the
  spring to the target. *Why:* upstream's overdamped spring keeps motion's frame loop running (~60 rAF/s)
  for seconds after the number visibly settled (DEBUG_F0); this ends the loop as soon as it reads final. Also, when the
  document becomes hidden mid-count, jump to the target so no spring keeps motion's loop alive while hidden.

- **M-CountUp-3** (F0b) — (guard) `to` that is non-finite or |v| > 1e15 renders "—" and never animates;
  a bad `from` starts at `to`. Never prints NaN / ∞ / 1e+308 (DEBUG_F1 F1-4, DEBUG_F2 F2-4).
  (settle) the spring is critically damped with ω = 9.23 / duration (within 0.1% of the target at
  `duration`, same ease-out shape) instead of upstream's heavily overdamped 20+40/d / 100/d, with
  `restDelta` at display precision and a hard settle (jump + final text + onEnd) at delay + duration.
  2-decimal values used to crawl 3–6 s (F1 OQ5); now they settle at `duration`.

### Counter/Counter.jsx
- **M-Counter-1** — `prefers-reduced-motion`: digits `jump()` to the new value instead of `set()`
  (spring). Same reason as M-CountUp-1.
- **M-Counter-2** (F0b) — a non-finite value or |v| > 1e15 renders "—" (upstream would build digit
  columns from "NaN"/"Infinity"). The file's own `Number` component shadows the global, so the guard uses
  the global `isFinite()`.
- Note (no change): `Counter` derives `places` from `value.toString()`, so it cannot render a sign or a
  `$`. Widgets pass `Math.abs(value)` and render the sign / ▲▼ / `$` beside it.

### DecryptedText/DecryptedText.jsx
- **M-DecryptedText-1** — `prefers-reduced-motion`: when an animation would start, settle immediately to
  the final text (forward) or the scrambled text (reverse); no interval is created.

### ElectricBorder/ElectricBorder.jsx
- **M-ElectricBorder-1** — the noise loop runs only while the document is visible and reduced motion is
  off (`useMotionAllowed()`); otherwise exactly one static frame is drawn and no rAF is scheduled. The
  loop is capped at **30 fps** (like MasonFX), the first frame no longer computes a giant delta from
  t=0, and the cleanup resets its refs. *Why:* bundle §5 (pause when hidden, reduced motion) and the
  GPU budget next to CRT Warp (WM §7.2).

### LogoLoop/LogoLoop.jsx
- **M-LogoLoop-1** — `useAnimationLoop` takes a `motionOk` flag (`useMotionAllowed()`): no rAF loop while
  hidden or under reduced motion; the strip stays at its current offset (static).

### PixelTransition/PixelTransition.jsx
- **M-PixelTransition-1 — one-way adaptation (WM §7.4).** New props: `mode` = `'hover'` (upstream
  behavior, default) | `'cover'` (on mount, pixels switch **on** in random order over
  `animationStepDuration`, then `onDone()`) | `'uncover'` (starts fully covered, pixels switch **off** in
  random order, then `onDone()` — used for undo, "removal rewinding"); `rows` (grid rows, so pixels stay
  square on a non-square widget; default `gridSize`); `accentColor` + `accentShare` (that share of
  pixels uses the accent color, ~15% `#4a9eff` per WM). In cover/uncover mode the hover/focus/click
  handlers are inert and the card is not focusable. The random-stagger core (`gsap … stagger:{each,
  from:'random'}`) is the component's own.
- **M-PixelTransition-2** — the grid is cleared with `replaceChildren()` instead of `innerHTML = ''`
  (same effect; keeps `innerHTML` out of the bundle source).
- **M-PixelTransition-3** — on unmount, kill pending pixel tweens and the delayed call (upstream left
  them running against detached nodes).

### ShinyText/ShinyText.jsx
- **M-ShinyText-1** — the shine step is driven by our own `requestAnimationFrame` loop that exists only
  while the document is visible and reduced motion is off, instead of motion's `useAnimationFrame`
  (which keeps motion's frame loop alive for the lifetime of the component). The step function itself
  is the upstream code, unchanged. Reduced motion / hidden → static text in `color`.

### Stack/Stack.jsx
- **M-Stack-1** — autoplay's `setInterval` only exists while the document is visible
  (`useDocumentVisible()`).
- **M-Stack-2** (Behavior Coach fix, 2026-11-18) — a card element that carries a React `key` keeps that key as its
  stack id (`k:<key>`); cards without a key get the upstream slot id (`index + 1`), so nothing changes for them.
  New optional prop `onSendToBack(id)`, called when a drag / click / autoplay sends a card to the back.
  *Why:* upstream numbers the cards by slot every time the `cards` prop changes, so a parent that re-orders
  `cards` (the Behavior Coach's ‹ › buttons, which also window the deck to 12 cards) only swapped the content of
  motion elements that never moved: no spring ran, the deck just jumped. With the id tied to the card, the same
  element moves to its new slot and Stack's own spring (`rotateZ` / `scale`) plays. `onSendToBack` lets the
  parent keep its own position in step with a card that was dragged away.
- **M-Stack-3** (UIFIX2, 2026-11-18) — the fallback deck is empty: upstream's 4 demo cards (`<img>` photos from
  `images.unsplash.com`, used only when `cards` is empty) are removed and the `else` branch returns `[]`.
  *Why:* Mason must make no outside request and the public bundle must hold no outside address. Mason's only
  caller (the Behavior Coach) never mounts Stack without cards, so nothing visible changes; but an empty
  `cards` from any later caller would have fetched four photos from a third-party server.

## Not modified (copied verbatim + header)
Counter.css, AnimatedList.css, SpotlightCard (jsx+css), Stepper (jsx+css), Stack.css, GlassSurface
(jsx+css), Dock (jsx+css), ElectricBorder.css, PixelTransition.css, StarBorder (jsx+css), LogoLoop.css,
ShinyText.css. (`npm run rb:diff` is the source of truth.)

Scrollbars (UI overhaul Task 1, 2026-11-16): no vendor file was changed. AnimatedList.css's visible-scrollbar
rules (8px, `#120F17` track, `#2F293A` thumb) are overridden in `src/theme.css` with the global theme
(`--scrollbar-*` from templates/mason_v2.html :root), under M-ALL-3 (theming, no file change). The two upstream
hidden-scrollbar cases are kept verbatim because they already hide the bar in Chromium AND Firefox:
AnimatedList.css `.no-scrollbar` (`::-webkit-scrollbar{display:none}` + `scrollbar-width:none`) and Dock.jsx's
inline `scrollbarWidth:'none'` (Dock isn't mounted anywhere today). Only a *visible* bar with
`scrollbar-width`/`scrollbar-color` loses the theme in Chromium, so don't copy those properties into Mason code.

## Components from the reactbits.dev registry (not in the pinned pull)

`GlideSelect` and `SquishSwitch` (chat-bar model picker, 2026-11-12) are newer than the pinned
pull, so they were taken from the shadcn registry JSON (`https://reactbits.dev/r/<Name>-JS-CSS.json`)
on 2026-11-12, unmodified apart from line 1. They are outside `RB_SET`, so `npm run rb:diff` does
not check them; their line-1 header says "React Bits registry" instead of the pinned-sha form.
`GlideSelect` needs `@hugeicons/react` + `@hugeicons/core-free-icons` (added to `ui/package.json`).
