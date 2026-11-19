// #scanners default page layout (SC §5): the scorecard across the top, then 4 rows, each a list on
// the left and its chart on the right, paired by link channel a–d. 36-column Gridstack units.
// The 4 scans are stored by scan_id (SC §3 "The 4 scans", never by title); a 5th scan is not laid
// out automatically — any list can be pointed at it with its scan picker.
export const PAGE = 'scanners';
export const SCAN_IDS = Object.freeze([
  '9b0088e7-1c04-4b36-a627-523dff2e934f', // Catalyst Movers
  '02d48f02-1896-4f75-b342-6d7271ce460a', // Unusual Options Activity
  'f2d2bd5f-7f61-46bf-8046-01bc09c90e0b', // Earnings Reaction
  'b955071d-f993-4bca-8726-980b7bf875fc' // Quality Pullback (3mo+)
]);
export const LINKS = Object.freeze(['a', 'b', 'c', 'd']);
export const TOP_H = 7; // scorecard height (both horizons visible)
export const ROW_H = 8;
export const LIST_W = 20;
export const CHART_W = 16;
export const rowY = i => TOP_H + i * ROW_H;
