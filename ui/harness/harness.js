/* Mason UI harness controller (vanilla; Gridstack + window.MasonUI, exactly like the template).
 *
 * Tabs: one per future page (Congress, Crowd, Scanners, Real Money), any extra page a widget
 * declares (e.g. the F0 demos' "foundation"), and "Dashboard sim", which mounts every registered
 * widget type TWICE. Page layouts come from each widget's meta.js `pages` — nobody edits this file
 * to add a widget. All text is set with textContent (never innerHTML).
 *
 * Test hooks (Playwright): window.__hx = {ready, show(page), grids, handles, remove(id), ids(page)}.
 */
const PAGES = [
  { id: 'congress', title: 'Congress', desc: 'Disclosures lag trades by up to 45 days; graded from the first session after disclosure.' },
  { id: 'crowd', title: 'Crowd', desc: 'Robinhood "100 Most Popular" entries and exits.' },
  { id: 'scanners', title: 'Scanners', desc: 'Robinhood scanner intake (SCANNER_INTAKE_DESIGN §5).' },
  { id: 'realmoney', title: 'Real Money', desc: 'Accounts, exposure, earnings, paper vs real.' }
];
const SIM = { id: 'dashboard', title: 'Dashboard sim', desc: 'Every registered widget type, mounted twice.' };

const BASE_COLS = 36;
const BREAKPOINTS = [{ w: 640, c: 12 }, { w: 1040, c: 24 }]; // same as templates/mason_v2.html

const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = String(text);
  return n;
};

