// S3-F1: Congress leaderboard — page #congress only (catalog:false; not a contracts §6.1 type).
export default {
  title: 'Congress Leaderboard',
  icon: 'ph ph-ranking',
  group: 'Feeds',
  multi: false,
  catalog: false,
  size: { w: 9, h: 10, minW: 8, minH: 8 },
  defaults: {},
  desc: 'Politicians ranked by shrunk 90-day excess return vs SPY, with n',
  settings: [],
  routes: ['/api/rh/congress'],
  pages: [{ page: 'congress', x: 0, y: 8, w: 9, h: 10, config: { link: 'congress' } }]
};
