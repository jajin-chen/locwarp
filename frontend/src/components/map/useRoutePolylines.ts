import { useEffect, useRef } from 'react';
import type { MutableRefObject } from 'react';
import L from 'leaflet';
import type { Position } from './types';

// Update route polyline (base line + flowing-arrow overlay).
export function useRoutePolylines({
  mapRef,
  routePath,
  dualMode,
}: {
  mapRef: MutableRefObject<L.Map | null>;
  routePath: Position[];
  dualMode: boolean;
}): void {
  const polylineRef = useRef<L.Polyline | null>(null);
  // Second polyline layered on top for the flowing-arrow animation (design 6).
  const polylineArrowRef = useRef<L.Polyline | null>(null);

  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;

    if (polylineRef.current) {
      polylineRef.current.remove();
      polylineRef.current = null;
    }
    if (polylineArrowRef.current) {
      polylineArrowRef.current.remove();
      polylineArrowRef.current = null;
    }

    if (dualMode) return;

    if (routePath.length > 1) {
      const latlngs: L.LatLngExpression[] = routePath.map((p) => [p.lat, p.lng]);
      // Design 6 (chosen): flowing arrows. Base solid line + animated white
      // dash overlay that flows from start to end so the user can tell the
      // travel direction at a glance.
      const base = L.polyline(latlngs, {
        color: '#3a66c5',
        weight: 7,
        opacity: 0.9,
        lineCap: 'round',
        lineJoin: 'round',
      }).addTo(map);
      polylineRef.current = base;

      const arrows = L.polyline(latlngs, {
        color: '#ffffff',
        weight: 3,
        opacity: 0.95,
        dashArray: '2 38',
        lineCap: 'round',
        className: 'route-flow-dash',
      }).addTo(map);
      polylineArrowRef.current = arrows;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [routePath, dualMode]);
}
