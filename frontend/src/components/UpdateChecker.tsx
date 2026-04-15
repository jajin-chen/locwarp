import React, { useEffect, useState } from 'react';
import { createPortal } from 'react-dom';
import pkg from '../../package.json';
import { useT } from '../i18n';

const CURRENT = (pkg as { version: string }).version;
const REPO = 'keezxc1223/locwarp';
const RELEASES_URL = `https://github.com/${REPO}/releases`;
const API_URL = `https://api.github.com/repos/${REPO}/releases/latest`;
// Don't nag more than once per 6 h per unique newer version.
const COOLDOWN_MS = 6 * 60 * 60 * 1000;
const DISMISS_KEY = 'locwarp.update_check.dismissed';

function parseVer(s: string): number[] {
  return s.replace(/^v/i, '').split('.').map((p) => parseInt(p, 10) || 0);
}

/** Returns true if `a` is strictly newer than `b`. */
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

/**
 * Checks GitHub on mount for a newer release; shows a dismissible dialog
 * when one is found. Silent when already on the latest version or when
 * the network / API is unreachable. User-dismissed versions are cached
 * in localStorage for 6 hours to avoid nagging.
 */
const UpdateChecker: React.FC = () => {
  const t = useT();
  const [latest, setLatest] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        // Previously dismissed the same version recently?
        try {
          const raw = localStorage.getItem(DISMISS_KEY);
          if (raw) {
            const { version, at } = JSON.parse(raw);
            if (typeof version === 'string' && typeof at === 'number' &&
                Date.now() - at < COOLDOWN_MS) {
              // Only suppress if the dismissed version is still the latest
              // we know of; if a *newer* version than that appears, show again.
              // We don't know yet — fetch and compare below.
              var dismissedVersion: string | null = version;
              var dismissedAt: number = at;
            }
          }
        } catch { /* ignore malformed cache */ }

        const r = await fetch(API_URL, {
          headers: { Accept: 'application/vnd.github+json' },
        });
        if (!r.ok) return;
        const data = await r.json();
        const tag: string | undefined = data?.tag_name;
        if (!tag || cancelled) return;
        if (!isNewer(tag, CURRENT)) return;

        // If user already dismissed THIS version within cooldown, stay quiet.
        // (But a brand-new tag beyond the dismissed one will show.)
        // @ts-ignore — defined conditionally above
        if (typeof dismissedVersion !== 'undefined' && dismissedVersion !== null) {
          // @ts-ignore
          if (parseVer(tag).join('.') === parseVer(dismissedVersion).join('.')) {
            return;
          }
        }
        setLatest(tag);
      } catch {
        // Offline / rate-limited / DNS — silent.
      }
    })();
    return () => { cancelled = true; };
  }, []);

  if (!latest) return null;

  const dismiss = () => {
    try {
      localStorage.setItem(DISMISS_KEY, JSON.stringify({
        version: latest, at: Date.now(),
      }));
    } catch { /* storage disabled */ }
    setLatest(null);
  };

  return createPortal(
    <div
      className="anim-fade-in"
      onClick={dismiss}
      style={{
        position: 'fixed', top: 0, left: 0, right: 0, bottom: 0,
        background: 'rgba(8, 10, 20, 0.55)',
        backdropFilter: 'blur(4px)', WebkitBackdropFilter: 'blur(4px)',
        zIndex: 2000,
        display: 'flex', alignItems: 'center', justifyContent: 'center',
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="anim-scale-in"
        style={{
          background: 'rgba(26, 29, 39, 0.96)',
          backdropFilter: 'blur(14px)', WebkitBackdropFilter: 'blur(14px)',
          border: '1px solid rgba(108, 140, 255, 0.25)',
          borderRadius: 12, padding: 22, width: 360, color: '#e0e0e0',
          boxShadow: '0 20px 60px rgba(12, 18, 40, 0.65), 0 0 0 1px rgba(255,255,255,0.05) inset',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 14 }}>
          <div
            style={{
              width: 32, height: 32, borderRadius: 8,
              background: 'linear-gradient(135deg, #6c8cff, #4285f4)',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              flexShrink: 0,
            }}
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2.5">
              <path d="M12 2v13M5 9l7-7 7 7" />
              <path d="M5 21h14" />
            </svg>
          </div>
          <div style={{ fontSize: 15, fontWeight: 600, flex: 1 }}>
            {t('update.title')}
          </div>
        </div>

        <div style={{ fontSize: 12.5, lineHeight: 1.7, marginBottom: 14 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between' }}>
            <span style={{ opacity: 0.65 }}>{t('update.current')}</span>
            <span style={{ fontFamily: 'monospace' }}>v{CURRENT}</span>
          </div>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <span style={{ opacity: 0.65 }}>{t('update.latest')}</span>
            <a
              href={RELEASES_URL}
              target="_blank"
              rel="noreferrer"
              style={{
                fontFamily: 'monospace', color: '#6c8cff',
                textDecoration: 'none', fontWeight: 600,
              }}
              onClick={(e) => {
                // Electron webview: intercept and open externally if possible.
                try {
                  const anyWin: any = window;
                  if (anyWin.locwarp?.openExternal) {
                    e.preventDefault();
                    anyWin.locwarp.openExternal(RELEASES_URL);
                  }
                } catch { /* default browser nav */ }
              }}
            >
              {latest} ↗
            </a>
          </div>
        </div>

        <div style={{ fontSize: 12, opacity: 0.75, marginBottom: 16, lineHeight: 1.6 }}>
          {t('update.go_to_github')}
        </div>

        <div style={{ display: 'flex', gap: 8 }}>
          <a
            href={RELEASES_URL}
            target="_blank"
            rel="noreferrer"
            className="action-btn primary"
            style={{
              flex: 1, textAlign: 'center', textDecoration: 'none',
              display: 'inline-flex', alignItems: 'center', justifyContent: 'center', gap: 6,
            }}
            onClick={(e) => {
              try {
                const anyWin: any = window;
                if (anyWin.locwarp?.openExternal) {
                  e.preventDefault();
                  anyWin.locwarp.openExternal(RELEASES_URL);
                }
              } catch { /* default */ }
            }}
          >
            {t('update.download')}
          </a>
          <button className="action-btn" onClick={dismiss}>
            {t('update.later')}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
};

export default UpdateChecker;
