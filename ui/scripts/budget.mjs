#!/usr/bin/env node
// Size budgets (bundle design §5), run as the last step of `npm run build`; exits 1 when over.
//   entry   = mason-ui.js + everything it statically imports (what every page pays): <= 180 KB gzip
//   chunks  = every lazy chunk under chunks/ (widgets w-*, React Bits rb-*, shared):  <=  40 KB gzip each
// Sizes are gzip -9 of the built bytes, read via static/ui/manifest.json.
import fs from 'node:fs';
import path from 'node:path';
import zlib from 'node:zlib';
import { fileURLToPath } from 'node:url';

export const ENTRY_BUDGET = 180 * 1024;
export const CHUNK_BUDGET = 40 * 1024;
const UI = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const OUT = path.resolve(UI, '../static/ui');

const gz = f => zlib.gzipSync(fs.readFileSync(path.join(OUT, f)), { level: 9 }).length;
const kb = n => `${(n / 1024).toFixed(1)} KB`;

export function measure() {
  const manifest = JSON.parse(fs.readFileSync(path.join(OUT, 'manifest.json'), 'utf8'));
  const entryKey = Object.keys(manifest).find(k => manifest[k].isEntry);
  if (!entryKey) throw new Error('manifest has no entry');
  const closure = new Set();
  const walk = k => {
    if (closure.has(k) || !manifest[k]) return;
    closure.add(k);
    (manifest[k].imports || []).forEach(walk);
  };
  walk(entryKey);
  const entryFiles = [...closure].map(k => manifest[k].file);
  const entry = { files: entryFiles, gzip: entryFiles.reduce((a, f) => a + gz(f), 0) };
  const chunks = [];
  for (const [k, v] of Object.entries(manifest)) {
    if (closure.has(k) || !v.file || !v.file.endsWith('.js')) continue;
    const m = k.match(/src\/widgets\/([^/]+)\/index\.jsx$/);
    const r = k.match(/src\/reactbits\/([^/]+)\/[^/]+\.jsx$/);
    chunks.push({ key: k, file: v.file, kind: m ? 'widget' : r ? 'reactbits' : 'shared', name: m ? m[1] : r ? r[1] : v.name || k, gzip: gz(v.file) });
  }
  chunks.sort((a, b) => a.file.localeCompare(b.file));
  const css = fs.existsSync(path.join(OUT, 'mason-ui.css')) ? gz('mason-ui.css') : 0;
  return { entry, chunks, css };
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const { entry, chunks, css } = measure();
  let fail = false;
  const ok = (n, b) => (n <= b ? 'ok ' : ((fail = true), 'OVER'));
  console.log('\nbundle budgets (gzip -9)');
  console.log(`  ${ok(entry.gzip, ENTRY_BUDGET)} entry ${kb(entry.gzip).padStart(9)} / ${kb(ENTRY_BUDGET)}  (${entry.files.join(' + ')})`);
  for (const c of chunks) console.log(`  ${ok(c.gzip, CHUNK_BUDGET)} ${c.kind.padEnd(9)} ${kb(c.gzip).padStart(9)} / ${kb(CHUNK_BUDGET)}  ${c.file}`);
  console.log(`  info css   ${kb(css).padStart(9)}  mason-ui.css (no budget)`);
  if (fail) {
    console.error('\nBUDGET FAIL: a bundle is over its gzip budget (docs/ui/REACT_BITS_BUNDLE_DESIGN.md §5)');
    process.exit(1);
  }
  console.log('budgets: OK');
}
