// newsBook: page widget on #news only (catalog:false — the dashboard catalog / layout allowlist don't
// know it). 36-column Gridstack units (WM §4.2).
export default {
  title: 'For your book',
  icon: 'ph ph-target',
  group: 'News',
  multi: false,
  catalog: false,
  size: { w: 11, h: 14, minW: 6, minH: 5 },
  defaults: {},
  desc: 'Stories touching your holdings and watchlist; click one to open the story card',
  settings: [],
  routes: ['/api/news/flow', '/api/news'],
  pages: [{ page: 'news', id: 'book', x: 14, y: 13, w: 11, h: 14 }]
};
