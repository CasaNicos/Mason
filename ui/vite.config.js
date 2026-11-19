// Mason UI bundle build (spec: docs/ui/REACT_BITS_BUNDLE_DESIGN.md §2-§5).
//
// Output (../static/ui/, committed, served by Flask's static route):
//   mason-ui.js          the entry: window.MasonUI (React + motion + gsap + lib)
//   mason-ui.css         ALL CSS (theme + React Bits + widgets), one file, no runtime CSS injection
//   chunks/<name>-<hash>.js   one lazy chunk per widget (widgets/<type>/index.jsx) and per React Bits component
//   harness.html + harness/*  the mock-mode test surface (emitted verbatim, loads ./mason-ui.js like the template)
//   fixtures/<slug>.json      copied from ui/fixtures/ for ?rhx_mock=1
//   manifest.json             read by scripts/budget.mjs
//
// Determinism: file names are content hashes (or fixed), emitted assets are sorted, nothing
// time-based is written. scripts/build-twice.mjs proves two clean builds are byte-identical.
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.dirname(fileURLToPath(import.meta.url));
const OUT = path.resolve(ROOT, '../static/ui');
const REPO = path.resolve(ROOT, '..');

// Scope every React Bits stylesheet under `.mui-root` at build time so its generic class names
// (.item, .card, .bottom-gradient, .outer-container, ...) can never restyle the vanilla template.
// `:where()` keeps the upstream specificity unchanged, so theme.css overrides still win.
// The copied .css files stay byte-identical to upstream (bar the provenance header). See MODS.md.
const SCOPE = ':where(.mui-root)';
function scopeSelector(sel) {
  return sel
    .split(',')
    .map(s => {
      const t = s.trim();
      if (!t) return t;
      if (t.startsWith(SCOPE)) return t;
      if (/^(html|body|:root)\b/.test(t)) return t.replace(/^(html|body|:root)/, SCOPE);
      return `${SCOPE} ${t}`;
    })
    .join(', ');
}
const scopeReactBitsCss = () => ({
  postcssPlugin: 'mason-scope-reactbits',
  Once(root, { result }) {
    const file = (result.opts && result.opts.from) || '';
    if (!file.split(path.sep).join('/').includes('/src/reactbits/')) return;
    root.walkRules(rule => {
      const parent = rule.parent;
      if (parent && parent.type === 'atrule' && /keyframes$/i.test(parent.name)) return;
      rule.selector = scopeSelector(rule.selector);
    });
  }
});
scopeReactBitsCss.postcss = true;

// Emit the harness (verbatim) and the fixtures into the build so they are part of static/ui.
function masonAssets() {
  const listSorted = dir =>
    fs.existsSync(dir) ? fs.readdirSync(dir).filter(f => !f.startsWith('.')).sort() : [];
  return {
    name: 'mason-assets',
    apply: 'build',
    generateBundle() {
      this.emitFile({
        type: 'asset',
        fileName: 'harness.html',
        source: fs.readFileSync(path.join(ROOT, 'harness.html'), 'utf8')
      });
      // classic script that must run before any module chunk (DEBUG_F0 F1); same source the bundle imports
      this.emitFile({
        type: 'asset',
        fileName: 'debug-boot.js',
        source: fs.readFileSync(path.join(ROOT, 'src/lib/debug-boot.js'), 'utf8')
      });
      for (const f of listSorted(path.join(ROOT, 'harness'))) {
        this.emitFile({
          type: 'asset',
          fileName: `harness/${f}`,
          source: fs.readFileSync(path.join(ROOT, 'harness', f), 'utf8')
        });
      }
      for (const f of listSorted(path.join(ROOT, 'fixtures')).filter(f => f.endsWith('.json'))) {
        const raw = fs.readFileSync(path.join(ROOT, 'fixtures', f), 'utf8');
        JSON.parse(raw); // a broken fixture fails the build, not the harness
        this.emitFile({ type: 'asset', fileName: `fixtures/${f}`, source: raw });
      }
    }
  };
}

// chunks/<widget type | React Bits name | module name>-<hash>.js
function chunkName(info) {
  const id = (info.facadeModuleId || '').split(path.sep).join('/');
  let m = id.match(/\/src\/widgets\/([^/]+)\/index\.jsx$/);
  if (m) return `chunks/w-${m[1]}-[hash].js`;
  m = id.match(/\/src\/reactbits\/([^/]+)\/[^/]+\.jsx$/);
  if (m) return `chunks/rb-${m[1]}-[hash].js`;
  return 'chunks/[name]-[hash].js';
}

export default defineConfig({
  root: ROOT,
  base: './',
  publicDir: false,
  plugins: [react(), masonAssets()],
  css: { postcss: { plugins: [scopeReactBitsCss()] } },
  resolve: {
    // Debugger-owned tests live outside ui/ (Tests/ui, Tests/debug): resolve shared deps from here.
    dedupe: ['react', 'react-dom', 'motion', 'gsap', '@testing-library/react', '@testing-library/dom']
  },
  build: {
    outDir: OUT,
    emptyOutDir: true,
    target: 'es2022',
    minify: true,
    sourcemap: false,
    cssCodeSplit: false,
    modulePreload: false,
    manifest: 'manifest.json',
    reportCompressedSize: false,
    rolldownOptions: {
      input: { 'mason-ui': path.join(ROOT, 'src/entry.js') },
      preserveEntrySignatures: 'exports-only',
      output: {
        // Vendor groups: React, motion and gsap are each one cacheable chunk, loaded with the entry
        // (bundle §5 "the entry (React + motion + gsap + lib)"), instead of being sliced by importer.
        advancedChunks: {
          groups: [
            { name: 'vendor-react', test: /[\\/]node_modules[\\/](react|react-dom|scheduler)[\\/]/ },
            { name: 'vendor-motion', test: /[\\/]node_modules[\\/](motion|framer-motion|motion-dom|motion-utils)[\\/]/ },
            { name: 'vendor-gsap', test: /[\\/]node_modules[\\/]gsap[\\/]/ }
          ]
        },
        entryFileNames: '[name].js',
        chunkFileNames: chunkName,
        assetFileNames: a => (a.names && a.names.some(n => n.endsWith('.css')) ? 'mason-ui.css' : 'assets/[name]-[hash][extname]')
      }
    }
  },
  server: { fs: { allow: [REPO] } },
  test: {
    environment: 'jsdom',
    globals: false,
    setupFiles: [path.join(ROOT, 'test/setup.js')],
    include: [
      'test/**/*.test.{js,jsx}',
      '../Tests/ui/**/*.test.{js,jsx}',
      '../Tests/debug/**/*.test.{js,jsx}'
    ],
    css: false,
    restoreMocks: true
  }
});
