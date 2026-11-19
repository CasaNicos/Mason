// calendar. 36-column Gridstack units (WM §4.2). catalog:false for the same reason as learning_loop:
// MasonUI.catalog() is the Robinhood type list (merged into the dashboard only when the RH flag is on);
// this widget is catalogued by the template's own WIDGET_CATALOG.calendar (same title / icon / group /
// size), so it exists with that flag off too, and is mounted through MasonUI.mountWidget.
export default {
  title: 'Calendar',
  icon: 'ph ph-calendar-blank',
  group: 'Lists',
  multi: false,
  catalog: false,
  size: { w: 14, h: 18, minW: 8, minH: 8 },
  defaults: {},
  desc: 'Dates that matter: earnings, the Fed, hearings, conferences, and your own events',
  settings: [],
  routes: ['/api/calendar', '/api/calendar/meta']
};
