// Page widget (not a catalog type: the /api/ui/layout allowlist only knows crowdFeed).
// UI overhaul Task 4: four scrolling lanes (by rank / movers). Layout key bumped with the page (crowd_v2).
export default {
  title: 'Crowd Tracker',
  icon: 'ph ph-fire',
  group: 'Feeds',
  multi: false,
  catalog: false,
  size: { w: 24, h: 9, minW: 12, minH: 5 },
  defaults: { mode: 'rank' },
  desc: "Today's 100 Most Popular in four scrolling rows (by rank or by movement); your holdings marked",
  settings: [{ key: 'mode', label: 'Rows', options: ['rank', 'move'] }],
  routes: ['/api/rh/crowd'],
  pages: [{ page: 'crowd', x: 0, y: 0, w: 24, h: 9 }]
};
