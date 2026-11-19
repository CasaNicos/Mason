// React Bits: TextAnimations/CountUp @ c49d6978d2496660f0f0c5a3b3ca77a059566a93
import { useInView, useMotionValue, useSpring } from 'motion/react';
import { useCallback, useEffect, useRef } from 'react';
import { usePrefersReducedMotion } from '../../lib/motion.js'; // MASON MOD M-CountUp-1
import { onVisibility } from '../../lib/visibility.js'; // MASON MOD M-CountUp-2

// MASON MOD M-CountUp-3: values must be finite and |v| <= SAFE_MAX; anything else renders "—"
// (to) or starts at the target (from). Never prints NaN / ∞ / 1e+308.
const SAFE_MAX = 1e15;
const isSafe = v => typeof v === 'number' && Number.isFinite(v) && Math.abs(v) <= SAFE_MAX;

export default function CountUp({
  to: toRaw,
  from: fromRaw = 0,
  direction = 'up',
  delay = 0,
  duration = 2,
  className = '',
  startWhen = true,
  separator = '',
  onStart,
  onEnd
}) {
  // MASON MOD M-CountUp-3 (guard): a bad `to` shows "—" and never animates; a bad `from` starts at `to`.
  const bad = !isSafe(toRaw);
  const to = bad ? 0 : toRaw;
  const from = bad ? 0 : isSafe(fromRaw) ? fromRaw : to;
  const ref = useRef(null);
  const motionValue = useMotionValue(direction === 'down' ? to : from);

  // MASON MOD M-CountUp-3 (settle): upstream's spring (damping 20+40/d, stiffness 100/d) is heavily
  // overdamped and crawls for 3–6 s below display precision on 2-decimal values. Same ease-out look,
  // but critically damped so it is within 0.1% of the target at `duration` (ω·d ≈ 9.23), plus a
  // restDelta at display precision and a hard settle at delay+duration (see the start effect).
  const dur = Number.isFinite(duration) && duration > 0 ? duration : 2;
  const omega = 9.23 / dur;
  const springValue = useSpring(motionValue, {
    damping: 2 * omega,
    stiffness: omega * omega,
    restDelta: 0.5 * 10 ** -Math.min(6, Math.max(getDecimalsOf(from), getDecimalsOf(to)))
  });

  const isInView = useInView(ref, { once: true, margin: '0px' });
  const reduce = usePrefersReducedMotion(); // MASON MOD M-CountUp-1

  const getDecimalPlaces = num => {
    const str = num.toString();

    if (str.includes('.')) {
      const decimals = str.split('.')[1];

      if (parseInt(decimals) !== 0) {
        return decimals.length;
      }
    }

    return 0;
  };

  const maxDecimals = Math.max(getDecimalPlaces(from), getDecimalPlaces(to));

  const formatValue = useCallback(
    latest => {
      const hasDecimals = maxDecimals > 0;

      const options = {
        useGrouping: !!separator,
        minimumFractionDigits: hasDecimals ? maxDecimals : 0,
        maximumFractionDigits: hasDecimals ? maxDecimals : 0
      };

      const formattedNumber = Intl.NumberFormat('en-US', options).format(latest);

      return separator ? formattedNumber.replace(/,/g, separator) : formattedNumber;
    },
    [maxDecimals, separator]
  );

  useEffect(() => {
    if (ref.current) {
      ref.current.textContent = bad ? '—' : formatValue(direction === 'down' ? to : from); // M-CountUp-3
    }
  }, [from, to, direction, formatValue, bad]);

  useEffect(() => {
    if (bad) return undefined; // MASON MOD M-CountUp-3: nothing to animate
    if (isInView && startWhen) {
      if (typeof onStart === 'function') onStart();

      // MASON MOD M-CountUp-1: reduced motion → show the final value at once, no spring.
      if (reduce) {
        const target = direction === 'down' ? from : to;
        motionValue.jump(target);
        springValue.jump(target);
        if (ref.current) ref.current.textContent = formatValue(target);
        if (typeof onEnd === 'function') onEnd();
        return undefined;
      }

      const timeoutId = setTimeout(() => {
        motionValue.set(direction === 'down' ? from : to);
      }, delay * 1000);

      const durationTimeoutId = setTimeout(
        () => {
          // MASON MOD M-CountUp-3: hard settle at the intended end, so the loop always stops.
          const target = direction === 'down' ? from : to;
          if (springValue.get() !== target) springValue.jump(target);
          if (ref.current) ref.current.textContent = formatValue(target);
          if (typeof onEnd === 'function') onEnd();
        },
        delay * 1000 + dur * 1000
      );

      return () => {
        clearTimeout(timeoutId);
        clearTimeout(durationTimeoutId);
      };
    }
  }, [isInView, startWhen, motionValue, direction, from, to, delay, onStart, onEnd, dur, reduce, springValue, formatValue, bad]);

  useEffect(() => {
    const unsubscribe = springValue.on('change', latest => {
      if (bad) return; // M-CountUp-3
      if (ref.current) {
        ref.current.textContent = formatValue(latest);
      }
      // MASON MOD M-CountUp-2: the overdamped spring crawls for seconds below display precision
      // (~60 rAF/s after the number already reads final). Once the shown text equals the target's,
      // jump to the target so motion's frame loop goes idle.
      const target = motionValue.get();
      if (latest !== target && formatValue(latest) === formatValue(target)) springValue.jump(target);
    });

    // MASON MOD M-CountUp-2: tab hidden mid-count → finish now, so no spring runs while hidden.
    const offVis = onVisibility(hidden => {
      if (hidden && springValue.get() !== motionValue.get()) springValue.jump(motionValue.get());
    });

    return () => {
      unsubscribe();
      offVis();
    };
  }, [springValue, formatValue, motionValue, bad]);

  return <span className={className} ref={ref} />;
}

// MASON MOD M-CountUp-3: decimal count of a (safe) number, for the spring's restDelta.
function getDecimalsOf(num) {
  const str = String(num);
  if (!str.includes('.') || str.includes('e')) return 0;
  return str.split('.')[1].length;
}
