// Account scope from DATA (contracts §2.8, S3 brief §4): /api/rh/accounts rows carry
// scope: "personal" | "family". "Mine" = scope === "personal". No account name is ever hard-coded.
// Shared by rhAccounts / rhExposure / rhEarnings; useRhApi's per-route cache means this adds no
// second request when rhAccounts is on the same page.
import { useMemo } from 'react';
import { useRhApi } from '../../lib/api.js';

export const isFamily = row => !!row && String(row.scope || '').toLowerCase() === 'family';
export const isMine = row => !!row && String(row.scope || '').toLowerCase() === 'personal';

/** Plain helper for tests and widgets: rows → {list:[{account, scope}], family:Set<lowercased label>}. */
export function scopesFrom(rows) {
  const list = (Array.isArray(rows) ? rows : [])
    .filter(r => r && typeof r.account === 'string' && r.account)
    .map(r => ({ account: r.account, scope: isFamily(r) ? 'family' : isMine(r) ? 'personal' : 'unknown' }));
  return { list, family: new Set(list.filter(r => r.scope === 'family').map(r => r.account.toLowerCase())) };
}

/** React: {list, family, status}. `list` is empty until /api/rh/accounts answers (or when it is off). */
export function useAccountScopes() {
  const api = useRhApi('/api/rh/accounts', null, { pollMs: 60000 });
  const rows = api.status === 'ok' ? api.data : null;
  const s = useMemo(() => scopesFrom(rows), [rows]);
  return { ...s, status: api.status };
}

/** "<name> · family" style label for any account name, driven by the scope data. */
export function accountLabel(name, family) {
  const n = String(name == null ? '' : name);
  return family && family.has(n.toLowerCase()) ? `${n} · family` : n;
}
