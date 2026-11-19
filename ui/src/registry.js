// Widget + React Bits registry. NOBODY edits this file to add a widget: widgets self-register by
// existing as ui/src/widgets/<type>/{index.jsx, meta.js} (bundle design §3 "Registry").
//
//  - index.jsx  (lazy: its own chunk)  default export = the React component.
//  - meta.js    (eager: in the entry, so catalog() works without loading any widget chunk)
//               default export = catalog metadata, see normalizeMeta() below and ui/README.md.
//  - Folder names starting with "_" are internal (demo/test) types, and meta.catalog === false
//    marks mountable-but-not-a-dashboard-widget UI (e.g. WM's wm-picker). Both are excluded from
//    MasonUI.catalog() unless {internal:true} is passed.
const widgetLoaders = import.meta.glob('./widgets/*/index.jsx');
const widgetMetaMods = import.meta.glob('./widgets/*/meta.js', { eager: true });
const rbLoaders = import.meta.glob('./reactbits/*/*.jsx');

const TYPE_RE = /^[A-Za-z_][A-Za-z0-9_-]{0,63}$/;
const typeOf = p => p.split('/')[2];

const DEFAULT_SIZE = { w: 12, h: 9, minW: 6, minH: 4 }; // 36-col units (WM §4.2)

function clonePlain(v) {
  return v == null ? v : JSON.parse(JSON.stringify(v));
}

function normalizeMeta(type, m = {}) {
  const pages = Array.isArray(m.pages)
    ? m.pages
        .filter(p => p && typeof p.page === 'string')
        .map(p => ({
          page: p.page,
          id: typeof p.id === 'string' ? p.id : type,
          x: Number.isFinite(p.x) ? p.x : 0,
          y: Number.isFinite(p.y) ? p.y : 0,
          w: Number.isFinite(p.w) ? p.w : (m.size && m.size.w) || DEFAULT_SIZE.w,
          h: Number.isFinite(p.h) ? p.h : (m.size && m.size.h) || DEFAULT_SIZE.h,
          config: p.config && typeof p.config === 'object' ? p.config : {}
        }))
    : [];
  return {
    type,
    title: typeof m.title === 'string' && m.title ? m.title : type,
    icon: typeof m.icon === 'string' ? m.icon : 'ph ph-squares-four',
    group: typeof m.group === 'string' ? m.group : 'Feeds',
    multi: !!m.multi,
    size: { ...DEFAULT_SIZE, ...(m.size || {}) },
    defaults: m.defaults && typeof m.defaults === 'object' ? m.defaults : {},
    desc: typeof m.desc === 'string' ? m.desc : '',
    settings: Array.isArray(m.settings) ? m.settings : [],
    routes: Array.isArray(m.routes) ? m.routes : [],
    pages,
    catalog: m.catalog !== false,
    internal: type.startsWith('_')
  };
}

const widgets = new Map(); // type → {meta, load}
for (const p of Object.keys(widgetLoaders).sort()) {
  const type = typeOf(p);
  if (!TYPE_RE.test(type)) continue;
  const mod = widgetMetaMods[`./widgets/${type}/meta.js`];
  widgets.set(type, { meta: normalizeMeta(type, mod && mod.default), load: widgetLoaders[p] });
}

const components = new Map(); // React Bits name → loader (only <Name>/<Name>.jsx)
for (const p of Object.keys(rbLoaders).sort()) {
  const [, , dir, file] = p.split('/');
  if (file === `${dir}.jsx`) components.set(dir, rbLoaders[p]);
}

export const hasWidget = type => widgets.has(type);
export const widgetMeta = type => (widgets.has(type) ? widgets.get(type).meta : null);

/** {type: meta} — a deep copy. Internal (_*) and catalog:false types only with {internal:true}. */
export function catalog({ internal = false } = {}) {
  const out = {};
  for (const [type, w] of widgets) {
    if (!internal && (w.meta.internal || !w.meta.catalog)) continue;
    out[type] = clonePlain(w.meta);
  }
  return out;
}

export function mountables() {
  return [...components.keys(), ...widgets.keys()].sort();
}

const loaded = new Map();
async function resolveDefault(loader) {
  const mod = await loader();
  const C = mod && (mod.default || mod[Object.keys(mod)[0]]);
  if (typeof C !== 'function' && !(C && typeof C === 'object' && C.$$typeof)) throw new Error('module has no component export');
  return C;
}

/** Promise<Component> for a widget type or React Bits component name. */
export function loadComponent(name) {
  if (loaded.has(name)) return loaded.get(name);
  let loader = null;
  if (widgets.has(name)) loader = widgets.get(name).load;
  else if (components.has(name)) loader = components.get(name);
  if (!loader) return Promise.reject(new Error(`Unknown widget type: ${name}`));
  const p = resolveDefault(loader);
  loaded.set(name, p);
  p.catch(() => loaded.delete(name));
  return p;
}
