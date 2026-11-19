// wm-editbar — the edit-mode toolbar (WM design §6.3, §7.3, §7.5), built on React Bits GlassSurface
// (frosted panel; its own backdrop-filter blur fallback where url() filters are unsupported) and
// DecryptedText ("EDIT MODE" scrambles, then settles left to right in ~600 ms, once per mount).
//
// Mounted by the template (T4):  MasonUI.mount(el, 'wm-editbar', {count, onReset, onDone}) → {update, unmount}
// Props (ENGINEER_BRIEF §5 C3): count (number of widgets on the board), onReset(), onDone().
// Phase 1 shows exactly two buttons, Reset and Done (W17). Updating `count` never replays the scramble.
// Motion: reduced motion → no scramble (plain label, 150 ms fade). Hidden page → the scramble waits
// until the page is visible, and if the page hides mid-scramble it stops at once on the plain label.
import { useEffect, useRef, useState } from 'react';
import GlassSurface from '../../reactbits/GlassSurface/GlassSurface.jsx';
import DecryptedText from '../../reactbits/DecryptedText/DecryptedText.jsx';
import { usePrefersReducedMotion } from '../../lib/motion.js';
import { useDocumentVisible } from '../../lib/visibility.js';
import './wm-editbar.css';

const LABEL = 'EDIT MODE';
// 9 characters revealed one per tick, sequential, left to right: 9 × 62 ms ≈ 560 ms + the final tick ≈ 620 ms
const SPEED_MS = 62;
const GLYPHS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789#%&*+=<>/\\|';

/** "N widgets" ("1 widget"); anything that isn't a finite number ≥ 0 → "— widgets". */
export function countLabel(n) {
  if (typeof n !== 'number' || !Number.isFinite(n) || n < 0) return '— widgets';
  const k = Math.floor(n);
  return `${k} widget${k === 1 ? '' : 's'}`;
}

// 'wait' (hidden at mount) → 'play' (DecryptedText mounted, scrambling once) → 'still' (plain label,
// after a mid-play hide). Reduced motion never leaves the plain label.
function useScramblePhase(reduce, visible) {
  const [phase, setPhase] = useState(() => (visible ? 'play' : 'wait'));
  const started = useRef(phase === 'play');
  useEffect(() => {
    if (!visible && started.current) setPhase('still');
    else if (visible && !started.current) {
      started.current = true;
      setPhase('play');
    }
  }, [visible]);
  return reduce ? 'still' : phase;
}

export default function WmEditbar({ count, onReset, onDone }) {
  const reduce = usePrefersReducedMotion();
  const visible = useDocumentVisible();
  const phase = useScramblePhase(reduce, visible);
  const cb = useRef({});
  cb.current = { onReset, onDone };
  const call = name => () => {
    const f = cb.current[name];
    if (typeof f === 'function') f();
  };

  return (
    <div className={`wm-editbar-host${reduce ? ' is-reduced' : ''}`}>
      <GlassSurface
        width="auto"
        height="auto"
        borderRadius={14}
        brightness={40}
        opacity={0.9}
        blur={10}
        backgroundOpacity={0.35}
        saturation={1.4}
        distortionScale={-90}
        displace={0.5}
        className="wm-editbar__glass"
      >
        <div className={`wm-editbar${reduce ? ' is-reduced' : ''}`} role="toolbar" aria-label="Edit layout">
          <span className="wm-editbar__pulse" aria-hidden="true" />
          <span className="wm-editbar__label">
            <span className="mui-sr">Edit mode</span>
            {phase === 'play' ? (
              <DecryptedText
                text={LABEL}
                animateOn="view"
                sequential
                revealDirection="start"
                speed={SPEED_MS}
                characters={GLYPHS}
                className="wm-editbar__ch"
                encryptedClassName="wm-editbar__ch is-enc"
                parentClassName="wm-editbar__dt"
                aria-hidden="true"
              />
            ) : (
              <span className="wm-editbar__plain" aria-hidden="true">
                {LABEL}
              </span>
            )}
          </span>
          <span className="wm-editbar__sep" aria-hidden="true" />
          <span className="wm-editbar__count" aria-live="polite">
            {countLabel(count)}
          </span>
          <span className="wm-editbar__actions">
            <button type="button" className="wm-editbar__btn" onClick={call('onReset')}>
              <i className="ph ph-arrow-counter-clockwise" aria-hidden="true" />
              <span>Reset</span>
            </button>
            <button type="button" className="wm-editbar__btn wm-editbar__btn--done" onClick={call('onDone')}>
              <i className="ph ph-check" aria-hidden="true" />
              <span>Done</span>
            </button>
          </span>
        </div>
      </GlassSurface>
    </div>
  );
}
