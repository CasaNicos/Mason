// S3-F2 catalog entry "Crowd Moves" (contracts §6.1: multi ✅ {kind: all|enter|exit}, Feeds).
export default {
  title: 'Crowd Moves',
  icon: 'ph ph-users-three',
  group: 'Feeds',
  multi: true,
  size: { w: 12, h: 10, minW: 8, minH: 6 },
  defaults: { kind: 'all' },
  desc: "Tickers joining / leaving Robinhood's 100 Most Popular, graded vs SPY",
  settings: [{ key: 'kind', label: 'Show', options: ['all', 'enter', 'exit'] }],
  routes: ['/api/rh/crowd'],
  // On #crowd (UI overhaul Task 4 layout) the feed sits bottom-right, beside the scorecard. onPage hides the
  // "Crowd page ↗" link.
  pages: [{ page: 'crowd', x: 20, y: 22, w: 16, h: 11, config: { kind: 'all', onPage: true } }]
};
