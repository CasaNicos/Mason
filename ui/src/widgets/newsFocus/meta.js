// newsFocus: page widget on #news only (catalog:false). 36-column Gridstack units (WM §4.2).
export default {
  title: 'Story focus',
  icon: 'ph ph-crosshair',
  group: 'News',
  multi: false,
  catalog: false,
  size: { w: 14, h: 14, minW: 7, minH: 5 },
  defaults: {},
  desc: 'The top story, or any headline you click: summary, Mason’s read, tickers with mini charts, related stories, source link',
  settings: [],
  routes: ['/api/news/flow', '/api/news', '/api/bars'],
  pages: [{ page: 'news', id: 'focus', x: 0, y: 13, w: 14, h: 14 }]
};
