// S3-F1: the Congress disclosure feed. Catalog entry per contracts §6.1 (multi, Feeds,
// settings {ticker?, heldOnly}); defaults are flat primitives (WM §5.2: '' means "no ticker").
// On the #congress page it carries config.link = 'congress' so it follows the overlap cards'
// filter and drives the page chart.
export default {
  title: 'Congress Feed',
  icon: 'ph ph-bank',
  group: 'Feeds',
  multi: true,
  size: { w: 12, h: 10, minW: 9, minH: 10 },
  defaults: { ticker: '', heldOnly: false },
  desc: 'Latest congressional trade disclosures: who, what, how much, how late, and how it played out vs SPY',
  settings: [
    { key: 'ticker', label: 'Ticker (blank = all)', type: 'text' },
    { key: 'heldOnly', label: 'Held tickers only', type: 'bool' }
  ],
  routes: ['/api/rh/congress'],
  pages: [{ page: 'congress', x: 9, y: 0, w: 14, h: 18, config: { link: 'congress' } }]
};
