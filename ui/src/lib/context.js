// Per-widget-root context set by MasonUI.mountWidget. refreshSignal increments on every
// handle.refresh(); useRhApi listens and force-refetches its route. screen (S3-F6) is the widget's
// on-screen store (lib/visibility.js createScreenStore), null outside mountWidget.
import { createContext } from 'react';

export const WidgetCtx = createContext({ refreshSignal: 0, type: null, instanceId: null, screen: null });
