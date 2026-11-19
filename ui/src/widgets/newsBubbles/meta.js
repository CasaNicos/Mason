// newsBubbles: page widget on #news only (catalog:false). 36-column Gridstack units (WM §4.2).
// config.group: 'tickers' | 'sectors' (the Tickers / Sectors toggle, saved per page).
export default {
  title: 'Impact map',
  icon: 'ph ph-circles-three',
  group: 'News',
  multi: false,
  catalog: false,
  size: { w: 12, h: 7, minW: 6, minH: 4 },
  defaults: { group: 'tickers' },
  desc: 'One bubble per ticker or sector: size = stories, color + ▲/▼ = net sentiment; click to filter',
  settings: [],
  routes: ['/api/news/flow', '/api/news'],
  pages: [{ page: 'news', id: 'bubbles', x: 24, y: 6, w: 12, h: 7 }]
};
