// A small segmented control (radiogroup of pills) with a sliding highlight (motion layoutId).
// Built here because React Bits has no segmented/tab control: GooeyNav (the closest) is an uncontrolled link nav
// whose gooey filter assumes a black page, and PillNav is a whole site nav bar with a logo and a mobile menu.
// Keyboard: one pill is in the Tab order (the selected one); arrows / Home / End move AND select (radio pattern).
import { useId, useRef } from 'react';
import { motion } from 'motion/react';

export default function Segmented({ options, value, onChange, label, className = '' }) {
  const lid = useId();
  const refs = useRef([]);
  const idx = Math.max(
    0,
    options.findIndex(o => o.key === value)
  );
  const pick = i => {
    const o = options[i];
    if (!o) return;
    if (o.key !== value && typeof onChange === 'function') onChange(o.key);
    const el = refs.current[i];
    if (el && typeof el.focus === 'function') el.focus();
  };
  const onKeyDown = e => {
    const n = options.length;
    let next = null;
    if (e.key === 'ArrowRight' || e.key === 'ArrowDown') next = (idx + 1) % n;
    else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') next = (idx - 1 + n) % n;
    else if (e.key === 'Home') next = 0;
    else if (e.key === 'End') next = n - 1;
    if (next == null) return;
    e.preventDefault();
    pick(next);
  };
  return (
    <div className={`mui-tk-seg ${className}`} role="radiogroup" aria-label={label} onKeyDown={onKeyDown}>
      {options.map((o, i) => {
        const on = i === idx;
        return (
          <button
            key={o.key}
            ref={el => {
              refs.current[i] = el;
            }}
            type="button"
            role="radio"
            aria-checked={on}
            tabIndex={on ? 0 : -1}
            className={`mui-tk-seg__opt${on ? ' is-on' : ''}`}
            title={o.title || undefined}
            onClick={() => pick(i)}
          >
            {on ? (
              <motion.span
                layoutId={`mui-tk-seg-${lid}`}
                className="mui-tk-seg__pill"
                aria-hidden="true"
                transition={{ type: 'spring', stiffness: 560, damping: 40 }}
              />
            ) : null}
            <span className="mui-tk-seg__lbl">{o.label}</span>
          </button>
        );
      })}
    </div>
  );
}
