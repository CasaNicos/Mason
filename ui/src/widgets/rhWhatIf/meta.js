// rhWhatIf catalog metadata (contracts §6.1: What-If · Robinhood · multi ❌). #realmoney y >= 30.
export default {
  title: 'What-If',
  icon: 'ph ph-arrows-split',
  group: 'Robinhood',
  multi: false,
  size: { w: 15, h: 12, minW: 10, minH: 9 },
  defaults: { horizon: 'today' },
  desc: 'What your exits cost or saved you if you had held 30, 90, 180 days or until today',
  settings: [{ key: 'horizon', label: 'Horizon', options: ['30', '90', '180', 'today'] }],
  routes: ['/api/rh/whatif', '/api/rh/accounts'],
  pages: [{ page: 'realmoney', x: 21, y: 30, w: 15, h: 13 }]
};
