// apiSources (UI overhaul Task 6). 36-column Gridstack units. catalog:false: an API-page widget mounted by
// static/v2/api.js through mv.gridPage → MasonUI.mountWidget. `pages` = the harness's "Api" tab.
// config.filter ('all' | 'issues' | 'up') is a flat string (WM §5.2), persisted by the page glue.
export default {
  title: 'Source detail',
  icon: 'ph ph-plugs-connected',
  group: 'System',
  multi: false,
  catalog: false,
  size: { w: 36, h: 10, minW: 8, minH: 5 },
  defaults: { filter: 'all' },
  desc: 'Every probed source as a detail card: status, latency, last check, detail, probe; click to focus it',
  settings: [],
  routes: ['/api/health'],
  pages: [{ page: 'api', x: 0, y: 16, w: 36, h: 11 }]
};
