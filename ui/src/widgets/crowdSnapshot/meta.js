// UI overhaul Task 4 (2026-11-16) — page widget: click any ticker on #crowd to open it here.
export default {
  title: 'Ticker Snapshot',
  icon: 'ph ph-identification-card',
  group: 'Feeds',
  multi: false,
  catalog: false,
  size: { w: 22, h: 13, minW: 10, minH: 7 },
  defaults: { ticker: '' },
  desc: 'What the company does, sector and size, a price chart and its crowd history (rank path, joins/exits vs SPY)',
  settings: [],
  routes: ['/api/rh/crowd', '/api/rh/crowd/ticker'],
  pages: [{ page: 'crowd', x: 0, y: 9, w: 22, h: 13 }]
};
