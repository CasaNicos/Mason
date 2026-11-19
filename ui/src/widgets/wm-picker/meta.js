// wm-picker: the Widget Manager palette's "Add widget…" list (WM design §6.2 / §7.1).
// Mounted by the template palette with MasonUI.mount(el, 'wm-picker', props); never a dashboard widget.
// Props contract: docs/ui/wm_build/ENGINEER_BRIEF.md §5 C3 (see index.jsx header).
export default {
  title: 'WIDGET PICKER',
  icon: 'ph ph-plus-square',
  group: 'System',
  multi: false,
  catalog: false,
  size: { w: 12, h: 9, minW: 6, minH: 4 },
  defaults: {},
  desc: 'Widget Manager palette picker (internal)',
  settings: []
};
