import { useEffect, useRef } from 'react';
import type { MutableRefObject } from 'react';
import L from 'leaflet';
import type { Position } from './types';

// 種花模式: place a numbered badge (1..N) at every circle-segment vertex
// so the user can see exactly how many segments each flower's circle has
// and the order they're walked. divIcon markers sit above the route line
// (zIndexOffset) so they stay visible once the simulation starts.
export function useFlowerPreview({
  mapRef,
  flowerPreview,
  dualMode,
}: {
  mapRef: MutableRefObject<L.Map | null>;
  flowerPreview?: Position[][];
  dualMode: boolean;
}): void {
  const flowerPreviewRef = useRef<L.Marker[]>([]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    flowerPreviewRef.current.forEach((p) => { try { p.remove(); } catch { /* ignore */ } });
    flowerPreviewRef.current = [];
    if (dualMode) return;
    if (!flowerPreview || flowerPreview.length === 0) return;
    flowerPreview.forEach((poly) => {
      if (!poly || poly.length < 2) return;
      poly.forEach((p, k) => {
        const icon = L.divIcon({
          className: 'flower-seg-badge',
          html: `<div style="display:flex;align-items:center;justify-content:center;`
            + `width:20px;height:20px;border-radius:50%;`
            + `background:#ffcf3f;color:#2a1d00;font-size:11px;font-weight:800;`
            + `border:2px solid #8a5a12;box-shadow:0 1px 4px rgba(0,0,0,0.6);`
            + `font-family:system-ui,sans-serif;">${k + 1}</div>`,
          iconSize: [20, 20],
          iconAnchor: [10, 10],
        });
        const m = L.marker([p.lat, p.lng], {
          icon,
          interactive: false,
          keyboard: false,
          zIndexOffset: 1000,
        }).addTo(map);
        flowerPreviewRef.current.push(m);
      });
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [flowerPreview, dualMode]);
}
