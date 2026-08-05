import React, { useState } from 'react';
import type { MutableRefObject } from 'react';
import L from 'leaflet';
import { parseCoord } from '../../utils/coords';
import type { TRef } from './types';

// Coordinate-input overlay (replaces the sidebar's two-field coord input).
// parseCoord scrapes the first valid lat/lng out of arbitrary pasted
// text — bracket decoration, trailing notes ("一般火"), and label
// prefixes are all discarded so users don't have to hand-clean copies
// from Google Maps / chat / spreadsheets.
function CoordInputStrip({
  mapRef,
  t,
  tRef,
  deviceConnected,
  onShowToast,
  onTeleport,
  onNavigate,
  onCoordPreview,
}: {
  mapRef: MutableRefObject<L.Map | null>;
  t: (k: any, v?: any) => string;
  tRef: TRef;
  deviceConnected: boolean;
  onShowToast?: (msg: string) => void;
  onTeleport: (lat: number, lng: number, source?: 'menu' | 'coord') => void;
  onNavigate: (lat: number, lng: number, source?: 'menu' | 'coord') => void;
  onCoordPreview?: (lat: number, lng: number) => void;
}) {
  const [coordInput, setCoordInput] = useState('');
  const submitCoordGo = (kind: 'teleport' | 'navigate' = 'teleport') => {
    const parsed = parseCoord(coordInput);
    if (!parsed) {
      if (onShowToast) onShowToast(tRef.current('panel.coord_invalid'));
      return;
    }
    if (kind === 'navigate') onNavigate(parsed.lat, parsed.lng, 'coord');
    else onTeleport(parsed.lat, parsed.lng, 'coord');
    setCoordInput('');
  };
  // Preview-only: pan the map view to the parsed coordinate without
  // touching the iPhone GPS. Lets the user "peek" at a coordinate before
  // deciding to teleport. Keeps the input populated so the next click
  // can promote it to a real teleport / navigate.
  const submitCoordPreview = () => {
    const parsed = parseCoord(coordInput);
    if (!parsed) {
      if (onShowToast) onShowToast(tRef.current('panel.coord_invalid'));
      return;
    }
    if (onCoordPreview) {
      // Parent owns the pan + preview-pin drop. We let it decide so the
      // pin and the camera move together for both this overlay and the
      // bookmark-list "fly camera only" path.
      onCoordPreview(parsed.lat, parsed.lng);
      return;
    }
    const m = mapRef.current;
    if (!m) return;
    const targetZoom = Math.max(m.getZoom(), 16);
    m.setView([parsed.lat, parsed.lng], targetZoom, { animate: true });
  };

  return (
    <div
      onContextMenu={(e) => e.stopPropagation()}
      onMouseDown={(e) => e.stopPropagation()}
      className="anim-fade-slide-up"
      style={{
        display: 'flex', alignItems: 'center', gap: 6,
        background: 'rgba(26, 29, 39, 0.82)',
        backdropFilter: 'blur(14px) saturate(140%)',
        WebkitBackdropFilter: 'blur(14px) saturate(140%)',
        borderRadius: 10,
        padding: '7px 9px',
        boxShadow: '0 10px 32px rgba(12, 18, 40, 0.55), 0 0 0 1px rgba(255, 255, 255, 0.06) inset',
        border: '1px solid rgba(108, 140, 255, 0.15)',
        pointerEvents: 'auto',
      }}
    >
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#6c8cff" strokeWidth="2" style={{ flexShrink: 0 }}>
        <path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0118 0z" />
        <circle cx="12" cy="10" r="3" />
      </svg>
      <input
        type="text"
        value={coordInput}
        onChange={(e) => setCoordInput(e.target.value)}
        onKeyDown={(e) => { if (e.key === 'Enter') submitCoordGo('teleport'); }}
        placeholder={tRef.current('panel.coord_placeholder')}
        style={{
          width: 210, background: 'transparent', border: 'none',
          color: '#e8e8e8', fontSize: 12, outline: 'none',
          fontFamily: 'monospace',
        }}
      />
      <button
        onClick={async () => {
          try {
            const text = await navigator.clipboard.readText();
            if (text) setCoordInput(text.trim());
          } catch {
            if (onShowToast) onShowToast(tRef.current('panel.paste_denied'));
          }
        }}
        title={tRef.current('panel.paste_tooltip')}
        style={{
          background: 'rgba(255,255,255,0.08)',
          color: '#c7d0e4', border: '1px solid rgba(255,255,255,0.12)',
          borderRadius: 4, padding: '4px 8px', fontSize: 11, fontWeight: 600,
          cursor: 'pointer', display: 'inline-flex', alignItems: 'center', gap: 3,
        }}
      >
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <path d="M16 4h2a2 2 0 012 2v14a2 2 0 01-2 2H6a2 2 0 01-2-2V6a2 2 0 012-2h2" />
          <rect x="8" y="2" width="8" height="4" rx="1" ry="1" />
        </svg>
        {tRef.current('panel.paste')}
      </button>
      <button
        onClick={() => submitCoordGo('teleport')}
        disabled={!coordInput.trim() || !deviceConnected}
        title={t('map.teleport_here')}
        style={{
          background: !coordInput.trim() || !deviceConnected ? 'rgba(108,140,255,0.3)' : '#6c8cff',
          color: '#fff', border: 'none', borderRadius: 4,
          padding: '4px 10px', fontSize: 11, fontWeight: 600,
          cursor: !coordInput.trim() || !deviceConnected ? 'not-allowed' : 'pointer',
        }}
      >{tRef.current('panel.coord_teleport')}</button>
      <button
        onClick={submitCoordPreview}
        disabled={!coordInput.trim()}
        title={tRef.current('panel.coord_preview_tooltip')}
        style={{
          background: 'transparent',
          color: !coordInput.trim() ? 'rgba(199, 208, 228, 0.4)' : '#c7d0e4',
          border: `1px solid ${!coordInput.trim() ? 'rgba(255,255,255,0.1)' : 'rgba(255,255,255,0.28)'}`,
          borderRadius: 4,
          padding: '4px 10px', fontSize: 11, fontWeight: 600,
          cursor: !coordInput.trim() ? 'not-allowed' : 'pointer',
          display: 'inline-flex', alignItems: 'center', gap: 4,
        }}
      >
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" />
          <circle cx="12" cy="12" r="3" />
        </svg>
        {tRef.current('panel.coord_preview')}
      </button>
      <button
        onClick={() => submitCoordGo('navigate')}
        disabled={!coordInput.trim() || !deviceConnected}
        title={tRef.current('panel.coord_navigate_tooltip')}
        style={{
          background: 'transparent',
          color: !coordInput.trim() || !deviceConnected ? 'rgba(76, 175, 80, 0.4)' : '#4caf50',
          border: `1px solid ${!coordInput.trim() || !deviceConnected ? 'rgba(76, 175, 80, 0.3)' : 'rgba(76, 175, 80, 0.55)'}`,
          borderRadius: 4,
          padding: '4px 10px', fontSize: 11, fontWeight: 600,
          cursor: !coordInput.trim() || !deviceConnected ? 'not-allowed' : 'pointer',
        }}
      >{tRef.current('panel.coord_navigate')}</button>
    </div>
  );
}

export default CoordInputStrip;
