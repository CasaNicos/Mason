// rhPvr catalog metadata (contracts §6.1: Paper vs Real · Robinhood · multi ❌).
// Page slot: #realmoney rows y >= 30 belong to F5 (F4 owns y 0–29). 36-column units.
export default {
  title: 'Paper vs Real',
  icon: 'ph ph-scales',
  group: 'Robinhood',
  multi: false,
  size: { w: 20, h: 12, minW: 12, minH: 10 },
  defaults: { range: '1M', account: 'all' },
  desc: "Mason's paper picks vs your real trades: cumulative return, head-to-head and overlap, with caveats",
  // WM §4.3 select shape. The account is picked in the widget (its options come from the data).
  settings: [{ key: 'range', label: 'Range', options: ['1M', '3M', 'YTD', 'ALL'] }],
  routes: ['/api/rh/pvr', '/api/rh/accounts'],
  pages: [{ page: 'realmoney', x: 0, y: 30, w: 21, h: 13 }]
};
