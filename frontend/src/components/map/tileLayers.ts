import L from 'leaflet';
import maplibreWorkerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url';

// MapLibre GL (+ its Leaflet binding) is ~800KB and only needed for the
// OpenFreeMap Liberty vector layer, so it is loaded on demand via dynamic
// import() the first time that layer is selected. The Leaflet binding looks
// up `window.maplibregl` rather than taking it as a constructor argument,
// so the shim is installed after maplibre-gl resolves and before the
// binding is imported / `L.maplibreGL({ ... })` is called.
let maplibreStackPromise: Promise<void> | null = null;
function loadMaplibreStack(): Promise<void> {
  if (!maplibreStackPromise) {
    maplibreStackPromise = (async () => {
      const [maplibregl] = await Promise.all([
        import('maplibre-gl'),
        import('maplibre-gl/dist/maplibre-gl.css'),
      ]);
      // MapLibre GL 6 is ESM-only and exposes named exports through the
      // namespace object. Point the packaged Electron build at Vite's worker
      // chunk because import.meta.url is file:// there.
      maplibregl.config.WORKER_URL = maplibreWorkerUrl;
      if (typeof window !== 'undefined' && !(window as any).maplibregl) {
        (window as any).maplibregl = maplibregl;
      }
      await import('@maplibre/maplibre-gl-leaflet');
    })().catch((err) => {
      // Allow a retry on the next layer selection instead of caching failure.
      maplibreStackPromise = null;
      throw err;
    });
  }
  return maplibreStackPromise;
}

