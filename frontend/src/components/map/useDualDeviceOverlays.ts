import { useEffect, useRef } from 'react';
import type { MutableRefObject } from 'react';
import L from 'leaflet';
import type { DeviceRuntime, RuntimesMap } from '../../hooks/useSimulation';
import type { DeviceInfo } from '../../hooks/useDevice';
import type { Position } from './types';
import { haversineM } from './utils';

const DEVICE_COLORS = ['#4285f4', '#ff9800'];
const DEVICE_LETTERS = ['A', 'B'];

// ── Dual-mode per-device overlays ────────────────────────────────────
// Keeps refs for markers/polylines/circles keyed by udid so updates don't
// recreate Leaflet layers on every position tick.
export function useDualDeviceOverlays({
  mapRef,
  dualMode,
  devices,
  runtimes,
  randomWalkRadius,
  t,
}: {
  mapRef: MutableRefObject<L.Map | null>;
  dualMode: boolean;
  devices?: DeviceInfo[];
  runtimes?: RuntimesMap;
  randomWalkRadius: number | null;
  t: (k: any, v?: any) => string;
}): void {
  const deviceMarkersRef = useRef<Record<string, L.Marker>>({});
  const deviceDestMarkersRef = useRef<Record<string, L.Marker>>({});
  const deviceDestSharedRef = useRef<L.Marker | null>(null);
  const devicePolylinesRef = useRef<Record<string, L.Polyline>>({});
  const deviceCirclesRef = useRef<Record<string, L.Circle>>({});

  const clearDeviceOverlays = () => {
    Object.values(deviceMarkersRef.current).forEach((m) => { try { m.remove(); } catch { /* ignore */ } });
    deviceMarkersRef.current = {};
    Object.values(deviceDestMarkersRef.current).forEach((m) => { try { m.remove(); } catch { /* ignore */ } });
    deviceDestMarkersRef.current = {};
    if (deviceDestSharedRef.current) {
      try { deviceDestSharedRef.current.remove(); } catch { /* ignore */ }
      deviceDestSharedRef.current = null;
    }
    Object.values(devicePolylinesRef.current).forEach((p) => { try { p.remove(); } catch { /* ignore */ } });
    devicePolylinesRef.current = {};
    Object.values(deviceCirclesRef.current).forEach((c) => { try { c.remove(); } catch { /* ignore */ } });
    deviceCirclesRef.current = {};
  };

  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    if (!dualMode || !devices || !runtimes) {
      clearDeviceOverlays();
      return;
    }

    const activeUdids = new Set<string>();
    devices.slice(0, 2).forEach((dev, i) => {
      const rt: DeviceRuntime | undefined = runtimes[dev.udid];
      if (!rt) return;
      activeUdids.add(dev.udid);
      const color = DEVICE_COLORS[i];
      const letter = DEVICE_LETTERS[i];

      // Current position marker
      if (rt.currentPos) {
        const latlng: L.LatLngExpression = [rt.currentPos.lat, rt.currentPos.lng];
        const existing = deviceMarkersRef.current[dev.udid];
        if (existing) {
          (existing as any).setLatLng(latlng);
        } else {
          const icon = L.divIcon({
            className: 'current-pos-marker',
            html: `<div class="pos-pulse-ring" style="border-color:${color};"></div>
              <div class="pos-pulse-ring pos-pulse-ring-2" style="border-color:${color};"></div>
              <svg width="44" height="44" viewBox="0 0 44 44" class="pos-icon">
                <circle cx="22" cy="22" r="13" fill="${color}" opacity="0.95"/>
                <circle cx="22" cy="22" r="11" fill="none" stroke="#ffffff" stroke-width="2"/>
                <text x="22" y="26" text-anchor="middle" fill="#ffffff" font-size="13" font-weight="700" font-family="system-ui">${letter}</text>
              </svg>`,
            iconSize: [44, 44],
            iconAnchor: [22, 22],
          });
          const marker = L.marker(latlng, { icon, zIndexOffset: 1000 + i }).addTo(map);
          marker.bindTooltip(`${letter} · ${dev.name}`, { direction: 'top', offset: [0, -20] });
          deviceMarkersRef.current[dev.udid] = marker;
        }
      } else if (deviceMarkersRef.current[dev.udid]) {
        try { deviceMarkersRef.current[dev.udid].remove(); } catch { /* ignore */ }
        delete deviceMarkersRef.current[dev.udid];
      }

      // Route polyline
      const existingLine = devicePolylinesRef.current[dev.udid];
      if (existingLine) {
        try { existingLine.remove(); } catch { /* ignore */ }
        delete devicePolylinesRef.current[dev.udid];
      }
      if (rt.routePath && rt.routePath.length > 1) {
        const latlngs: L.LatLngExpression[] = rt.routePath.map((p) => [p.lat, p.lng]);
        const line = L.polyline(latlngs, { color, weight: 4, opacity: 0.85 }).addTo(map);
        devicePolylinesRef.current[dev.udid] = line;
      }

      // Random-walk radius circle
      const existingCircle = deviceCirclesRef.current[dev.udid];
      if (existingCircle) {
        try { existingCircle.remove(); } catch { /* ignore */ }
        delete deviceCirclesRef.current[dev.udid];
      }
      if (randomWalkRadius && randomWalkRadius > 0 && rt.currentPos) {
        const c = L.circle([rt.currentPos.lat, rt.currentPos.lng], {
          radius: randomWalkRadius,
          color, weight: 2, opacity: 0.7,
          fillColor: color, fillOpacity: 0.06,
          dashArray: '6, 6',
        }).addTo(map);
        deviceCirclesRef.current[dev.udid] = c;
      }
    });

    // Remove layers for devices no longer in the slice
    Object.keys(deviceMarkersRef.current).forEach((u) => {
      if (!activeUdids.has(u)) {
        try { deviceMarkersRef.current[u].remove(); } catch { /* ignore */ }
        delete deviceMarkersRef.current[u];
      }
    });
    Object.keys(devicePolylinesRef.current).forEach((u) => {
      if (!activeUdids.has(u)) {
        try { devicePolylinesRef.current[u].remove(); } catch { /* ignore */ }
        delete devicePolylinesRef.current[u];
      }
    });
    Object.keys(deviceCirclesRef.current).forEach((u) => {
      if (!activeUdids.has(u)) {
        try { deviceCirclesRef.current[u].remove(); } catch { /* ignore */ }
        delete deviceCirclesRef.current[u];
      }
    });

    // Destination markers: dedup when both destinations are within ~5m.
    Object.values(deviceDestMarkersRef.current).forEach((m) => { try { m.remove(); } catch { /* ignore */ } });
    deviceDestMarkersRef.current = {};
    if (deviceDestSharedRef.current) {
      try { deviceDestSharedRef.current.remove(); } catch { /* ignore */ }
      deviceDestSharedRef.current = null;
    }

    const dests: { dev: DeviceInfo; color: string; letter: string; dest: Position }[] = [];
    devices.slice(0, 2).forEach((dev, i) => {
      const rt = runtimes[dev.udid];
      if (rt && rt.destination) {
        dests.push({ dev, color: DEVICE_COLORS[i], letter: DEVICE_LETTERS[i], dest: rt.destination });
      }
    });

    const allSame = dests.length >= 2 && dests.slice(1).every((d) => haversineM(d.dest, dests[0].dest) <= 5);
    if (dests.length === 0) {
      // nothing to draw
    } else if (allSame) {
      const d = dests[0].dest;
      const redIcon = L.divIcon({
        className: 'dest-marker',
        html: `<svg width="36" height="50" viewBox="0 0 36 50">
          <ellipse cx="18" cy="47" rx="6" ry="2" fill="#000" opacity="0.2"/>
          <path d="M18 2C9.7 2 3 8.7 3 17c0 12 15 30 15 30s15-18 15-30C33 8.7 26.3 2 18 2z" fill="#e53935"/>
          <circle cx="18" cy="17" r="7" fill="#ffffff" opacity="0.95"/>
        </svg>`,
        iconSize: [36, 50],
        iconAnchor: [18, 47],
      });
      const m = L.marker([d.lat, d.lng], { icon: redIcon }).addTo(map);
      m.bindTooltip(t('map.destination'), { direction: 'top', offset: [0, -48] });
      deviceDestSharedRef.current = m;
    } else {
      dests.forEach(({ dev, color, letter, dest }) => {
        const icon = L.divIcon({
          className: 'dest-marker',
          html: `<svg width="36" height="50" viewBox="0 0 36 50">
            <ellipse cx="18" cy="47" rx="6" ry="2" fill="#000" opacity="0.2"/>
            <path d="M18 2C9.7 2 3 8.7 3 17c0 12 15 30 15 30s15-18 15-30C33 8.7 26.3 2 18 2z" fill="${color}"/>
            <circle cx="18" cy="17" r="7" fill="#ffffff" opacity="0.95"/>
            <text x="18" y="21" text-anchor="middle" fill="${color}" font-size="11" font-weight="700" font-family="system-ui">${letter}</text>
          </svg>`,
          iconSize: [36, 50],
          iconAnchor: [18, 47],
        });
        const m = L.marker([dest.lat, dest.lng], { icon }).addTo(map);
        m.bindTooltip(`${letter} · ${t('map.destination')}`, { direction: 'top', offset: [0, -48] });
        deviceDestMarkersRef.current[dev.udid] = m;
      });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dualMode, devices, runtimes, randomWalkRadius, t]);
}
