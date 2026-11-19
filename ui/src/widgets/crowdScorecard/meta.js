// Page widget (not a catalog type: the /api/ui/layout allowlist only knows crowdFeed).
// UI overhaul Task 4: plain-English lead sentence + one bar per horizon (CI, n, beat-SPY kept, secondary).
export default {
  title: 'Crowd Scorecard',
  icon: 'ph ph-scales',
  group: 'Feeds',
  multi: false,
  catalog: false,
  size: { w: 20, h: 11, minW: 10, minH: 11 },
  defaults: { side: 'enter' },
  desc: 'Does following the crowd pay? Stocks that join (or leave) the 100 Most Popular vs SPY after 1 week, 1 month, 3 months',
  settings: [],
  routes: ['/api/rh/crowd'],
  pages: [{ page: 'crowd', x: 0, y: 22, w: 20, h: 11 }]
};
