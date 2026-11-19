// tickerCard: the ticker lookup popup's content. Mounted by static/v2/ticker-lookup.js with
// MasonUI.mount(host, 'tickerCard', {symbol, onClose, onAddChart, follow}); never a dashboard widget.
export default {
  title: 'TICKER CARD',
  icon: 'ph ph-chart-line-up',
  group: 'System',
  multi: false,
  catalog: false,
  size: { w: 12, h: 9, minW: 6, minH: 4 },
  defaults: {},
  desc: 'Ticker lookup popup (internal)',
  settings: [],
  routes: ['/api/ticker_card', '/api/bars', '/api/following']
};
