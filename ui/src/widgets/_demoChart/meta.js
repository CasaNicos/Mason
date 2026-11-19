// F0 demo of the row → chart pattern (internal: "_" keeps it out of MasonUI.catalog()).
export default {
  title: 'DEMO CHART',
  icon: 'ph ph-chart-line',
  group: 'System',
  multi: true,
  size: { w: 18, h: 10, minW: 8, minH: 6 },
  defaults: { tickers: ['NVDA', 'RKLB', 'SPY', 'AMD'] },
  desc: 'F0 foundation demo: click a row, the chart follows (mason:chart-symbol)',
  settings: [],
  pages: [{ page: 'foundation', x: 14, y: 0, w: 22, h: 12 }]
};
