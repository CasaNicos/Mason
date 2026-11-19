// F0 pipeline demo (internal: the leading "_" keeps it out of MasonUI.catalog()).
// It exists to prove the bundle end to end: lazy chunk, meta, page layout, useRhApi + mock
// fixture, shared states, React Bits pieces, per-instance state, saveConfig. Not a product widget.
export default {
  title: 'DEMO FEED',
  icon: 'ph ph-flask',
  group: 'System',
  multi: true,
  size: { w: 12, h: 10, minW: 6, minH: 5 },
  defaults: { label: 'Demo', throwOnRender: false },
  desc: 'F0 foundation demo — not shipped to the catalog',
  settings: [{ key: 'label', label: 'Label', type: 'text' }],
  routes: ['/api/_demo/feed'],
  pages: [{ page: 'foundation', x: 0, y: 0, w: 14, h: 12 }]
};
