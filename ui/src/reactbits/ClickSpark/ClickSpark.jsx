// React Bits: Animations/ClickSpark @ c49d6978d2496660f0f0c5a3b3ca77a059566a93
import { useRef, useEffect, useCallback } from 'react';

const ClickSpark = ({
  sparkColor = '#fff',
  sparkSize = 10,
  sparkRadius = 15,
  sparkCount = 8,
  duration = 400,
  easing = 'ease-out',
  extraScale = 1.0,
  children,
  burst = null, // MASON MOD M-ClickSpark-2: {x, y, key} fires a burst programmatically (MasonUI.effects.spark)
  onIdle // MASON MOD M-ClickSpark-2: called when the last spark has finished
}) => {
  const canvasRef = useRef(null);
  const sparksRef = useRef([]);
  const startTimeRef = useRef(null);
  // MASON MOD M-ClickSpark-1: upstream keeps a rAF loop running forever, even with no sparks.
  // The loop now starts on a click/burst and stops itself once every spark has expired.
  const drawRef = useRef(null);
  const animIdRef = useRef(null);
  const onIdleRef = useRef(onIdle);
  onIdleRef.current = onIdle;
  const startLoop = useCallback(() => {
    if (animIdRef.current != null || !drawRef.current) return;
    animIdRef.current = requestAnimationFrame(drawRef.current);
  }, []);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const parent = canvas.parentElement;
    if (!parent) return;

    let resizeTimeout;

    const resizeCanvas = () => {
      const { width, height } = parent.getBoundingClientRect();
      if (canvas.width !== width || canvas.height !== height) {
        canvas.width = width;
        canvas.height = height;
      }
    };

    const handleResize = () => {
      clearTimeout(resizeTimeout);
      resizeTimeout = setTimeout(resizeCanvas, 100);
    };

    const ro = new ResizeObserver(handleResize);
    ro.observe(parent);

    resizeCanvas();

    return () => {
      ro.disconnect();
      clearTimeout(resizeTimeout);
    };
  }, []);

  const easeFunc = useCallback(
    t => {
      switch (easing) {
        case 'linear':
          return t;
        case 'ease-in':
          return t * t;
        case 'ease-in-out':
          return t < 0.5 ? 2 * t * t : -1 + (4 - 2 * t) * t;
        default:
          return t * (2 - t);
      }
    },
    [easing]
  );

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');

    const draw = timestamp => {
      animIdRef.current = null;
      if (!startTimeRef.current) {
        startTimeRef.current = timestamp;
      }
      ctx.clearRect(0, 0, canvas.width, canvas.height);

      sparksRef.current = sparksRef.current.filter(spark => {
        const elapsed = timestamp - spark.startTime;
        if (elapsed >= duration) {
          return false;
        }

        const progress = elapsed / duration;
        const eased = easeFunc(progress);

        const distance = eased * sparkRadius * extraScale;
        const lineLength = sparkSize * (1 - eased);

        const x1 = spark.x + distance * Math.cos(spark.angle);
        const y1 = spark.y + distance * Math.sin(spark.angle);
        const x2 = spark.x + (distance + lineLength) * Math.cos(spark.angle);
        const y2 = spark.y + (distance + lineLength) * Math.sin(spark.angle);

        ctx.strokeStyle = sparkColor;
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.moveTo(x1, y1);
        ctx.lineTo(x2, y2);
        ctx.stroke();

        return true;
      });

      // MASON MOD M-ClickSpark-1: keep drawing only while sparks are alive.
      if (sparksRef.current.length) animIdRef.current = requestAnimationFrame(draw);
      else if (typeof onIdleRef.current === 'function') onIdleRef.current();
    };
    drawRef.current = draw;
    if (sparksRef.current.length) startLoop();

    return () => {
      if (animIdRef.current != null) cancelAnimationFrame(animIdRef.current);
      animIdRef.current = null;
      drawRef.current = null;
    };
  }, [sparkColor, sparkSize, sparkRadius, sparkCount, duration, easeFunc, extraScale, startLoop]);

  // MASON MOD M-ClickSpark-2: shared by click and programmatic bursts. Skipped while the
  // document is hidden (nothing would be seen and rAF is paused anyway).
  const addSparks = useCallback(
    (x, y) => {
      if (typeof document !== 'undefined' && document.hidden) return false;
      const now = performance.now();
      const newSparks = Array.from({ length: sparkCount }, (_, i) => ({
        x,
        y,
        angle: (2 * Math.PI * i) / sparkCount,
        startTime: now
      }));
      sparksRef.current.push(...newSparks);
      startLoop();
      return true;
    },
    [sparkCount, startLoop]
  );

  const handleClick = e => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;
    addSparks(x, y);
  };

  useEffect(() => {
    if (!burst || typeof burst.x !== 'number' || typeof burst.y !== 'number') return;
    if (!addSparks(burst.x, burst.y) && typeof onIdleRef.current === 'function') onIdleRef.current();
  }, [burst, addSparks]);

  return (
    <div
      style={{
        position: 'relative',
        width: '100%',
        height: '100%'
      }}
      onClick={handleClick}
    >
      <canvas
        ref={canvasRef}
        style={{
          width: '100%',
          height: '100%',
          display: 'block',
          userSelect: 'none',
          position: 'absolute',
          top: 0,
          left: 0,
          pointerEvents: 'none'
        }}
      />
      {children}
    </div>
  );
};

export default ClickSpark;
