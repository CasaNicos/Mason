#!/usr/bin/env node
// Reproducible-build check (bundle design §5): two clean builds must produce identical bytes.
// Hashes every file under static/ui after each `npm run build` and compares the trees.
//   npm run build:check            build twice, compare build 1 vs build 2
//   npm run build:check -- --strict   also require build 1 == the static/ui that existed before (the committed tree)
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { execSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const UI = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const OUT = path.resolve(UI, '../static/ui');

function hashTree(dir) {
  const out = {};
  const walk = d => {
    if (!fs.existsSync(d)) return;
    for (const e of fs.readdirSync(d, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
      const p = path.join(d, e.name);
      if (e.isDirectory()) walk(p);
      else out[path.relative(dir, p).split(path.sep).join('/')] = crypto.createHash('sha256').update(fs.readFileSync(p)).digest('hex');
    }
  };
  walk(dir);
  return out;
}
const treeHash = t => crypto.createHash('sha256').update(Object.entries(t).map(([k, v]) => `${v}  ${k}\n`).join('')).digest('hex');
function diff(a, b) {
  const keys = [...new Set([...Object.keys(a), ...Object.keys(b)])].sort();
  return keys.filter(k => a[k] !== b[k]).map(k => `${k}: ${a[k] ? a[k].slice(0, 12) : 'MISSING'} vs ${b[k] ? b[k].slice(0, 12) : 'MISSING'}`);
}
const build = () => execSync('npm run build --silent', { cwd: UI, stdio: ['ignore', 'ignore', 'inherit'] });

const strict = process.argv.includes('--strict');
const before = hashTree(OUT);
build();
const one = hashTree(OUT);
build();
const two = hashTree(OUT);

console.log(`files: ${Object.keys(two).length}`);
console.log(`build 1 tree sha256: ${treeHash(one)}`);
console.log(`build 2 tree sha256: ${treeHash(two)}`);
let fail = false;
const d12 = diff(one, two);
if (d12.length) {
  fail = true;
  console.error(`NOT REPRODUCIBLE: ${d12.length} file(s) differ between two clean builds:\n  ${d12.join('\n  ')}`);
}
if (Object.keys(before).length) {
  const d01 = diff(before, one);
  console.log(`pre-existing static/ui ${d01.length ? `DIFFERS from the fresh build (${d01.length} files)` : 'matches the fresh build'} (tree ${treeHash(before)})`);
  if (d01.length && strict) {
    fail = true;
    console.error(`  ${d01.join('\n  ')}`);
  }
}
if (fail) process.exit(1);
console.log('build-twice: byte-identical');
