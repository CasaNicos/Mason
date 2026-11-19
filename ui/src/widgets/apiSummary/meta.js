// apiSummary (UI overhaul Task 6). 36-column Gridstack units. catalog:false: an API-page widget mounted by
// static/v2/api.js through mv.gridPage → MasonUI.mountWidget. `pages` = the harness's "Api" tab.
export default {
  title: 'Link status',
  icon: 'ph ph-pulse',
  group: 'System',
  multi: false,
  catalog: false,
  size: { w: 12, h: 8, minW: 6, minH: 5 },
  defaults: {},
  desc: 'Source-health summary: links up / limited / down, health by kind, background loops',
  settings: [],
  routes: ['/api/health'],
  pages: [{ page: 'api', x: 24, y: 8, w: 12, h: 8 }]
};
