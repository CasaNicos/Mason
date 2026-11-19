// scannerList: page-only (catalog:false). One per scan on #scanners, see scannerShared/layout.js.
import { PAGE, SCAN_IDS, LINKS, ROW_H, LIST_W, rowY } from '../scannerShared/layout.js';

export default {
  title: 'SCANNER',
  icon: 'ph ph-list-magnifying-glass',
  group: 'Feeds',
  multi: true,
  catalog: false,
  size: { w: LIST_W, h: ROW_H, minW: 12, minH: 8 }, // one compact row + its advice line fits at 1280/900/600 px (Gridstack rows scale with width)
  defaults: { scan_id: '', slot: 1, link: 'a' },
  desc: 'One Robinhood scan, live: columns exactly as configured in Robinhood, plus first seen, qwen and watch status',
  settings: [],
  routes: ['/api/scanners'],
  pages: SCAN_IDS.map((scan_id, i) => ({
    page: PAGE,
    id: `list${i + 1}`,
    x: 0,
    y: rowY(i),
    w: LIST_W,
    h: ROW_H,
    config: { scan_id, slot: i + 1, link: LINKS[i] }
  }))
};
