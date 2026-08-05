import { useCallback, useEffect, useRef, useState } from 'react';
import type { Dispatch, MutableRefObject, SetStateAction } from 'react';
import L from 'leaflet';
import { cellsInBounds, approxCellSizeMeters } from '../../services/s2grid';
import type { S2CellPolygon } from '../../services/s2grid';

// ── S2 cell grid overlay ────────────────────────────────────────────
// State (level + enabled, persisted in localStorage), the leaflet-bar
// button sync, and the draw-on-move/zoom effect. The toggle handler is
// wired into the once-mounted button via a ref so it always sees the
// latest setter.
export function useS2Grid({
  mapRef,
  s2GridBtnRef,
  s2GridHandlerRef,
  t,
}: {
  mapRef: MutableRefObject<L.Map | null>;
  s2GridBtnRef: MutableRefObject<HTMLButtonElement | null>;
  s2GridHandlerRef: MutableRefObject<() => void>;
  t: (k: any, v?: any) => string;
}): {
  s2Enabled: boolean;
  setS2Enabled: Dispatch<SetStateAction<boolean>>;
  s2Level: number;
  setS2Level: Dispatch<SetStateAction<number>>;
  s2Suppressed: boolean;
} {
  const s2LayerRef = useRef<L.LayerGroup | null>(null);

  // S2 cell grid state. Persisted in localStorage so the user's preferred
  // level + on/off survives across launches (similar to tile-layer choice).
  const [s2Enabled, setS2Enabled] = useState<boolean>(() => {
    try { return localStorage.getItem('locwarp.s2_enabled') === '1'; }
    catch { return false; }
  });
  const [s2Level, setS2Level] = useState<number>(() => {
    try {
      const raw = localStorage.getItem('locwarp.s2_level');
      const n = raw ? parseInt(raw, 10) : 17;
      if (Number.isFinite(n) && n >= 1 && n <= 30) return n;
    } catch { /* fall through */ }
    return 17;
  });

  useEffect(() => {
    try { localStorage.setItem('locwarp.s2_enabled', s2Enabled ? '1' : '0'); }
    catch { /* ignore */ }
  }, [s2Enabled]);
  useEffect(() => {
    try { localStorage.setItem('locwarp.s2_level', String(s2Level)); }
    catch { /* ignore */ }
  }, [s2Level]);

  const toggleS2Grid = useCallback(() => {
    setS2Enabled((prev) => !prev);
  }, []);
  useEffect(() => {
    s2GridHandlerRef.current = toggleS2Grid;
    const btn = s2GridBtnRef.current;
    if (!btn) return;
    btn.style.background = s2Enabled ? '#6c8cff' : 'var(--bg-surface, #2a2f3a)';
    btn.title = t('map.s2_toggle');
    btn.setAttribute('aria-pressed', s2Enabled ? 'true' : 'false');
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [toggleS2Grid, s2Enabled, t]);

  // Track whether the grid was suppressed because the user is too far zoomed
  // out. The level picker uses this to tell them to zoom in instead of
  // silently showing nothing.
  const [s2Suppressed, setS2Suppressed] = useState(false);

  // Recompute + paint S2 polygons whenever the layer is toggled, the level
  // changes, or the user pans / zooms. Capped per zoom inside cellsInBounds
  // so wide zooms with high levels don't lock the UI.
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    const draw = () => {
      if (s2LayerRef.current) {
        try { s2LayerRef.current.remove(); } catch { /* ignore */ }
        s2LayerRef.current = null;
      }
      if (!s2Enabled) {
        setS2Suppressed(false);
        return;
      }
      // Suppress when the chosen level would render cells smaller than ~2 px:
      // the BFS safety cap clips at a center cluster and the grid then looks
      // like it 'wanders' with the cursor as you pan. Tell the user to zoom
      // in (or pick a coarser level) instead of silently rendering garbage.
      const zoom = map.getZoom();
      const lat = map.getCenter().lat;
      const cellMeters = approxCellSizeMeters(s2Level, lat);
      // Web Mercator: world circumference at the equator is 40075016m, mapped
      // to 256*2^zoom pixels. cos(lat) factor already baked into approxCellSizeMeters.
      const cellPx = cellMeters * (256 * Math.pow(2, zoom)) / 40075016;
      if (cellPx < 2) {
        setS2Suppressed(true);
        return;
      }
      setS2Suppressed(false);
      const bounds = map.getBounds();
      let cells: S2CellPolygon[];
      try {
        cells = cellsInBounds(bounds, s2Level);
      } catch {
        return;
      }
      if (!cells.length) return;
      const layer = L.layerGroup();
      // Solid colour, transparent fill — keeps the underlying map readable.
      // Slightly thinner stroke at high levels (more cells, would otherwise
      // blanket the screen).
      const weight = s2Level >= 18 ? 0.6 : s2Level >= 16 ? 0.8 : 1.1;
      for (const c of cells) {
        L.polygon(c.corners, {
          color: '#6c8cff',
          weight,
          opacity: 0.85,
          fill: true,
          fillColor: '#6c8cff',
          fillOpacity: 0.04,
          interactive: false,
          // Sit below markers so cell lines never block clicks on bookmark
          // pins / waypoint markers / context menu.
          pane: 'overlayPane',
        }).addTo(layer);
      }
      layer.addTo(map);
      s2LayerRef.current = layer;
    };
    draw();
    map.on('moveend', draw);
    map.on('zoomend', draw);
    return () => {
      map.off('moveend', draw);
      map.off('zoomend', draw);
      if (s2LayerRef.current) {
        try { s2LayerRef.current.remove(); } catch { /* ignore */ }
        s2LayerRef.current = null;
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [s2Enabled, s2Level]);

  return { s2Enabled, setS2Enabled, s2Level, setS2Level, s2Suppressed };
}
