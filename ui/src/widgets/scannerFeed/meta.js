// scannerFeed: the compact catalog entry (contracts §6.1: "Scanner Hits", multi, {scan_id}, Feeds).
// Rows: ticker · scan · first seen · qwen score; a click opens #scanners focused on the hit.
export default {
  title: 'Scanner Hits',
  icon: 'ph ph-scan',
  group: 'Feeds',
  multi: true,
  size: { w: 12, h: 9, minW: 6, minH: 6 },
  defaults: { scan_id: '' },
  desc: 'Robinhood scanner hits this session; click a hit to open it on the Scanners page',
  settings: [{ key: 'scan_id', label: 'Scan (scan_id; blank = all scans)', type: 'text' }],
  routes: ['/api/scanners'],
  pages: []
};
