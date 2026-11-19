// rhStatus catalog entry (contracts §6.1: group System; WM §4.3). 36-column Gridstack units.
export default {
  title: 'RH Data Health',
  icon: 'ph ph-heartbeat',
  group: 'System',
  multi: false,
  size: { w: 36, h: 5, minW: 10, minH: 4 },
  defaults: {},
  desc: 'Robinhood connection, last sync, backfill, rate-limit hits and cross-check mismatches',
  settings: [],
  routes: ['/api/rh/status'],
  // S3-F6: was (0,23,36,5); shares the y 23 row with rhAlertsLog now (pills wrap to 2–3 lines at w 16).
  pages: [{ page: 'realmoney', x: 20, y: 23, w: 16, h: 7 }]
};
