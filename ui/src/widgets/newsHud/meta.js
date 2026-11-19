// newsHud: page widget on #news only (catalog:false). 36-column Gridstack units (WM §4.2).
export default {
  title: 'Wire HUD',
  icon: 'ph ph-broadcast',
  group: 'News',
  multi: false,
  catalog: false,
  size: { w: 36, h: 6, minW: 10, minH: 3 },
  defaults: {},
  desc: 'Live wire tape, story counts, bull/bear gauge, and the filter chips every News widget follows',
  settings: [],
  routes: ['/api/news/flow', '/api/news'],
  pages: [{ page: 'news', id: 'hud', x: 0, y: 0, w: 36, h: 6 }]
};
