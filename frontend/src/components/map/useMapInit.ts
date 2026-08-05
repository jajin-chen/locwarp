import { useEffect, useRef } from 'react';
import type { Dispatch, MutableRefObject, RefObject, SetStateAction } from 'react';
import L from 'leaflet';
import { setupTileLayers } from './tileLayers';
import type { ContextMenuState, Position, TRef, WpMenuState } from './types';

export interface MapInitRefs {
  mapContainerRef: RefObject<HTMLDivElement | null>;
  mapRef: MutableRefObject<L.Map | null>;
  recenterBtnRef: MutableRefObject<HTMLButtonElement | null>;
  recenterHandlerRef: MutableRefObject<() => void>;
  followBtnRef: MutableRefObject<HTMLButtonElement | null>;
  followHandlerRef: MutableRefObject<() => void>;
  s2GridBtnRef: MutableRefObject<HTMLButtonElement | null>;
  s2GridHandlerRef: MutableRefObject<() => void>;
}

// Initialize map: Leaflet map instance, zoom / recenter / follow / S2 grid
// controls, tile layers, click + contextmenu wiring, the onMapReady
// imperative escape hatch, and the saved-initial-position fetch. Runs once
// per mount; everything dynamic is routed through refs so the once-wired
// handlers never go stale.
export function useMapInit({
  tRef,
  onMapClickRef,
  onShowToastRef,
  followStateRef,
  setFollowMode,
  closeContextMenu,
  setWpMenu,
  setContextMenu,
  setS2PickerOpen,
  onMapReady,
  prevPositionRef,
}: {
  tRef: TRef;
  onMapClickRef: MutableRefObject<((lat: number, lng: number) => void) | undefined>;
  onShowToastRef: MutableRefObject<((msg: string) => void) | undefined>;
  followStateRef: MutableRefObject<boolean>;
  setFollowMode: Dispatch<SetStateAction<boolean>>;
  closeContextMenu: () => void;
  setWpMenu: Dispatch<SetStateAction<WpMenuState>>;
  setContextMenu: Dispatch<SetStateAction<ContextMenuState>>;
  setS2PickerOpen: Dispatch<SetStateAction<boolean>>;
  onMapReady?: (api: {
    panTo: (lat: number, lng: number, zoom?: number) => void;
    fitBounds: (points: { lat: number; lng: number }[]) => void;
  }) => void;
  prevPositionRef: MutableRefObject<Position | null>;
}): MapInitRefs {
  const mapContainerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<L.Map | null>(null);
  // Recenter-on-user-position button. We mount it as a real Leaflet control
  // (not absolutely-positioned React JSX) so Leaflet's own .leaflet-top
  // .leaflet-left layout pins it to the same x as the zoom buttons, with the
  // standard 10px gap. Any other approach leaves it a few px off.
  const recenterBtnRef = useRef<HTMLButtonElement | null>(null);
  const recenterHandlerRef = useRef<() => void>(() => {});
  // Follow-mode toggle button (sits below recenter as a third leaflet-bar).
  // When enabled, the map auto-pans to the current position on every update.
  // Manual map drag disables follow so the user can pan/look around freely.
  const followBtnRef = useRef<HTMLButtonElement | null>(null);
  const followHandlerRef = useRef<() => void>(() => {});
  // S2 cell grid overlay (Pokemon GO / Ingress style). Toggle button below
  // the library button. Default level 17 (~80m cells, the canonical Niantic
  // decor cell). Layer + level live in refs / state; visibility is mirrored
  // on the button via background colour.
  const s2GridBtnRef = useRef<HTMLButtonElement | null>(null);
  const s2GridHandlerRef = useRef<() => void>(() => {});

  // Initialize map
  useEffect(() => {
    if (!mapContainerRef.current || mapRef.current) return;

    const map = L.map(mapContainerRef.current, {
      center: [25.033, 121.5654],
      zoom: 13,
      // Keep Leaflet's default control off so we can position our own
      // zoom control below the EtaBar on the left (default top-left
      // would collide with the overlay).
      zoomControl: false,
      // Snap wheel zoom to integer levels + require a full notch per step,
      // so one wheel tick = one tile-load batch instead of cascading
      // intermediate zooms that all fire tile requests and bomb OSM's
      // rate limiter with black-tile fallout.
      zoomSnap: 1,
      wheelPxPerZoomLevel: 120,
      wheelDebounceTime: 60,
    });
    const zoomCtrl = L.control.zoom({ position: 'topleft' });
    zoomCtrl.addTo(map);
    // Nudge the top-left and top-right control clusters down so they sit
    // below the EtaBar (full-width, absolute-positioned at top:0) instead
    // of being partially covered by it.
    const topLeftEl = (map as any)._controlCorners?.topleft as HTMLElement | undefined;
    if (topLeftEl) {
      topLeftEl.style.marginTop = '56px';
    }
    const topRightEl = (map as any)._controlCorners?.topright as HTMLElement | undefined;
    if (topRightEl) {
      topRightEl.style.marginTop = '56px';
    }

    // Recenter button as a second leaflet-bar in the topleft corner. This
    // way Leaflet's layout (margin-left: 10px on each control + 10px gap
    // between stacked controls) handles positioning — guarantees same x as
    // the zoom +/- buttons with a natural gap below them.
    if (topLeftEl) {
      const wrapper = L.DomUtil.create('div', 'leaflet-bar leaflet-control');
      const btn = L.DomUtil.create('button', '', wrapper) as HTMLButtonElement;
      btn.type = 'button';
      btn.title = tRef.current('map.recenter');
      btn.setAttribute('role', 'button');
      btn.style.cssText = [
        'width: 30px', 'height: 30px', 'display: flex',
        'align-items: center', 'justify-content: center',
        'padding: 0', 'margin: 0', 'cursor: pointer',
        'background: var(--bg-surface, #2a2f3a)',
        'color: #fff', 'border: none', 'border-radius: 0',
      ].join(';');
      btn.innerHTML = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <circle cx="12" cy="12" r="3" />
        <line x1="12" y1="2" x2="12" y2="5" />
        <line x1="12" y1="19" x2="12" y2="22" />
        <line x1="2" y1="12" x2="5" y2="12" />
        <line x1="19" y1="12" x2="22" y2="12" />
      </svg>`;
      L.DomEvent.disableClickPropagation(wrapper);
      L.DomEvent.on(btn, 'click', (e: Event) => {
        e.preventDefault();
        if (btn.disabled) return;
        recenterHandlerRef.current();
      });
      topLeftEl.appendChild(wrapper);
      recenterBtnRef.current = btn;
    }

    // Follow-mode toggle, mounted as a third leaflet-bar so it lines up
    // exactly under the recenter button with Leaflet's standard 10px gap.
    if (topLeftEl) {
      const wrapper = L.DomUtil.create('div', 'leaflet-bar leaflet-control');
      const btn = L.DomUtil.create('button', '', wrapper) as HTMLButtonElement;
      btn.type = 'button';
      btn.title = tRef.current('map.follow_off');
      btn.setAttribute('role', 'button');
      btn.setAttribute('aria-pressed', 'false');
      btn.style.cssText = [
        'width: 30px', 'height: 30px', 'display: flex',
        'align-items: center', 'justify-content: center',
        'padding: 0', 'margin: 0', 'cursor: pointer',
        'background: var(--bg-surface, #2a2f3a)',
        'color: #fff', 'border: none', 'border-radius: 0',
      ].join(';');
      btn.innerHTML = `<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round">
        <polygon points="3 11 22 2 13 21 11 13 3 11"/>
      </svg>`;
      L.DomEvent.disableClickPropagation(wrapper);
      L.DomEvent.on(btn, 'click', (e: Event) => {
        e.preventDefault();
        followHandlerRef.current();
      });
      topLeftEl.appendChild(wrapper);
      followBtnRef.current = btn;
    }

    // (Library shortcut star removed — the top navigation bar's 收藏 tab
    // is now the single entry point for the library panel; issue #34.)

    // S2 cell grid toggle — fifth leaflet-bar. Tap to overlay an Ingress /
    // Pokemon GO style cell grid at the user-chosen level (default 17).
    // Right-click (or long-press) opens the level picker popover.
    if (topLeftEl) {
      const wrapper = L.DomUtil.create('div', 'leaflet-bar leaflet-control');
      const btn = L.DomUtil.create('button', 'locwarp-s2-btn', wrapper) as HTMLButtonElement;
      btn.type = 'button';
      btn.title = tRef.current('map.s2_toggle');
      btn.setAttribute('role', 'button');
      btn.setAttribute('aria-pressed', 'false');
      btn.style.cssText = [
        'width: 30px', 'height: 30px', 'display: flex',
        'align-items: center', 'justify-content: center',
        'padding: 0', 'margin: 0', 'cursor: pointer',
        'background: var(--bg-surface, #2a2f3a)',
        'color: #fff', 'border: none', 'border-radius: 0',
      ].join(';');
      btn.innerHTML = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
        <rect x="3" y="3" width="18" height="18" rx="1" />
        <line x1="9" y1="3" x2="9" y2="21" />
        <line x1="15" y1="3" x2="15" y2="21" />
        <line x1="3" y1="9" x2="21" y2="9" />
        <line x1="3" y1="15" x2="21" y2="15" />
      </svg>`;
      L.DomEvent.disableClickPropagation(wrapper);
      L.DomEvent.on(btn, 'click', (e: Event) => {
        e.preventDefault();
        s2GridHandlerRef.current();
      });
      L.DomEvent.on(btn, 'contextmenu', (e: Event) => {
        e.preventDefault();
        setS2PickerOpen((o) => !o);
      });
      topLeftEl.appendChild(wrapper);
      s2GridBtnRef.current = btn;
    }

    // User-initiated drag disables follow mode so they can pan freely. We
    // only react when follow is currently on (read via ref so the handler
    // wired once at mount sees the latest state). dragstart fires only on
    // pointer drag — programmatic panTo / setView do not trigger it, so
    // the auto-pan loop won't accidentally turn itself off.
    map.on('dragstart', () => {
      if (!followStateRef.current) return;
      setFollowMode(false);
      try {
        onShowToastRef.current?.(tRef.current('map.follow_disabled_toast'));
      } catch { /* ignore */ }
    });

    // Tile layers + switcher control + persisted choice (including the lazy
    // MapLibre GL Liberty layer) live in map/tileLayers.ts.
    setupTileLayers(map);

    // Left-click on the map dismisses any open context menu.
    // If the parent wires `onMapClick` (currently used by the "left-click
    // to add waypoint" toggle in Loop / MultiStop modes), forward the
    // coordinates there too.
    map.on('click', (e: L.LeafletMouseEvent) => {
      closeContextMenu();
      setWpMenu((prev) => prev.visible ? { ...prev, visible: false } : prev);
      try {
        onMapClickRef.current?.(e.latlng.lat, e.latlng.lng);
      } catch { /* ignore handler errors */ }
    });

    map.on('contextmenu', (e: L.LeafletMouseEvent) => {
      e.originalEvent.preventDefault();
      setContextMenu({
        visible: true,
        x: e.originalEvent.clientX,
        y: e.originalEvent.clientY,
        lat: e.latlng.lat,
        lng: e.latlng.lng,
      });
    });

    mapRef.current = map;

    // Hand the parent an imperative panTo so it can move the view without
    // touching React state (used by the StatusBar's Locate-PC pan-only flow).
    if (onMapReady) {
      try {
        onMapReady({
          panTo: (lat: number, lng: number, zoom?: number) => {
            const m = mapRef.current;
            if (!m) return;
            const targetZoom = zoom ?? Math.max(m.getZoom(), 16);
            m.setView([lat, lng], targetZoom, { animate: true });
          },
          // Move the view to encompass a set of points (e.g. a loaded
          // route's waypoints) so the user doesn't have to hunt for where
          // the route is on the map. A single point just centers on it.
          fitBounds: (points: { lat: number; lng: number }[]) => {
            const m = mapRef.current;
            if (!m || !points || points.length === 0) return;
            if (points.length === 1) {
              m.setView([points[0].lat, points[0].lng], Math.max(m.getZoom(), 16), { animate: true });
              return;
            }
            const bounds = L.latLngBounds(points.map((p) => [p.lat, p.lng] as [number, number]));
            m.fitBounds(bounds, { padding: [60, 60], maxZoom: 17, animate: true });
          },
        });
      } catch { /* non-fatal */ }
    }

    // Fetch the user-saved initial position from the backend (once, on mount).
    // If set, pan the map to it. Brief Taipei flash is acceptable.
    import('../../services/api').then(({ getInitialPosition }) => {
      getInitialPosition().then(({ position }) => {
        if (!position || !mapRef.current) return;
        if (prevPositionRef.current) return; // a real device position already arrived
        mapRef.current.setView([position.lat, position.lng], mapRef.current.getZoom());
      }).catch(() => { /* default center stays */ });
    });

    return () => {
      map.remove();
      mapRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return {
    mapContainerRef,
    mapRef,
    recenterBtnRef,
    recenterHandlerRef,
    followBtnRef,
    followHandlerRef,
    s2GridBtnRef,
    s2GridHandlerRef,
  };
}
