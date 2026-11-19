// S3-F1: Congress chart — page #congress only (catalog:false; not a contracts §6.1 type).
export default {
  title: 'Congress Chart',
  icon: 'ph ph-chart-line',
  group: 'Feeds',
  multi: false,
  catalog: false,
  size: { w: 13, h: 18, minW: 7, minH: 8 },
  defaults: {},
  desc: 'Daily candles for the selected Congress ticker, with its disclosure dates listed below',
  settings: [],
  routes: ['/api/rh/congress'],
  pages: [{ page: 'congress', x: 23, y: 0, w: 13, h: 18, config: { link: 'congress' } }]
};
