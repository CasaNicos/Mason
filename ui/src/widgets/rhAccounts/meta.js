// rhAccounts catalog entry (contracts §6.1; WM §4.3). 36-column Gridstack units.
export default {
  title: 'Robinhood Accounts',
  icon: 'ph ph-wallet',
  group: 'Robinhood',
  multi: false,
  size: { w: 14, h: 13, minW: 8, minH: 8 },
  defaults: {},
  desc: 'Every Robinhood account: equity, day P&L, cash ring, idle cash, dividends; expand for top positions',
  settings: [],
  routes: ['/api/rh/accounts'],
  pages: [{ page: 'realmoney', x: 0, y: 0, w: 14, h: 13 }]
};
