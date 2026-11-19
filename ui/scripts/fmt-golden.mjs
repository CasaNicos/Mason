#!/usr/bin/env node
// Writes test/fixtures/fmt_golden.json: lib/fmt.js output over a wide input grid. Generated ONCE
// from the pre-cache implementation (F0b); test/fmt_cache.test.js asserts byte-identical output.
import fs from 'node:fs';
import * as f from '../src/lib/fmt.js';

const nums = [0, -0, 1, -1, 0.004, 0.005, 0.0049999, -0.005, 0.015, 1.005, 2.675, 12.345, -12.345, 999.995, 999.994,
  1000, 1234.5, -1234.5, 99999.999, 1e6, 1530000, -2500, 987654321.987, 1e9, 1e12, 1.23456789e14, 1e15, 9.99e15,
  1e20, 1e300, 1e308, -1e308, Number.MAX_SAFE_INTEGER, Number.MIN_VALUE, 1e-7, -1e-7, 3.14159, NaN, Infinity,
  -Infinity, null, undefined, '12', {}, []];
const dates = ['2026-09-30', '2026-09-30T14:05:00Z', '2026-01-01T00:00:00Z', 'bad', null, 0, 1759240000000, '1999-12-31'];
const out = [];
const rec = (fn, args) => out.push([fn, JSON.stringify(args, (k, v) => (Object.is(v, -0) ? '__-0' : typeof v === 'number' && !Number.isFinite(v) ? '__' + String(v) : v === undefined ? '__u' : v)), f[fn](...args)]);
for (const v of nums) {
  rec('money', [v]); rec('money', [v, { signed: true }]); rec('money', [v, { compact: true }]); rec('money', [v, { signed: true, compact: true }]);
  for (const d of [0, 1, 2, 3]) { rec('pct', [v, { digits: d }]); rec('pct', [v, { digits: d, signed: false }]); rec('signed', [v, d]); rec('num', [v, d]); }
  rec('arrow', [v]); rec('dirClass', [v]); rec('nLabel', [v]);
}
for (const d of dates) { rec('date', [d, { utc: true }]); rec('date', [d]); rec('dateTime', [d, { utc: true }]); }
fs.writeFileSync(new URL('../test/fixtures/fmt_golden.json', import.meta.url), JSON.stringify(out, null, 0) + '\n');
console.log(`fmt golden: ${out.length} cases`);
