#!/usr/bin/env node
// Banned-construct grep (bundle design §5 "Security"): fails the build when ui/src contains
// dangerouslySetInnerHTML, eval( or new Function. Runs first in `npm run build`.
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const RULES = [
  ['dangerouslySetInnerHTML', /dangerouslySetInnerHTML/],
  ['eval(', /(^|[^A-Za-z0-9_$])eval\s*\(/],
  ['new Function', /\bnew\s+Function\s*\(/]
];

export function scanBanned(dir) {
  const hits = [];
  const walk = d => {
    if (!fs.existsSync(d)) return;
    for (const e of fs.readdirSync(d, { withFileTypes: true })) {
      const p = path.join(d, e.name);
      if (e.isDirectory()) walk(p);
      else if (/\.(jsx?|mjs|cjs|tsx?|html)$/.test(e.name)) {
        fs.readFileSync(p, 'utf8').split('\n').forEach((line, i) => {
          for (const [rule, re] of RULES) if (re.test(line)) hits.push({ rule, file: p, line: i + 1, text: line.trim() });
        });
      }
    }
  };
  walk(dir);
  return hits;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const UI = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  const hits = [...scanBanned(path.join(UI, 'src')), ...scanBanned(path.join(UI, 'harness'))];
  if (hits.length) {
    for (const h of hits) console.error(`BANNED ${h.rule}: ${path.relative(UI, h.file)}:${h.line}: ${h.text}`);
    process.exit(1);
  }
  console.log('check-src: no dangerouslySetInnerHTML / eval( / new Function under ui/src, ui/harness');
}
