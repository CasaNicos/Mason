// React Bits: Animations/PixelTransition @ c49d6978d2496660f0f0c5a3b3ca77a059566a93
import { useRef, useEffect, useState } from 'react';
import { gsap } from 'gsap';
import './PixelTransition.css';

function PixelTransition({
  firstContent,
  secondContent,
  gridSize = 7,
  pixelColor = 'currentColor',
  animationStepDuration = 0.3,
  once = false,
  aspectRatio = '100%',
  className = '',
  style = {},
  // MASON MOD M-PixelTransition-1 (one-way adaptation for MasonUI.effects.pixelDissolve, WM §7.4):
  //   mode 'hover' (upstream behavior) | 'cover' (pixels switch ON in random order once, then
  //   onDone) | 'uncover' (start fully covered, pixels switch OFF in random order, then onDone).
  //   rows: grid rows (default gridSize) so pixels can stay square on a non-square widget.
  //   accentColor/accentShare: that share of pixels gets the accent color ("digital" dissolve).
  mode = 'hover',
  rows,
  accentColor,
  accentShare = 0,
  onDone
}) {
  const containerRef = useRef(null);
  const pixelGridRef = useRef(null);
  const activeRef = useRef(null);
  const delayedCallRef = useRef(null);

  const [isActive, setIsActive] = useState(false);
  const onDoneRef = useRef(onDone); // MASON MOD M-PixelTransition-1
  onDoneRef.current = onDone;
  const gridRows = Math.max(1, Math.round(rows || gridSize));

  const isTouchDevice =
    'ontouchstart' in window || navigator.maxTouchPoints > 0 || window.matchMedia('(pointer: coarse)').matches;

  useEffect(() => {
    const pixelGridEl = pixelGridRef.current;
    if (!pixelGridEl) return;

    pixelGridEl.replaceChildren(); // MASON MOD M-PixelTransition-2 (was innerHTML = '')

    for (let row = 0; row < gridRows; row++) {
      for (let col = 0; col < gridSize; col++) {
        const pixel = document.createElement('div');
        pixel.classList.add('pixelated-image-card__pixel');
        const accent = accentColor && accentShare > 0 && Math.random() < accentShare;
        pixel.style.backgroundColor = accent ? accentColor : pixelColor;

        const w = 100 / gridSize;
        const h = 100 / gridRows;
        pixel.style.width = `${w}%`;
        pixel.style.height = `${h}%`;
        pixel.style.left = `${col * w}%`;
        pixel.style.top = `${row * h}%`;
        if (mode === 'uncover') pixel.style.display = 'block';
        pixelGridEl.appendChild(pixel);
      }
    }
  }, [gridSize, gridRows, pixelColor, accentColor, accentShare, mode]);

  // MASON MOD M-PixelTransition-1: one-way cover / uncover, run once on mount.
  useEffect(() => {
    if (mode !== 'cover' && mode !== 'uncover') return undefined;
    const pixelGridEl = pixelGridRef.current;
    if (!pixelGridEl) return undefined;
    const pixels = pixelGridEl.querySelectorAll('.pixelated-image-card__pixel');
    const on = mode === 'cover';
    const each = animationStepDuration / Math.max(1, pixels.length);
    gsap.set(pixels, { display: on ? 'none' : 'block' });
    const tween = gsap.to(pixels, { display: on ? 'block' : 'none', duration: 0, stagger: { each, from: 'random' } });
    const done = gsap.delayedCall(animationStepDuration, () => {
      if (typeof onDoneRef.current === 'function') onDoneRef.current();
    });
    return () => {
      tween.kill();
      done.kill();
    };
  }, [mode, animationStepDuration, gridSize, gridRows]);

  // MASON MOD M-PixelTransition-3: kill pending tweens on unmount (upstream leaves them running).
  useEffect(
    () => () => {
      const el = pixelGridRef.current;
      if (el) gsap.killTweensOf(el.querySelectorAll('.pixelated-image-card__pixel'));
      if (delayedCallRef.current) delayedCallRef.current.kill();
    },
    []
  );

  const animatePixels = activate => {
    setIsActive(activate);

    const pixelGridEl = pixelGridRef.current;
    const activeEl = activeRef.current;
    if (!pixelGridEl || !activeEl) return;

    const pixels = pixelGridEl.querySelectorAll('.pixelated-image-card__pixel');
    if (!pixels.length) return;

    gsap.killTweensOf(pixels);
    if (delayedCallRef.current) {
      delayedCallRef.current.kill();
    }

    gsap.set(pixels, { display: 'none' });

    const totalPixels = pixels.length;
    const staggerDuration = animationStepDuration / totalPixels;

    gsap.to(pixels, {
      display: 'block',
      duration: 0,
      stagger: {
        each: staggerDuration,
        from: 'random'
      }
    });

    delayedCallRef.current = gsap.delayedCall(animationStepDuration, () => {
      activeEl.style.display = activate ? 'block' : 'none';
      activeEl.style.pointerEvents = activate ? 'none' : '';
    });

    gsap.to(pixels, {
      display: 'none',
      duration: 0,
      delay: animationStepDuration,
      stagger: {
        each: staggerDuration,
        from: 'random'
      }
    });
  };

  const oneWay = mode === 'cover' || mode === 'uncover'; // MASON MOD M-PixelTransition-1: no hover handlers
  const handleEnter = () => {
    if (oneWay) return;
    if (!isActive) animatePixels(true);
  };
  const handleLeave = () => {
    if (oneWay) return;
    if (isActive && !once) animatePixels(false);
  };
  const handleClick = () => {
    if (oneWay) return;
    if (!isActive) animatePixels(true);
    else if (isActive && !once) animatePixels(false);
  };

  return (
    <div
      ref={containerRef}
      className={`pixelated-image-card ${className}`}
      style={style}
      onMouseEnter={!isTouchDevice ? handleEnter : undefined}
      onMouseLeave={!isTouchDevice ? handleLeave : undefined}
      onClick={isTouchDevice ? handleClick : undefined}
      onFocus={!isTouchDevice ? handleEnter : undefined}
      onBlur={!isTouchDevice ? handleLeave : undefined}
      tabIndex={oneWay ? -1 : 0}
    >
      <div style={{ paddingTop: aspectRatio }} />
      <div className="pixelated-image-card__default" aria-hidden={isActive}>
        {firstContent}
      </div>
      <div className="pixelated-image-card__active" ref={activeRef} aria-hidden={!isActive}>
        {secondContent}
      </div>
      <div className="pixelated-image-card__pixels" ref={pixelGridRef} />
    </div>
  );
}

export default PixelTransition;