async function boot() {
  if (!window.MasonUI) {
    await new Promise(r => window.addEventListener('masonui:ready', r, { once: true }));
  }
  const UI = window.MasonUI;
  await UI.ready;
  const cat = UI.catalog({ internal: true });
  const types = Object.keys(cat).sort();

  const mode = document.getElementById('hx-mode');
  const mock = UI.isMock();
  mode.textContent = mock ? 'mock data · ?rhx_mock=1' : 'live api';
  mode.classList.toggle('mock', mock);

  // pages declared by widgets, beyond the four fixed ones
  const extra = [];
  for (const t of types) {
    for (const p of cat[t].pages) {
      if (!PAGES.some(x => x.id === p.page) && !extra.some(x => x.id === p.page)) {
        extra.push({ id: p.page, title: p.page.replace(/(^|[-_])(\w)/g, (_m, _a, c) => ' ' + c.toUpperCase()).trim(), desc: 'Declared by widget meta.js' });
      }
    }
  }
  const pages = [...PAGES, ...extra, SIM];

  const tabs = document.getElementById('hx-tabs');
  const host = document.getElementById('hx-pages');
  const grids = {};
  const handles = {}; // widget instance id → {handle, item, page, type}
  const sections = {};

  const placementsFor = pageId => {
    if (pageId === SIM.id) {
      const out = [];
      for (const t of types) for (const n of [1, 2]) out.push({ type: t, id: `${t}__${n}`, w: cat[t].size.w, h: cat[t].size.h, auto: true, config: {} });
      return out;
    }
    const out = [];
    for (const t of types) for (const p of cat[t].pages) if (p.page === pageId) out.push({ type: t, id: `${pageId}:${p.id}`, x: p.x, y: p.y, w: p.w, h: p.h, config: p.config || {} });
    return out;
  };

  for (const p of pages) {
    const count = placementsFor(p.id).length;
    const tab = el('button', 'hx-tab');
    tab.type = 'button';
    tab.setAttribute('role', 'tab');
    tab.dataset.page = p.id;
    tab.appendChild(el('span', null, p.title));
    tab.appendChild(el('span', 'hx-count', count));
    tab.addEventListener('click', () => show(p.id));
    tabs.appendChild(tab);

    const sec = el('section', 'hx-page');
    sec.id = `hx-page-${p.id}`;
    sec.dataset.page = p.id;
    sec.hidden = true;
    const head = el('div', 'hx-page-head');
    head.appendChild(el('h1', null, p.title));
    head.appendChild(el('p', null, p.desc));
    sec.appendChild(head);
    if (!count) {
      const empty = el('div', 'hx-empty');
      empty.appendChild(el('span', null, `No widget declares page "${p.id}" yet. A widget appears here when its `));
      empty.appendChild(el('code', null, 'meta.js'));
      empty.appendChild(el('span', null, ' has '));
      empty.appendChild(el('code', null, `pages: [{ page: '${p.id}', x, y, w, h }]`));
      empty.appendChild(el('span', null, ' (36-column units). See ui/README.md.'));
      sec.appendChild(empty);
    }
    const gridEl = el('div', 'grid-stack');
    sec.appendChild(gridEl);
    host.appendChild(sec);
    sections[p.id] = { sec, gridEl, built: false };
  }

  function buildGrid(pageId) {
    const s = sections[pageId];
    if (s.built) return;
    s.built = true;
    const gs = window.GridStack.init(
      {
        column: BASE_COLS,
        columnOpts: { breakpointForWindow: true, breakpoints: BREAKPOINTS, layout: 'moveScale' },
        cellHeight: 'auto',
        margin: 10,
        float: true,
        handle: '.m-widget-head',
        resizable: { handles: 'e, se, s' },
        animate: true
      },
      s.gridEl
    );
    grids[pageId] = gs;
    for (const pl of placementsFor(pageId)) addWidget(pageId, gs, pl);
  }

  function addWidget(pageId, gs, pl) {
    const meta = cat[pl.type];
    const item = el('div', 'grid-stack-item');
    item.setAttribute('gs-id', pl.id);
    item.setAttribute('gs-w', pl.w);
    item.setAttribute('gs-h', pl.h);
    if (pl.auto) item.setAttribute('gs-auto-position', 'true');
    else {
      item.setAttribute('gs-x', pl.x);
      item.setAttribute('gs-y', pl.y);
    }
    item.setAttribute('gs-min-w', meta.size.minW || 1);
    item.setAttribute('gs-min-h', meta.size.minH || 2);
    const content = el('div', 'grid-stack-item-content');
    const w = el('div', 'm-widget');
    w.dataset.widgetId = pl.id;
    w.dataset.type = pl.type;
    const head = el('div', 'm-widget-head');
    const icon = el('i', `m-widget-icon ${meta.icon}`);
    head.appendChild(icon);
    head.appendChild(el('span', 'm-widget-title', meta.title));
    if (pageId === SIM.id) head.appendChild(el('span', 'hx-inst', pl.id.split('__')[1] === '1' ? '#1' : '#2'));
    const x = el('button', 'hx-x', '✕');
    x.type = 'button';
    x.title = 'Remove this instance';
    x.setAttribute('aria-label', `Remove ${meta.title} ${pl.id}`);
    x.addEventListener('pointerdown', e => e.stopPropagation());
    x.addEventListener('click', () => remove(pl.id));
    head.appendChild(x);
    const body = el('div', 'm-widget-body');
    w.appendChild(head);
    w.appendChild(body);
    content.appendChild(w);
    item.appendChild(content);
    gs.el.appendChild(item);
    gs.makeWidget(item);
    const handle = UI.mountWidget(pl.type, body, { id: pl.id, config: pl.config });
    handles[pl.id] = { handle, item, page: pageId, type: pl.type };
  }

  function remove(id) {
    const h = handles[id];
    if (!h) return false;
    h.handle.destroy();
    grids[h.page].removeWidget(h.item);
    delete handles[id];
    updateDebug();
    return true;
  }

  function show(pageId) {
    if (!sections[pageId]) pageId = pages[0].id;
    for (const [id, s] of Object.entries(sections)) s.sec.hidden = id !== pageId;
    for (const t of tabs.children) t.setAttribute('aria-selected', t.dataset.page === pageId ? 'true' : 'false');
    buildGrid(pageId);
    if (location.hash !== `#${pageId}`) history.replaceState(null, '', `#${pageId}`);
    updateDebug();
  }

  const dbg = document.getElementById('hx-dbg');
  function updateDebug() {
    const s = UI.__debug.snapshot();
    dbg.textContent = `roots ${s.roots} · intervals ${s.intervals} · rAF ${s.rafPending}`;
  }
  dbg.addEventListener('click', updateDebug);

  const start = (location.hash || '').slice(1);
  show(sections[start] ? start : pages[0].id);

  window.__hx = {
    ready: true,
    pages: pages.map(p => p.id),
    types,
    show,
    grids,
    handles,
    remove,
    ids: pageId => Object.keys(handles).filter(k => handles[k].page === pageId).sort()
  };
  window.dispatchEvent(new CustomEvent('hx:ready'));
}

boot();
