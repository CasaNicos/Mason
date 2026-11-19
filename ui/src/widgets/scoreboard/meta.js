// scoreboard. 36-column Gridstack units (WM §4.2). catalog:false for the same reason as learning_loop and
// calendar: MasonUI.catalog() is the Robinhood type list (merged into the dashboard only when the RH flag is
// on); this widget is catalogued by the template's own WIDGET_CATALOG.scoreboard (same title / icon / group /
// size), so it exists with that flag off too, and is mounted through MasonUI.mountWidget.
export default {
  title: 'Scoreboard',
  icon: 'ph ph-trophy',
  group: 'Account',
  multi: false,
  catalog: false,
  size: { w: 14, h: 20, minW: 8, minH: 8 },
  defaults: {},
  desc: "How Mason's own trades are doing against the same money in the S&P 500",
  settings: [],
  routes: ['/api/learning/scoreboard']
};
