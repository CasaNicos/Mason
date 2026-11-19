// tickerSearch: the header ticker search's results list. Mounted by static/v2/ticker-lookup.js with
// MasonUI.mount(el, 'tickerSearch', props) under the header input; never a dashboard widget.
export default {
  title: 'TICKER SEARCH',
  icon: 'ph ph-magnifying-glass',
  group: 'System',
  multi: false,
  catalog: false,
  size: { w: 12, h: 9, minW: 6, minH: 4 },
  defaults: {},
  desc: 'Header ticker search results (internal)',
  settings: [],
  routes: ['/api/symbol_search']
};
