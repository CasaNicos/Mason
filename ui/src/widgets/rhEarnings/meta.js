// rhEarnings catalog entry (contracts §6.1; WM §4.3). 36-column Gridstack units.
export default {
  title: 'Earnings Strip',
  icon: 'ph ph-calendar-check',
  group: 'Robinhood',
  multi: false,
  size: { w: 20, h: 10, minW: 10, minH: 7 },
  defaults: {},
  desc: 'Held tickers by next report date: 8 quarters of EPS surprise, timing, implied move, held-through flag',
  settings: [],
  routes: ['/api/rh/earnings', '/api/rh/accounts'],
  pages: [{ page: 'realmoney', x: 0, y: 13, w: 20, h: 10 }]
};
