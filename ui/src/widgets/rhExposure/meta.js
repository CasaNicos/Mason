// rhExposure catalog entry (contracts §6.1; WM §4.3). 36-column Gridstack units.
// defaults.account: '' = Mine (server default scope = personal accounts), 'all', or one account label.
export default {
  title: 'True Exposure',
  icon: 'ph ph-squares-four',
  group: 'Robinhood',
  multi: false,
  size: { w: 22, h: 13, minW: 10, minH: 7 },
  defaults: { account: '' },
  desc: 'Treemap of what you really hold once ETFs are looked through; colored by day change',
  settings: [],
  routes: ['/api/rh/exposure', '/api/rh/accounts'],
  pages: [{ page: 'realmoney', x: 14, y: 0, w: 22, h: 13 }]
};
