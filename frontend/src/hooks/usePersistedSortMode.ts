import { useState } from 'react';

// Sort mode persisted in localStorage so it survives restart.
export function usePersistedSortMode<M extends string>(
  storageKey: string,
  validModes: readonly M[],
  fallback: M,
): [M, (m: M) => void] {
  const [sortMode, setSortModeRaw] = useState<M>(() => {
    try {
      const v = localStorage.getItem(storageKey) as M | null;
      if (v !== null && validModes.includes(v)) return v;
    } catch { /* ignore */ }
    return fallback;
  });
  const setSortMode = (m: M) => {
    setSortModeRaw(m);
    try { localStorage.setItem(storageKey, m); } catch { /* ignore */ }
  };
  return [sortMode, setSortMode];
}
