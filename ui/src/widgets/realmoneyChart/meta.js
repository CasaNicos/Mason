// S3-F6 realmoneyChart: the #realmoney page chart (page-only, catalog:false; not a contracts §6.1 type).
// It follows chartId 'main', which rhExposure tile clicks and rhAlertsLog fire clicks emit, so on
// #realmoney "click a tile → chart it" works like the dashboard. Slot: right of the Earnings Strip,
// under True Exposure (rhAlertsLog moved to the bottom row to make room).
export default {
  title: 'Holdings Chart',
  icon: 'ph ph-chart-line-up',
  group: 'Robinhood',
  multi: false,
  catalog: false,
  size: { w: 16, h: 10, minW: 8, minH: 7 },
  defaults: {},
  desc: 'Daily candles for the holding you last clicked in True Exposure or the RH Alerts Log',
  settings: [],
  routes: ['/api/rh/exposure'],
  pages: [{ page: 'realmoney', x: 20, y: 13, w: 16, h: 10 }]
};