// Build the base tile layers, restore the user's saved choice, and mount
// the layer-switcher control. Extracted verbatim from the map-init effect.
export function setupTileLayers(map: L.Map): void {
  // Tile layer tuning (shared across all providers):
  //   updateWhenIdle=false    — load during pan, not only on idle
  //   updateWhenZooming=true  — fetch target-level tiles during zoom so
  //                             the user sees sharp tiles instead of
  //                             upscaled-and-blurry placeholders
  //   keepBuffer=4            — keep 4 rows/cols of off-screen tiles cached
  //   crossOrigin=true        — enable HTTP cache reuse across layers
  //
  // detectRetina intentionally NOT enabled: its "fetch zoom+1, display at
  // half size" approach makes every label on the map physically smaller,
  // which users reported as hard to read on HiDPI screens. Slightly
  // softer raster is the lesser evil versus unreadable labels.
  const baseOpts = {
    updateWhenIdle: false,
    updateWhenZooming: true,
    keepBuffer: 4,
    crossOrigin: true,
  } as const;
  // OSM Standard (Mapnik). Uses a/b/c subdomains to parallelise fetches.
  // electron/main.js rewrites the User-Agent for these hosts so tile.osm.org
  // does not reject the default Chromium UA with HTTP 418.
  const osmLayer = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    ...baseOpts,
    subdomains: 'abc', maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  });
  // CartoDB Voyager: OSM data, CARTO-hosted CDN. No OSM rate-limit risk,
  // built-in @2x retina, 4 subdomains. Use this when OSM feels laggy.
  const cartoLayer = L.tileLayer(
    'https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png',
    {
      ...baseOpts,
      subdomains: 'abcd', maxZoom: 20,
      attribution:
        '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>',
    },
  );
  // ESRI World Imagery — free satellite/aerial imagery, global coverage.
  // URL template uses {y}/{x} order (ESRI convention), not the usual
  // {x}/{y}. No API key needed, generous usage limits.
  const esriSatLayer = L.tileLayer(
    'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
    {
      ...baseOpts,
      maxZoom: 19,
      attribution:
        'Tiles &copy; Esri &mdash; Source: Esri, Maxar, Earthstar Geographics, and the GIS User Community',
    },
  );
  // OpenFreeMap Liberty — free, no API key, vector tiles styled to look
  // close to Mapbox / Google. Rendered via MapLibre GL through the
  // maplibre-gl-leaflet binding so Leaflet treats it like any other
  // base layer. Bigger bundle than raster but globally free with no
  // monthly cap. The MapLibre stack is heavy, so the entry in the layers
  // control is a lazy L.layerGroup shell: the real GL layer is created
  // (after a dynamic import) the first time the shell is added to the
  // map, then parked inside the group. LayerGroup handles the async race
  // for us — if the user switches away before the import resolves, the
  // GL layer is stored in the (detached) group and only attaches when
  // Liberty is selected again.
  const libertyLayer = L.layerGroup([]);
  let libertyGlLayer: L.Layer | null = null;
  libertyLayer.on('add', () => {
    if (libertyGlLayer) return;
    loadMaplibreStack()
      .then(() => {
        if (libertyGlLayer) return;
        libertyGlLayer = (L as any).maplibreGL({
          style: 'https://tiles.openfreemap.org/styles/liberty',
          attribution:
            '&copy; <a href="https://openfreemap.org/" target="_blank" rel="noopener">OpenFreeMap</a> &copy; <a href="https://www.openmaptiles.org/" target="_blank" rel="noopener">OpenMapTiles</a> &copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors',
        }) as L.Layer;
        libertyLayer.addLayer(libertyGlLayer);
      })
      .catch((err) => {
        console.error('Failed to load MapLibre GL for Liberty layer', err);
      });
  });

  // NLSC 通用版電子地圖 — Taiwan government basemap (內政部國土測繪中心).
  // No API key, no quota, completely free. Coverage is Taiwan-only:
  // the rest of the world renders as a blank/grey backdrop. WMTS uses
  // the {y}/{x} (row/col) ordering convention same as ESRI.
  const nlscLayer = L.tileLayer(
    'https://wmts.nlsc.gov.tw/wmts/EMAP/default/GoogleMapsCompatible/{z}/{y}/{x}',
    {
      ...baseOpts,
      maxZoom: 20,
      attribution:
        '&copy; <a href="https://www.nlsc.gov.tw/" target="_blank" rel="noopener">內政部國土測繪中心</a>',
    },
  );

  // GSI 地理院タイル — Japan government basemap (国土地理院).
  // Same model as NLSC: no API key, no quota, free. Coverage is
  // Japan-only. Standard XYZ tile layout (no row/col swap), so the
  // URL template matches Leaflet's defaults directly.
  const gsiLayer = L.tileLayer(
    'https://cyberjapandata.gsi.go.jp/xyz/std/{z}/{x}/{y}.png',
    {
      ...baseOpts,
      maxZoom: 18,
      attribution:
        '&copy; <a href="https://www.gsi.go.jp/" target="_blank" rel="noopener">国土地理院</a>',
    },
  );

  // Restore the user's previous choice so switching persists between launches.
  const savedLayer = (() => {
    try { return localStorage.getItem('locwarp.tile_layer') || 'osm'; }
    catch { return 'osm'; }
  })();
  const layers: Record<string, L.Layer> = {
    'OSM': osmLayer,
    'CartoDB Voyager': cartoLayer,
    'ESRI 衛星 / Satellite': esriSatLayer,
    'OpenFreeMap Liberty': libertyLayer,
    'NLSC 台灣電子地圖': nlscLayer,
    'GSI 日本地理院地圖': gsiLayer,
  };
  const initialKey =
    savedLayer === 'carto' ? 'CartoDB Voyager' :
    savedLayer === 'esri' ? 'ESRI 衛星 / Satellite' :
    savedLayer === 'liberty' ? 'OpenFreeMap Liberty' :
    savedLayer === 'nlsc' ? 'NLSC 台灣電子地圖' :
    savedLayer === 'gsi' ? 'GSI 日本地理院地圖' :
    'OSM';
  layers[initialKey].addTo(map);
  L.control.layers(layers, undefined, { position: 'topright', collapsed: true }).addTo(map);
  map.on('baselayerchange', (e: any) => {
    try {
      const key: string =
        e?.name === 'CartoDB Voyager' ? 'carto' :
        e?.name === 'ESRI 衛星 / Satellite' ? 'esri' :
        e?.name === 'OpenFreeMap Liberty' ? 'liberty' :
        e?.name === 'NLSC 台灣電子地圖' ? 'nlsc' :
        e?.name === 'GSI 日本地理院地圖' ? 'gsi' : 'osm';
      localStorage.setItem('locwarp.tile_layer', key);
    } catch { /* storage disabled */ }
  });
}
