// ESLint (flat config). Bundle design §4/§5: react/no-danger is an ERROR; eval / new Function too.
// Only eslint + eslint-plugin-react are allowed (no @eslint/js, no `globals` package), so the
// globals are listed inline below.
import react from 'eslint-plugin-react';

const browser = Object.fromEntries(
  [
    'window', 'document', 'navigator', 'location', 'performance', 'console', 'fetch', 'URL', 'URLSearchParams',
    'AbortController', 'CustomEvent', 'Event', 'MouseEvent', 'KeyboardEvent', 'HTMLElement', 'HTMLCanvasElement', 'Element', 'Node',
    'ResizeObserver', 'IntersectionObserver', 'MutationObserver', 'requestAnimationFrame',
    'cancelAnimationFrame', 'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'queueMicrotask',
    'getComputedStyle', 'matchMedia', 'history', 'localStorage', 'sessionStorage', 'Intl', 'Promise', 'Map', 'Set',
    'WeakMap', 'Symbol', 'Proxy', 'Reflect', 'globalThis', 'GridStack', 'structuredClone', 'CSS', 'btoa', 'atob',
    'Blob', 'Image', 'DOMParser', 'XMLSerializer', 'SVGElement', 'getSelection'
  ].map(k => [k, 'readonly'])
);
const node = Object.fromEntries(['process', 'Buffer', '__dirname', 'console'].map(k => [k, 'readonly']));
const vitest = Object.fromEntries(['describe', 'it', 'expect', 'vi', 'beforeAll', 'beforeEach', 'afterAll', 'afterEach'].map(k => [k, 'readonly']));

// Upstream React Bits files carry `eslint-disable-next-line react-hooks/exhaustive-deps`
// comments. We don't ship eslint-plugin-react-hooks (not on the allowlist), so register a no-op
// rule under that name to keep those directives valid without editing upstream code.
const reactHooksStub = {
  rules: {
    'exhaustive-deps': { meta: { type: 'suggestion', schema: [] }, create: () => ({}) },
    'rules-of-hooks': { meta: { type: 'problem', schema: [] }, create: () => ({}) }
  }
};

export default [
  { ignores: ['node_modules/**', '../static/**', 'dist/**', 'test/.tmp-*/**'] },
  {
    files: ['**/*.{js,jsx,mjs}'],
    plugins: { react, 'react-hooks': reactHooksStub },
    languageOptions: {
      ecmaVersion: 2023,
      sourceType: 'module',
      parserOptions: { ecmaFeatures: { jsx: true } },
      globals: { ...browser }
    },
    settings: { react: { version: '19.3' } },
    linterOptions: { reportUnusedDisableDirectives: 'off' },
    rules: {
      // security (bundle §5) — these are the non-negotiables
      'react/no-danger': 'error',
      'react/no-danger-with-children': 'error',
      'no-eval': 'error',
      'no-implied-eval': 'error',
      'no-new-func': 'error',
      'no-script-url': 'error',
      'react/jsx-no-script-url': 'error',
      'react/jsx-no-target-blank': 'error',
      // correctness
      'no-undef': 'error',
      'react/jsx-no-undef': 'error',
      'react/jsx-uses-vars': 'error',
      'react/jsx-key': 'error',
      'no-unused-vars': ['warn', { args: 'none', varsIgnorePattern: '^_', caughtErrors: 'none' }]
    }
  },
  {
    files: ['scripts/**/*.mjs', 'vite.config.js', 'eslint.config.js'],
    languageOptions: { globals: { ...browser, ...node } }
  },
  {
    files: ['test/**/*.{js,jsx}'],
    languageOptions: { globals: { ...browser, ...node, ...vitest } }
  },
  {
    // upstream React Bits code: only the security rules matter here; don't nag about style
    files: ['src/reactbits/**/*.jsx'],
    rules: { 'no-unused-vars': 'off', 'react/jsx-key': 'off' }
  }
];
