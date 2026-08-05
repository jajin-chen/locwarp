import { useEffect, useRef } from 'react';
import type { MutableRefObject } from 'react';
import L from 'leaflet';
import type { Position, TRef } from './types';

// Preview pin (camera-only fly target). Amber teardrop with an eye icon
// to convey "you're peeking at this coordinate, GPS hasn't actually
// moved here". Click the marker to dismiss the pin.
export function usePreviewPin({
  mapRef,
  previewPin,
  onPreviewPinClear,
  tRef,
}: {
  mapRef: MutableRefObject<L.Map | null>;
  previewPin?: Position | null;
  onPreviewPinClear?: () => void;
  tRef: TRef;
}): void {
  const previewMarkerRef = useRef<L.Marker | null>(null);
  const previewSigRef = useRef<string | null>(null);

  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;

    const sig = previewPin ? `${previewPin.lat.toFixed(7)},${previewPin.lng.toFixed(7)}` : null;
    if (sig === previewSigRef.current) return;
    previewSigRef.current = sig;

    if (previewMarkerRef.current) {
      previewMarkerRef.current.remove();
      previewMarkerRef.current = null;
    }

    if (previewPin) {
      const amberIcon = L.divIcon({
        className: 'preview-marker',
        html: `<svg width="36" height="50" viewBox="0 0 36 50">
          <defs>
            <filter id="previewShadow" x="-20%" y="-10%" width="140%" height="130%">
              <feDropShadow dx="0" dy="2" stdDeviation="2.5" flood-color="#000" flood-opacity="0.4"/>
            </filter>
            <linearGradient id="previewGrad" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stop-color="#fbbf24"/>
              <stop offset="100%" stop-color="#d97706"/>
            </linearGradient>
          </defs>
          <ellipse cx="18" cy="47" rx="6" ry="2" fill="#000" opacity="0.2"/>
          <path d="M18 2C9.7 2 3 8.7 3 17c0 12 15 30 15 30s15-18 15-30C33 8.7 26.3 2 18 2z"
                fill="url(#previewGrad)" filter="url(#previewShadow)"
                stroke="rgba(255,255,255,0.7)" stroke-width="1.2"/>
          <circle cx="18" cy="17" r="7" fill="#ffffff" opacity="0.95"/>
          <svg x="11" y="10" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#d97706" stroke-width="2.2">
            <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/>
            <circle cx="12" cy="12" r="3"/>
          </svg>
        </svg>`,
        iconSize: [36, 50],
        iconAnchor: [18, 47],
      });

      const marker = L.marker([previewPin.lat, previewPin.lng], {
        icon: amberIcon,
        zIndexOffset: 500,
      }).addTo(map);

      const tip = `${tRef.current('map.preview_pin')} · ${previewPin.lat.toFixed(5)}, ${previewPin.lng.toFixed(5)}`;
      marker.bindTooltip(tip, { direction: 'top', offset: [0, -48] });
      if (onPreviewPinClear) {
        marker.on('click', () => onPreviewPinClear());
      }
      previewMarkerRef.current = marker;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [previewPin, onPreviewPinClear]);
}
