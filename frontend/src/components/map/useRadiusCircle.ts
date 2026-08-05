import { useEffect, useRef } from 'react';
import type { MutableRefObject } from 'react';
import L from 'leaflet';
import type { Position } from './types';

// Update random walk radius circle.
export function useRadiusCircle({
  mapRef,
  randomWalkRadius,
  currentPosition,
  randomWalkCenter,
  randomWalkCenterMode,
  dualMode,
}: {
  mapRef: MutableRefObject<L.Map | null>;
  randomWalkRadius: number | null;
  currentPosition: Position | null;
  randomWalkCenter?: Position | null;
  randomWalkCenterMode: 'fixed' | 'follow';
  dualMode: boolean;
}): void {
  const radiusCircleRef = useRef<L.Circle | null>(null);

  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;

    // Remove old circle
    if (radiusCircleRef.current) {
      radiusCircleRef.current.remove();
      radiusCircleRef.current = null;
    }

    if (dualMode) return;

    // Circle centre:
    //  - "follow" mode (or before a walk starts): track the avatar so it
    //    shows where the next hop can land.
    //  - "fixed" mode while walking: pin to the captured start centre so the
    //    circle stops drifting away from the real (fixed) sampling area.
    const center =
      randomWalkCenterMode === 'fixed' && randomWalkCenter
        ? randomWalkCenter
        : currentPosition;

    // Draw circle when radius is set and we have a centre
    if (randomWalkRadius && randomWalkRadius > 0 && center) {
      const circle = L.circle(
        [center.lat, center.lng],
        {
          radius: randomWalkRadius,
          color: '#4285f4',
          weight: 2,
          opacity: 0.6,
          fillColor: '#4285f4',
          fillOpacity: 0.08,
          dashArray: '6, 6',
        }
      ).addTo(map);
      radiusCircleRef.current = circle;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [randomWalkRadius, currentPosition, randomWalkCenter, randomWalkCenterMode, dualMode]);
}
