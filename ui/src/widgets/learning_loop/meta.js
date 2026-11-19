// learning_loop. 36-column Gridstack units (WM §4.2). catalog:false because MasonUI.catalog() is the
// Robinhood type list (merged into the dashboard only when the RH flag is on); this widget is catalogued
// by the template's own WIDGET_CATALOG.learning_loop (same title / icon / group / size) so it exists
// with that flag off too, and is mounted through MasonUI.mountWidget.
export default {
  title: 'Learning Loop',
  icon: 'ph ph-brain',
  group: 'System',
  multi: false,
  catalog: false,
  size: { w: 18, h: 16, minW: 9, minH: 6 },
  defaults: {},
  desc: "Mason's own trades: Opus budget, open positions, calibration, closed P&L",
  settings: [],
  routes: ['/api/agent_loop/status']
};
