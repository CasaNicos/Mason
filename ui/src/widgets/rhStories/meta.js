// rhStories catalog metadata (contracts §6.1: Trade Stories · Robinhood · multi ❌). #realmoney y >= 30.
export default {
  title: 'Trade Stories',
  icon: 'ph ph-book-open-text',
  group: 'Robinhood',
  multi: false,
  size: { w: 23, h: 12, minW: 11, minH: 9 },
  defaults: {},
  desc: 'Up to 5 closed trades a day: write the thesis, pick a verdict, Mason files the lesson',
  settings: [],
  routes: ['/api/rh/history/stories', '/api/rh/accounts'],
  pages: [{ page: 'realmoney', x: 13, y: 43, w: 23, h: 12 }]
};
