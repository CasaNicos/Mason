// The link-channel chip shared by scanner lists and charts: glyph + letter in the channel color,
// click (or Enter/Space) opens a small radio menu of the 4 channels. Escape or a click outside
// closes it. Built from scratch: no React Bits piece is a compact menu-radio (Dock/PillNav are
// navigation bars, far too large for a widget header).
import { useEffect, useRef, useState } from 'react';
import { CHANNELS, channel } from './link.js';

export default function LinkPicker({ value, onChange, what = 'list' }) {
  const [open, setOpen] = useState(false);
  const wrap = useRef(null);
  const ch = channel(value);

  useEffect(() => {
    if (!open) return undefined;
    const away = e => {
      if (wrap.current && !wrap.current.contains(e.target)) setOpen(false);
    };
    document.addEventListener('pointerdown', away);
    return () => document.removeEventListener('pointerdown', away);
  }, [open]);

  const pick = id => {
    setOpen(false);
    if (id !== ch.id && typeof onChange === 'function') onChange(id);
  };

  return (
    <span className="sc-link" ref={wrap}>
      <button
        type="button"
        className={`sc-link-btn sc-link--${ch.id}`}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={`Link ${ch.label}: this ${what} is on link channel ${ch.label}. Change channel`}
        title={`Link ${ch.label} — lists and charts on the same link follow each other. Click to re-link.`}
        onClick={() => setOpen(o => !o)}
      >
        <span className="sc-link-glyph" aria-hidden="true">
          {ch.glyph}
        </span>{' '}
        {ch.label}
      </button>
      {open ? (
        <span
          className="sc-link-menu"
          role="menu"
          aria-label="Link channel"
          onKeyDown={e => {
            if (e.key === 'Escape') setOpen(false);
          }}
        >
          {CHANNELS.map(c => (
            <button
              key={c.id}
              type="button"
              role="menuitemradio"
              aria-checked={c.id === ch.id}
              className={`sc-link-opt sc-link--${c.id}${c.id === ch.id ? ' is-on' : ''}`}
              onClick={() => pick(c.id)}
            >
              {c.glyph} {c.label}
            </button>
          ))}
        </span>
      ) : null}
    </span>
  );
}
