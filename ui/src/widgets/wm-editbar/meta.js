// wm-editbar: the Widget Manager edit-mode toolbar (WM design §6.3 / §7.3 / §7.5).
// Mounted by the template with MasonUI.mount(el, 'wm-editbar', {count, onReset, onDone}); never a dashboard widget.
export default {
  title: 'EDIT TOOLBAR',
  icon: 'ph ph-pencil-simple',
  group: 'System',
  multi: false,
  catalog: false,
  // UIFIX 2026-11-18: h 3 → 4, minH 2 → 4. A widget body is its rows minus the header and 24px of padding: at 2 rows
  // nothing was left for the bar and at 3 rows about 13px (1280px page), so the bar was cut off at any width.
  size: { w: 12, h: 4, minW: 6, minH: 4 },
  defaults: {},
  desc: 'Widget Manager edit-mode toolbar (internal)',
  settings: []
};
