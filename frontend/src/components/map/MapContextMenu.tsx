import React, { useLayoutEffect, useRef, useState } from 'react';
import { reverseGeocode } from '../../services/api';
import { contextMenuItemStyle, highlightItem, unhighlightItem } from './menuStyles';
import type { TRef } from './types';

// Right-click context menu: coordinate header (with what's-here reverse
// geocode), teleport / navigate (device-gated), copy coords, add
// bookmark, and (route modes only) add waypoint. Rendered only while the
// menu is open, so per-open state (clamped paint position, reverse-geo
// result) naturally resets on unmount.
function MapContextMenu({
  x,
  y,
  lat,
  lng,
  deviceConnected,
  showWaypointOption,
  onTeleport,
  onNavigate,
  onAddBookmark,
  onAddWaypoint,
  onShowToast,
  t,
  tRef,
  onClose,
}: {
  x: number;
  y: number;
  lat: number;
  lng: number;
  deviceConnected: boolean;
  showWaypointOption?: boolean;
  onTeleport: (lat: number, lng: number, source?: 'menu' | 'coord') => void;
  onNavigate: (lat: number, lng: number, source?: 'menu' | 'coord') => void;
  onAddBookmark: (lat: number, lng: number) => void;
  onAddWaypoint?: (lat: number, lng: number) => void;
  onShowToast?: (msg: string) => void;
  t: (k: any, v?: any) => string;
  tRef: TRef;
  onClose: () => void;
}) {
  // DOM ref + clamped position state. Separate states for "click point" and
  // "where the menu is actually painted" — the paint position is set ONCE
  // per open via useLayoutEffect after measuring the real rendered size.
  // Critical: the layout effect's deps do NOT include contextMenuPos itself,
  // otherwise the setState triggers the effect again and we get an infinite
  // reposition loop (that was v0.2.38's bug).
  const contextMenuElRef = useRef<HTMLDivElement | null>(null);
  const [contextMenuPos, setContextMenuPos] = useState<{ left: number; top: number } | null>(null);
  useLayoutEffect(() => {
    const el = contextMenuElRef.current;
    if (!el) return;
    const { width, height } = el.getBoundingClientRect();
    const margin = 8;
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    // Clamp: prefer opening rightward / downward, but if that overflows,
    // push the menu back in so it never clips the viewport edge.
    const left = Math.max(margin, Math.min(x, vw - width - margin));
    const top  = Math.max(margin, Math.min(y, vh - height - margin));
    setContextMenuPos({ left, top });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [x, y]);

  // Reverse-geocode state for the context menu header row. Keyed by the
  // right-click target so a stale address from a previous click never
  // leaks into a new lookup; unmounting on close resets everything.
  const [reverseGeo, setReverseGeo] = useState<{
    loading: boolean; address: string | null; error: string | null;
    key: string; // lat|lng the result belongs to
  }>({ loading: false, address: null, error: null, key: '' });

  return (
    <div
      ref={contextMenuElRef}
      className="context-menu anim-scale-in-tl"
      style={{
        position: 'fixed',
        // First paint renders at the click point but invisible; the
        // layout-effect below measures the actual rendered size and
        // clamps left/top into the viewport, then flips visibility on.
        // Because the layout effect runs synchronously before the
        // browser paints, the user never sees the unclamped position.
        left: contextMenuPos ? contextMenuPos.left : x,
        top: contextMenuPos ? contextMenuPos.top : y,
        visibility: contextMenuPos ? 'visible' : 'hidden',
        zIndex: 1000,
        background: 'rgba(26, 29, 39, 0.95)',
        backdropFilter: 'blur(10px)',
        WebkitBackdropFilter: 'blur(10px)',
        border: '1px solid rgba(108, 140, 255, 0.18)',
        borderRadius: 10,
        padding: '4px 0',
        boxShadow: '0 10px 32px rgba(12, 18, 40, 0.55), 0 0 0 1px rgba(255, 255, 255, 0.04) inset',
        minWidth: 180,
        maxWidth: 'calc(100vw - 16px)',
        maxHeight: 'calc(100vh - 16px)',
        overflow: 'auto',
      }}
      onClick={(e) => e.stopPropagation()}
    >
      {/* 1. Coordinates label — always visible at the top of the menu.
            Not clickable; shows the exact lat/lng of the right-click
            target directly instead of making the user click through. */}
      <div
        className="context-menu-item"
        style={{
          padding: '8px 16px 6px',
          color: '#9ac0ff',
          fontSize: 12,
          fontFamily: 'monospace',
          display: 'flex',
          alignItems: 'center',
          cursor: 'pointer',
          gap: 4,
        }}
        title={t('map.whats_here_tooltip')}
        onMouseEnter={highlightItem}
        onMouseLeave={unhighlightItem}
        onClick={async (e) => {
          e.stopPropagation();
          const key = `${lat.toFixed(6)}|${lng.toFixed(6)}`;
          if (reverseGeo.loading && reverseGeo.key === key) return;
          if (reverseGeo.address && reverseGeo.key === key) return;
          setReverseGeo({ loading: true, address: null, error: null, key });
          try {
            const res = await reverseGeocode(lat, lng);
            const name = res?.display_name || res?.address || null;
            if (name) {
              setReverseGeo({ loading: false, address: name, error: null, key });
            } else {
              setReverseGeo({ loading: false, address: null, error: t('map.whats_here_empty'), key });
            }
          } catch (err: any) {
            setReverseGeo({ loading: false, address: null, error: err?.message || 'error', key });
          }
        }}
      >
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ marginRight: 4, opacity: 0.8 }}>
          <path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0118 0z" />
          <circle cx="12" cy="10" r="3" />
        </svg>
        <span style={{ flex: 1 }}>{lat.toFixed(6)}, {lng.toFixed(6)}</span>
        <span style={{ fontSize: 10, opacity: 0.7, fontFamily: 'inherit' }}>
          {reverseGeo.loading && reverseGeo.key === `${lat.toFixed(6)}|${lng.toFixed(6)}`
            ? t('map.whats_here_loading')
            : t('map.whats_here')}
        </span>
      </div>
      {/* Reverse-geocode result or error, shown only after the user taps
          the header row. Wraps + selectable so the user can copy the
          address. Max width is clipped by .context-menu parent. */}
      {reverseGeo.key === `${lat.toFixed(6)}|${lng.toFixed(6)}` &&
       (reverseGeo.address || reverseGeo.error) && (
        <div
          onClick={(e) => e.stopPropagation()}
          style={{
            padding: '2px 16px 8px',
            color: reverseGeo.error ? '#ff8a80' : '#d0d0d0',
            fontSize: 11.5,
            lineHeight: 1.5,
            userSelect: 'text',
            cursor: 'text',
            wordBreak: 'break-word',
          }}
        >
          {reverseGeo.address ?? reverseGeo.error}
        </div>
      )}
      <div style={{ height: 1, background: '#444', margin: '2px 0 4px' }} />

      {/* 2 + 3. Teleport / Navigate (device-gated). */}
      {deviceConnected ? (
        <>
          <div
            className="context-menu-item"
            style={contextMenuItemStyle}
            onMouseEnter={highlightItem}
            onMouseLeave={unhighlightItem}
            onClick={() => {
              onTeleport(lat, lng);
              onClose();
            }}
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ marginRight: 8 }}>
              <circle cx="12" cy="12" r="10" />
              <line x1="12" y1="2" x2="12" y2="6" />
              <line x1="12" y1="18" x2="12" y2="22" />
              <line x1="2" y1="12" x2="6" y2="12" />
              <line x1="18" y1="12" x2="22" y2="12" />
            </svg>
            {t('map.teleport_here')}
          </div>
          <div
            className="context-menu-item"
            style={contextMenuItemStyle}
            onMouseEnter={highlightItem}
            onMouseLeave={unhighlightItem}
            onClick={() => {
              onNavigate(lat, lng);
              onClose();
            }}
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ marginRight: 8 }}>
              <polygon points="3,11 22,2 13,21 11,13" />
            </svg>
            {t('map.navigate_here')}
          </div>
        </>
      ) : (
        <div
          style={{ ...contextMenuItemStyle, color: '#ff6b6b', cursor: 'not-allowed', opacity: 0.75 }}
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ marginRight: 8 }}>
            <circle cx="12" cy="12" r="10" />
            <line x1="4.93" y1="4.93" x2="19.07" y2="19.07" />
          </svg>
          {t('map.device_disconnected')}
        </div>
      )}

      {/* 4. Copy coordinates to clipboard. */}
      <div
        className="context-menu-item"
        style={contextMenuItemStyle}
        onMouseEnter={highlightItem}
        onMouseLeave={unhighlightItem}
        onClick={async () => {
          const txt = `${lat.toFixed(6)}, ${lng.toFixed(6)}`;
          try {
            await navigator.clipboard.writeText(txt);
          } catch {
            const ta = document.createElement('textarea');
            ta.value = txt;
            document.body.appendChild(ta);
            ta.select();
            try { document.execCommand('copy'); } catch { /* ignore */ }
            document.body.removeChild(ta);
          }
          if (onShowToast) onShowToast(tRef.current('map.coords_copied'));
          onClose();
        }}
      >
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ marginRight: 8 }}>
          <rect x="9" y="9" width="13" height="13" rx="2" ry="2" />
          <path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1" />
        </svg>
        {t('map.copy_coords')}
      </div>

      {/* 5. Add to bookmarks. */}
      <div
        className="context-menu-item"
        style={contextMenuItemStyle}
        onMouseEnter={highlightItem}
        onMouseLeave={unhighlightItem}
        onClick={() => {
          onAddBookmark(lat, lng);
          onClose();
        }}
      >
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ marginRight: 8 }}>
          <path d="M19 21l-7-5-7 5V5a2 2 0 012-2h10a2 2 0 012 2z" />
        </svg>
        {t('map.add_bookmark')}
      </div>

      {/* 6. Add waypoint (only when in a route mode). */}
      {showWaypointOption && onAddWaypoint && (
        <>
          <div style={{ height: 1, background: '#444', margin: '4px 0' }} />
          <div
            className="context-menu-item"
            style={contextMenuItemStyle}
            onMouseEnter={highlightItem}
            onMouseLeave={unhighlightItem}
            onClick={() => {
              onAddWaypoint(lat, lng);
              onClose();
            }}
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ marginRight: 8 }}>
              <circle cx="12" cy="12" r="3" />
              <line x1="12" y1="5" x2="12" y2="1" />
              <line x1="12" y1="23" x2="12" y2="19" />
              <line x1="5" y1="12" x2="1" y2="12" />
              <line x1="23" y1="12" x2="19" y2="12" />
            </svg>
            {t('map.add_waypoint')}
          </div>
        </>
      )}

    </div>
  );
}

export default MapContextMenu;
