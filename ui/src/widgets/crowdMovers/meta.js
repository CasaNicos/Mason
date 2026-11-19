// UI overhaul Task 4 (2026-11-16) — page widget: today's crowd activity + biggest climbers / slides.
export default {
  title: 'Crowd Movers',
  icon: 'ph ph-arrows-down-up',
  group: 'Feeds',
  multi: false,
  catalog: false,
  size: { w: 14, h: 13, minW: 8, minH: 8 },
  defaults: { lookback: 'week' },
  desc: "How much the list reshuffled today, and who climbed or slipped over a day, a week or a month",
  settings: [{ key: 'lookback', label: 'Look back', options: ['day', 'week', 'month'] }],
  routes: ['/api/rh/crowd'],
  pages: [{ page: 'crowd', x: 22, y: 9, w: 14, h: 13 }]
};
