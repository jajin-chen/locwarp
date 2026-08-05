import { useEffect, useRef } from 'react';
import type { Dispatch, MutableRefObject, SetStateAction } from 'react';
import L from 'leaflet';
import type { TRef, Waypoint, WpMenuState } from './types';

// Update waypoint markers. The joined-signature ref means re-renders with an
// identical waypoint list (same coords, same order) skip the full
// remove-and-recreate cycle.
export function useWaypointLayer({
  mapRef,
  waypoints,
  tRef,
  setWpMenu,
}: {
  mapRef: MutableRefObject<L.Map | null>;
  waypoints: Waypoint[];
  tRef: TRef;
  setWpMenu: Dispatch<SetStateAction<WpMenuState>>;
}): void {
  const waypointMarkersRef = useRef<L.Marker[]>([]);
  const waypointSigRef = useRef<string>('');

  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;

    const sig = waypoints.map((w) => `${w.lat.toFixed(7)},${w.lng.toFixed(7)}`).join('|');
    if (sig === waypointSigRef.current) return;
    waypointSigRef.current = sig;

    waypointMarkersRef.current.forEach((m) => m.remove());
    waypointMarkersRef.current = [];

    waypoints.forEach((wp) => {
      // index 0 is the implicit start point; S + green, numbered + orange.
      // Design: subway-station style — thick ring + short stem + ground
      // shadow. Chosen by user (from route-marker-designs.html, pick 07).
      const isStart = wp.index === 0;
      const label = isStart ? 'S' : String(wp.index);
      const ringColor = isStart ? '#43a047' : '#ff9800';
      const ringGlow  = isStart ? 'rgba(67,160,71,0.32)' : 'rgba(255,152,0,0.3)';
      const textColor = isStart ? '#1b5e20' : '#e65100';
      const stemStart = isStart ? '#43a047' : '#ff9800';
      const stemEnd   = isStart ? 'rgba(67,160,71,0)' : 'rgba(255,152,0,0)';
      const wpIcon = L.divIcon({
        className: 'waypoint-marker',
        // Outer wrapper is pointer-events:auto + cursor:pointer so the
        // ENTIRE 40x46 marker area (ring + stem + ground shadow + the
        // padding around them) catches the left-click — not just the
        // 28px ring. Old layout had pointer-events:none on the wrapper
        // which meant a click on the stem or shadow passed straight
        // through to the map and the waypoint menu never opened.
        html: `<div style="
          position:relative;width:100%;height:100%;
          display:flex;flex-direction:column;align-items:center;justify-content:flex-end;
          pointer-events:auto;cursor:pointer;">
          <div style="
            width:28px;height:28px;border-radius:50%;
            border:4px solid ${ringColor};background:#fff;
            display:flex;align-items:center;justify-content:center;
            color:${textColor};font-weight:800;font-size:13px;
            font-family:system-ui,-apple-system,'Segoe UI',sans-serif;
            box-shadow:0 0 0 2px ${ringGlow}, 0 3px 8px rgba(0,0,0,0.4);
          ">${label}</div>
          <div style="
            width:2px;height:10px;margin-top:-1px;
            background:linear-gradient(180deg, ${stemStart}, ${stemEnd});
          "></div>
          <div style="
            width:12px;height:3px;margin-top:-1px;
            background:rgba(0,0,0,0.5);border-radius:50%;filter:blur(1px);
          "></div>
        </div>`,
        iconSize: [40, 46],
        // Anchor = bottom-center of the ground shadow = exact (lat, lng).
        iconAnchor: [20, 46],
      });

      const marker = L.marker([wp.lat, wp.lng], { icon: wpIcon }).addTo(map);
      marker.bindTooltip(
        isStart ? tRef.current('panel.waypoint_start') : tRef.current('panel.waypoint_num', { n: wp.index }),
        { direction: 'top', offset: [0, -28] },
      );
      // Left-click opens a mini menu (set as start / delete). Stop the
      // event from bubbling to BOTH the map (so the click-to-add-
      // waypoint toggle doesn't see it as a new map click) AND the
      // DOM document (so the document-level outside-click handler
      // doesn't immediately close the menu we just opened — without
      // DOM stopPropagation the menu opens and closes in the same
      // tick and the user sees nothing).
      marker.on('click', (ev) => {
        const oe = ev.originalEvent as MouseEvent | undefined;
        L.DomEvent.stopPropagation(ev);
        if (oe) {
          oe.preventDefault?.();
          oe.stopPropagation?.();
          (oe as any).stopImmediatePropagation?.();
        }
        const x = oe?.clientX ?? 0;
        const y = oe?.clientY ?? 0;
        setWpMenu({ visible: true, x, y, index: wp.index, isStart });
      });
      waypointMarkersRef.current.push(marker);
    });
    // The waypoint signature may have changed under our feet (insert /
    // remove / rotate). Any open menu now points at a stale index, so
    // dismiss it.
    setWpMenu((prev) => prev.visible ? { ...prev, visible: false } : prev);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [waypoints]);
}
