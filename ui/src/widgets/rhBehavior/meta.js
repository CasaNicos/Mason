// rhBehavior catalog metadata (contracts §6.1: Behavior Coach · Robinhood · multi ❌). #realmoney y >= 30.
export default {
  title: 'Behavior Coach',
  icon: 'ph ph-brain',
  group: 'Robinhood',
  multi: false,
  size: { w: 13, h: 11, minW: 9, minH: 8 },
  defaults: {},
  desc: 'Your trading habits, what each one cost you, and one suggested rule per finding',
  settings: [],
  routes: ['/api/rh/behavior'],
  pages: [{ page: 'realmoney', x: 0, y: 43, w: 13, h: 12 }]
};
