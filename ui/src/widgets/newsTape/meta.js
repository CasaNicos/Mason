// newsTape: page widget on #news only (catalog:false). 36-column Gridstack units (WM §4.2).
export default {
  title: 'The tape',
  icon: 'ph ph-broadcast',
  group: 'News',
  multi: false,
  catalog: false,
  size: { w: 11, h: 14, minW: 6, minH: 5 },
  defaults: {},
  desc: 'Macro and market stories, newest first; click one to open the story card',
  settings: [],
  routes: ['/api/news/flow', '/api/news'],
  pages: [{ page: 'news', id: 'tape', x: 25, y: 13, w: 11, h: 14 }]
};
