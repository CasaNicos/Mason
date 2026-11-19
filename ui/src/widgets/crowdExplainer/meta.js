// UI overhaul Task 4 (2026-11-16) — page widget: "What is the crowd?" illustrated tour (React Bits Stepper).
// Sits top-right beside the tracker, so a dismissed (folded) card only leaves room beside the tracker.
export default {
  title: 'What is the crowd?',
  icon: 'ph ph-question',
  group: 'Feeds',
  multi: false,
  catalog: false,
  size: { w: 12, h: 9, minW: 7, minH: 5 },
  defaults: { dismissed: false },
  desc: "A four-step picture guide: Robinhood's 100 Most Popular, joining / leaving the list, why Mason grades it vs SPY",
  settings: [],
  routes: ['/api/rh/crowd'],
  pages: [{ page: 'crowd', x: 24, y: 0, w: 12, h: 9 }]
};
