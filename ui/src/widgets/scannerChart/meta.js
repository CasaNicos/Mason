// scannerChart: page-only (catalog:false). 1m candles following a link channel, see scannerShared/layout.js.
import { PAGE, LINKS, ROW_H, LIST_W, CHART_W, rowY } from '../scannerShared/layout.js';

export default {
  title: 'SCANNER CHART',
  icon: 'ph ph-chart-line',
  group: 'Feeds',
  multi: true,
  catalog: false,
  size: { w: CHART_W, h: ROW_H, minW: 6, minH: 5 },
  defaults: { link: 'a' },
  desc: '1-minute candles of the row picked in any scanner list on the same link channel',
  settings: [],
  routes: [],
  pages: LINKS.map((link, i) => ({ page: PAGE, id: `chart${i + 1}`, x: LIST_W, y: rowY(i), w: CHART_W, h: ROW_H, config: { link } }))
};
