import { useEffect, useState } from 'react';
import pkg from '../../package.json';

const CURRENT = (pkg as { version: string }).version;
const REPO = 'keezxc1223/locwarp';
const API_URL = `https://api.github.com/repos/${REPO}/releases/latest`;

function parseVer(s: string): number[] {
  return s.replace(/^v/i, '').split('.').map((p) => parseInt(p, 10) || 0);
}

function isNewer(a: string, b: string): boolean {
  const x = parseVer(a);
  const y = parseVer(b);
  const n = Math.max(x.length, y.length);
  for (let i = 0; i < n; i++) {
    const xi = x[i] ?? 0;
    const yi = y[i] ?? 0;
    if (xi !== yi) return xi > yi;
  }
  return false;
}

export interface UpdateInfo {
  current: string;
  latest: string | null;
  releaseUrl: string | null;
}

/**
 * Hook: checks GitHub on mount for a newer release. Returns the latest tag
 * (or null if up-to-date / unreachable) plus a direct release URL. No
 * popup, no dismiss flow — caller decides how to surface the badge.
 */
export function useUpdateCheck(): UpdateInfo {
  const [latest, setLatest] = useState<string | null>(null);
  const [releaseUrl, setReleaseUrl] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await fetch(API_URL, {
          headers: { Accept: 'application/vnd.github+json' },
        });
        if (!r.ok) return;
        const data = await r.json();
        const tag: string | undefined = data?.tag_name;
        const html: string | undefined = data?.html_url;
        if (cancelled || !tag) return;
        if (!isNewer(tag, CURRENT)) return;
        setLatest(tag);
        setReleaseUrl(html || `https://github.com/${REPO}/releases/latest`);
      } catch {
        /* offline / rate-limited / DNS — silent */
      }
    })();
    return () => { cancelled = true; };
  }, []);

  return { current: CURRENT, latest, releaseUrl };
}
