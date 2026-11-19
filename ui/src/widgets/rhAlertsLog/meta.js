// rhAlertsLog catalog entry (contracts §6.1; WM §4.3). 36-column Gridstack units.
export default {
  title: 'RH Alerts Log',
  icon: 'ph ph-bell-ringing',
  group: 'Robinhood',
  multi: false,
  size: { w: 16, h: 10, minW: 8, minH: 7 },
  defaults: {},
  desc: 'Robinhood price alerts that fired in the last 30 days, plus your alert habits (informational)',
  settings: [],
  routes: ['/api/rh/alerts/log'],
  // S3-F6: moved from (20,13,16,10) to (0,23,20,7), still inside F4's y 0–29 band; (20,13,16,10) is now
  // the page chart (realmoneyChart), which follows this widget's fire clicks (chartId 'main').
  pages: [{ page: 'realmoney', x: 0, y: 23, w: 20, h: 7 }]
};
