import { useEffect, useRef } from 'react';
import type { MutableRefObject } from 'react';
import L from 'leaflet';
import type { Position } from './types';

// Update current position marker — move existing marker instead of recreating.
// When currentPosition becomes null (e.g. after 一鍵還原) remove the marker.
export function usePositionMarker({
  mapRef,
  currentPosition,
  dualMode,
  userAvatarHtml,
  prevPositionRef,
}: {
  mapRef: MutableRefObject<L.Map | null>;
  currentPosition: Position | null;
  dualMode: boolean;
  userAvatarHtml?: string;
  // Shared with the map-init effect: getInitialPosition only pans when no
  // real device position has arrived yet.
  prevPositionRef: MutableRefObject<Position | null>;
}): void {
  const currentMarkerRef = useRef<L.CircleMarker | null>(null);
  // Track the last avatar HTML we painted so the position-update effect
  // below can detect "avatar changed, need to rebuild marker even though
  // the position didn't change". Without this the new avatar only shows
  // up after the next teleport.
  const lastAvatarHtmlRef = useRef<string>('');

  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    if (dualMode) {
      // Dual-mode renderer below owns current-position markers; clear any
      // legacy single-device marker so it doesn't duplicate.
      if (currentMarkerRef.current) {
        try { (currentMarkerRef.current as any).remove(); } catch { /* ignore */ }
        currentMarkerRef.current = null;
      }
      // Pan the map to the new currentPosition in dual mode as well (address
      // search / coord input / bookmark click sets currentPosition before the
      // backend position_update arrives). First jump always centers; after
      // that only re-center on large jumps (>500m).
      if (currentPosition) {
        const latlng: L.LatLngExpression = [currentPosition.lat, currentPosition.lng];
        const prev = prevPositionRef.current;
        if (!prev) {
          map.setView(latlng, map.getZoom());
        } else {
          const dlat = (currentPosition.lat - prev.lat) * 111320;
          const dlng = (currentPosition.lng - prev.lng) * 111320 * Math.cos(currentPosition.lat * Math.PI / 180);
          const distM = Math.sqrt(dlat * dlat + dlng * dlng);
          if (distM > 500) {
            map.setView(latlng, map.getZoom());
          }
        }
        prevPositionRef.current = currentPosition;
      } else {
        prevPositionRef.current = null;
      }
      return;
    }
    if (!currentPosition) {
      if (currentMarkerRef.current) {
        try { (currentMarkerRef.current as any).remove(); } catch { /* ignore */ }
        currentMarkerRef.current = null;
      }
      prevPositionRef.current = null;
      return;
    }

    const latlng: L.LatLngExpression = [currentPosition.lat, currentPosition.lng];

    // If the avatar HTML changed since the marker was created, drop the
    // old marker so the recreate branch below paints with the new icon at
    // the current position — without this the user has to teleport again
    // to see their newly-saved avatar.
    const currentAvatar = userAvatarHtml ?? '';
    if (currentMarkerRef.current && lastAvatarHtmlRef.current !== currentAvatar) {
      try { (currentMarkerRef.current as any).remove(); } catch { /* ignore */ }
      currentMarkerRef.current = null;
    }

    if (currentMarkerRef.current) {
      // Just move the existing marker — no flicker. No tooltip update: the
      // marker is non-interactive (see below) and the coordinate readout
      // lives in the bottom status bar.
      (currentMarkerRef.current as any).setLatLng(latlng);
    } else {
      // First time: create the marker. User-supplied avatar HTML (if any)
      // replaces the default blue-person SVG. The pulse rings stay so the
      // marker still reads as a "live" position indicator.
      const avatarInner = userAvatarHtml && userAvatarHtml.length > 0
        ? userAvatarHtml
        : `<svg width="44" height="44" viewBox="0 0 44 44" class="pos-icon">
            <defs>
              <radialGradient id="posGlow" cx="50%" cy="50%" r="50%">
                <stop offset="0%" stop-color="#4285f4" stop-opacity="0.3"/>
                <stop offset="100%" stop-color="#4285f4" stop-opacity="0"/>
              </radialGradient>
              <filter id="posShadow" x="-30%" y="-30%" width="160%" height="160%">
                <feDropShadow dx="0" dy="1" stdDeviation="2" flood-color="#4285f4" flood-opacity="0.6"/>
              </filter>
            </defs>
            <circle cx="22" cy="22" r="20" fill="url(#posGlow)"/>
            <circle cx="22" cy="22" r="11" fill="#4285f4" filter="url(#posShadow)"/>
            <circle cx="22" cy="22" r="9" fill="#2b6ff2"/>
            <circle cx="22" cy="18" r="3.5" fill="#ffffff" opacity="0.95"/>
            <path d="M15.5 28.5c0-3.6 2.9-6.5 6.5-6.5s6.5 2.9 6.5 6.5" fill="#ffffff" opacity="0.95" stroke="none"/>
            <circle cx="22" cy="22" r="11" fill="none" stroke="#ffffff" stroke-width="2" opacity="0.8"/>
          </svg>`;
      const personIcon = L.divIcon({
        className: 'current-pos-marker',
        html: `<div class="pos-pulse-ring"></div>
          <div class="pos-pulse-ring pos-pulse-ring-2"></div>
          ${avatarInner}`,
        iconSize: [44, 44],
        iconAnchor: [22, 22],
      });

      // Non-interactive: no click handlers wired and no coord tooltip. The
      // blue person marker is pure UI — clicks should pass through to the
      // map / markers beneath it (bookmark pins etc.), and the coordinate
      // readout already lives in the bottom status bar.
      const marker = L.marker(latlng, {
        icon: personIcon,
        zIndexOffset: 1000,
        interactive: false,
      }).addTo(map);

      currentMarkerRef.current = marker as any;
      lastAvatarHtmlRef.current = currentAvatar;
    }

    // Only auto-center on first position or teleport (large jump > 500m)
    const prev = prevPositionRef.current;
    if (!prev) {
      map.setView(latlng, map.getZoom());
    } else {
      const dlat = (currentPosition.lat - prev.lat) * 111320;
      const dlng = (currentPosition.lng - prev.lng) * 111320 * Math.cos(currentPosition.lat * Math.PI / 180);
      const distM = Math.sqrt(dlat * dlat + dlng * dlng);
      if (distM > 500) {
        map.setView(latlng, map.getZoom());
      }
    }
    prevPositionRef.current = currentPosition;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentPosition, dualMode, userAvatarHtml]);
}
