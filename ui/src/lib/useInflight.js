// useInflight(): one-at-a-time guard for WRITE actions (POST / promote / save / skip).
//
//   const inflight = useInflight();
//   <button disabled={inflight.busy()} onClick={() => inflight.run(() => postRh(route, body))}>Save</button>
//   inflight.run(() => promote(hit), hit.id)        // keyed: one in flight per key
//
// run(fn, key = 'default'):
//   - checks a ref SYNCHRONOUSLY, so three clicks / requestSubmit()s in the same task call fn once
//     (React state alone can't stop that: it only updates after the task);
//   - returns undefined when a call for that key is already in flight, else fn's promise
//     (a synchronous throw becomes a rejected promise);
//   - clears the key in finally — after resolve, reject or throw.
// busy(key = 'default') → boolean, re-rendered on start/finish (use it to disable the control).
// DEBUG_F3 F3-1 / DEBUG_F5 F5-1 were exactly this bug class.
import { useCallback, useEffect, useRef, useState } from 'react';

export function useInflight() {
  const keys = useRef(new Set());
  const alive = useRef(true);
  const [, setVersion] = useState(0);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  const bump = useCallback(() => {
    if (alive.current) setVersion(v => v + 1);
  }, []);

  const run = useCallback(
    (fn, key = 'default') => {
      if (keys.current.has(key)) return undefined;
      keys.current.add(key);
      bump();
      let p;
      try {
        p = Promise.resolve(fn());
      } catch (e) {
        p = Promise.reject(e);
      }
      return p.finally(() => {
        keys.current.delete(key);
        bump();
      });
    },
    [bump]
  );

  const busy = useCallback(key => keys.current.has(key === undefined ? 'default' : key), []);

  return { run, busy };
}
