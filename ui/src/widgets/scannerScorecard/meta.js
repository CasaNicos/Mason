// scannerScorecard: page-only (catalog:false; contradiction K8 in the F3 packet). SC §4.6 per-scan
// hit rate and move at +1/+5 sessions, on the page.
import { PAGE, TOP_H } from '../scannerShared/layout.js';

export default {
  title: 'SCANNER SCORECARD',
  icon: 'ph ph-target',
  group: 'Feeds',
  multi: false,
  catalog: false,
  size: { w: 36, h: TOP_H, minW: 12, minH: TOP_H },
  defaults: {},
  desc: 'Which scanner has edge: graded hit rate (CI95, n) at +1 and +5 sessions per scan',
  settings: [],
  routes: ['/api/scanners/scorecard', '/api/scanners'],
  pages: [{ page: PAGE, x: 0, y: 0, w: 36, h: TOP_H }]
};
