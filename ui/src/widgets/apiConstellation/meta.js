// apiConstellation (UI overhaul Task 6). 36-column Gridstack units. catalog:false: an API-page widget,
// mounted by static/v2/api.js through mv.gridPage → MasonUI.mountWidget (not a dashboard catalog entry).
// `pages` places it on the harness's "Api" tab (the template's own layout lives in static/v2/api.js).
export default {
  title: 'Network constellation',
  icon: 'ph ph-graph',
  group: 'System',
  multi: false,
  catalog: false,
  size: { w: 24, h: 14, minW: 9, minH: 7 },
  defaults: {},
  desc: "Mason's data links as a live constellation: every probed source orbits the core, pulses show health and latency",
  settings: [],
  routes: ['/api/health'],
  pages: [{ page: 'api', x: 0, y: 0, w: 24, h: 16 }]
};
