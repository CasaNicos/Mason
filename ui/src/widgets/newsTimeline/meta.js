// newsTimeline: page widget on #news only (catalog:false). 36-column Gridstack units (WM §4.2).
export default {
  title: 'Sentiment river',
  icon: 'ph ph-wave-sine',
  group: 'News',
  multi: false,
  catalog: false,
  size: { w: 24, h: 7, minW: 8, minH: 4 },
  defaults: {},
  desc: "Today's news flow: bullish above the line, bearish below; drag to scrub and filter the feeds",
  settings: [],
  routes: ['/api/news/flow', '/api/news'],
  pages: [{ page: 'news', id: 'timeline', x: 0, y: 6, w: 24, h: 7 }]
};
