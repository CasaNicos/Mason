// S3-F1: "Your overlap" — page #congress only. catalog:false keeps it out of MasonUI.catalog(), so WM
// never offers a type the /api/ui/layout allowlist would reject (contracts §6.1 lists only congressFeed).
export default {
  title: 'Your Overlap',
  icon: 'ph ph-intersect',
  group: 'Feeds',
  multi: false,
  catalog: false,
  size: { w: 9, h: 8, minW: 9, minH: 8 },
  defaults: {},
  desc: 'Held or watched tickers that Congress traded in the last 45 days',
  settings: [],
  routes: ['/api/rh/congress'],
  pages: [{ page: 'congress', x: 0, y: 0, w: 9, h: 8, config: { link: 'congress' } }]
};
