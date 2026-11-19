#!/usr/bin/env node
// React Bits provenance check (bundle design §5-§7, S3 §4 build checks).
//  - every §7 component is present in src/reactbits/<Name>/ exactly once
//  - every copied file starts with the provenance header for the pinned upstream sha
//  - every file that differs from upstream (header line ignored) is named in MODS.md as
//    "<Name>/<File>" (e.g. `ElectricBorder/ElectricBorder.jsx`)
//  - no React Bits header appears anywhere else under src/ (= copied twice)
// Upstream: $RB_SRC or ~/gitpulls/react-bits/src/content.  Usage: npm run rb:diff
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { fileURLToPath } from 'node:url';

export const RB_SHA = 'c49d6978d2496660f0f0c5a3b3ca77a059566a93';
export const RB_SET = [
  'Components/AnimatedList', 'Components/Counter', 'Components/SpotlightCard', 'Components/Stepper',
  'Components/Stack', 'Components/GlassSurface', 'Components/Dock', 'Animations/ElectricBorder',
  'Animations/PixelTransition', 'Animations/StarBorder', 'Animations/LogoLoop', 'Animations/ClickSpark',
  'TextAnimations/CountUp', 'TextAnimations/ShinyText', 'TextAnimations/DecryptedText'
];
const UI = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const RB_DIR = path.join(UI, 'src', 'reactbits');
const UPSTREAM = process.env.RB_SRC || path.join(os.homedir(), 'gitpulls', 'react-bits', 'src', 'content');
const HEADER_RE = /^(\/\/|\/\*) React Bits: ([A-Za-z]+\/[A-Za-z]+) @ ([0-9a-f]{40})( \*\/)?$/;

function walk(dir, out = []) {
  if (!fs.existsSync(dir)) return out;
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) walk(p, out);
    else out.push(p);
  }
  return out;
}

export function rbDiff() {
  const mods = fs.existsSync(path.join(RB_DIR, 'MODS.md')) ? fs.readFileSync(path.join(RB_DIR, 'MODS.md'), 'utf8') : '';
  const res = { files: [], missing: [], duplicates: [], badHeader: [], differs: [], unlogged: [], upstreamMissing: false };
  const names = RB_SET.map(p => p.split('/')[1]);
  // duplicates: same component dir twice (case-insensitive) or RB header outside src/reactbits
  const dirs = fs.existsSync(RB_DIR) ? fs.readdirSync(RB_DIR).filter(d => fs.statSync(path.join(RB_DIR, d)).isDirectory()) : [];
  const seen = new Map();
  for (const d of dirs) {
    const k = d.toLowerCase();
    if (seen.has(k)) res.duplicates.push(`${seen.get(k)} + ${d}`);
    seen.set(k, d);
  }
  for (const f of walk(path.join(UI, 'src'))) {
    if (f.startsWith(RB_DIR + path.sep)) continue;
    const first = fs.readFileSync(f, 'utf8').split('\n', 1)[0];
    if (/React Bits: [A-Za-z]+\/[A-Za-z]+ @/.test(first)) res.duplicates.push(path.relative(UI, f));
  }
  for (const rel of RB_SET) {
    const name = rel.split('/')[1];
    const up = path.join(UPSTREAM, rel);
    const local = path.join(RB_DIR, name);
    if (!fs.existsSync(local)) {
      res.missing.push(name);
      continue;
    }
    if (!fs.existsSync(up)) res.upstreamMissing = true;
    const upFiles = fs.existsSync(up) ? fs.readdirSync(up).sort() : [];
    const localFiles = fs.readdirSync(local).sort();
    for (const uf of upFiles) if (!localFiles.includes(uf)) res.missing.push(`${name}/${uf}`);
    for (const lf of localFiles) {
      const lp = path.join(local, lf);
      const text = fs.readFileSync(lp, 'utf8');
      const [first, ...rest] = text.split('\n');
      const m = first.match(HEADER_RE);
      if (!m || m[2] !== rel || m[3] !== RB_SHA) res.badHeader.push(`${name}/${lf}`);
      const upPath = path.join(up, lf);
      const entry = { name, file: lf, local: lp, upstream: upPath, differs: false };
      if (fs.existsSync(upPath)) {
        entry.differs = rest.join('\n') !== fs.readFileSync(upPath, 'utf8');
      } else {
        entry.differs = true; // a file we added next to the component
      }
      if (entry.differs) {
        res.differs.push(`${name}/${lf}`);
        if (!mods.includes(`${name}/${lf}`)) res.unlogged.push(`${name}/${lf}`);
      }
      res.files.push(entry);
    }
  }
  void names;
  return res;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const r = rbDiff();
  console.log(`React Bits @ ${RB_SHA} (upstream: ${UPSTREAM}${r.upstreamMissing ? ' — MISSING' : ''})`);
  console.log(`files: ${r.files.length}; modified vs upstream: ${r.differs.length}`);
  for (const d of r.differs) console.log(`  modified: ${d}${r.unlogged.includes(d) ? '   <-- NOT IN MODS.md' : ''}`);
  const bad = [
    ['missing', r.missing], ['duplicates', r.duplicates], ['bad provenance header', r.badHeader], ['modified but not in MODS.md', r.unlogged]
  ].filter(([, v]) => v.length);
  for (const [k, v] of bad) console.log(`FAIL ${k}: ${v.join(', ')}`);
  if (bad.length || r.upstreamMissing) process.exit(1);
  console.log('rb-diff: OK');
}
