import React, { useRef, useEffect, useState, useCallback } from 'react';
import { useT } from '../i18n';
import TransportButtons from './map/TransportButtons';
import CoordInputStrip from './map/CoordInputStrip';
import S2LevelPicker from './map/S2LevelPicker';
import RecentPlacesPopover from './map/RecentPlacesPopover';
import MapContextMenu from './map/MapContextMenu';
import WaypointMenu from './map/WaypointMenu';
import type { Position, Waypoint, ContextMenuState } from './map/types';
import { usePositionMarker } from './map/usePositionMarker';
import { useDestinationMarker } from './map/useDestinationMarker';
import { usePreviewPin } from './map/usePreviewPin';
import { useWaypointLayer } from './map/useWaypointLayer';
import { useBookmarkClusters } from './map/useBookmarkClusters';
import { useRoutePolylines } from './map/useRoutePolylines';
import { useRadiusCircle } from './map/useRadiusCircle';
import { useFlowerPreview } from './map/useFlowerPreview';
import { useDualDeviceOverlays } from './map/useDualDeviceOverlays';
import { useMapInit } from './map/useMapInit';
import { useFollowMode } from './map/useFollowMode';
import { useS2Grid } from './map/useS2Grid';

import type { RuntimesMap } from '../hooks/useSimulation';
import type { DeviceInfo } from '../hooks/useDevice';

interface MapViewProps {
  currentPosition: Position | null;
  destination: Position | null;
  waypoints: Waypoint[];
  routePath: Position[];
  // 種花模式 preview: one segmented polygon (the circle the avatar will walk)
  // per waypoint, drawn so the user sees the configured radius + segment
  // count before / while running.
  flowerPreview?: Position[][];
  randomWalkRadius: number | null;
  // Fixed sampling centre of an active random walk (broadcast by the backend
  // on start). When set and mode is "fixed", the radius circle pins here.
  randomWalkCenter?: Position | null;
  randomWalkCenterMode?: 'fixed' | 'follow';
  onMapClick: (lat: number, lng: number) => void;
  onTeleport: (lat: number, lng: number, source?: 'menu' | 'coord') => void;
  onNavigate: (lat: number, lng: number, source?: 'menu' | 'coord') => void;
  onAddBookmark: (lat: number, lng: number) => void;
  onAddWaypoint?: (lat: number, lng: number) => void;
  // Left-click on a waypoint marker opens a small action menu. Both
  // handlers are optional — when undefined, the waypoint marker stays
  // tooltip-only (legacy behaviour).
  onSetWpAsStart?: (index: number) => void;
  onRemoveWaypoint?: (index: number) => void;
  // Arms a one-shot "insert after this waypoint" mode. Parent shows
  // the cancel banner and turns insertAfterActive on; the next map
  // click consumes the mode and inserts the new waypoint at index+1.
  onInsertAfterWp?: (index: number) => void;
  // When true the map cursor swaps to crosshair so the user knows the
  // next click will splice a new waypoint into the route.
  insertAfterActive?: boolean;
  showWaypointOption?: boolean;
  deviceConnected?: boolean;
  onShowToast?: (msg: string) => void;
  // HTML snippet to paint into the current-position divIcon. Lets the parent
  // swap the "blue person" for one of the preset characters or a user-
  // uploaded PNG. Empty / undefined = fall back to the built-in default.
  userAvatarHtml?: string;
  // Group mode: when runtimes + devices are present and 2+ devices connected,
  // render per-device markers/polylines/circles. Single-device rendering is
  // still driven by the legacy currentPosition/destination/routePath props.
  runtimes?: RuntimesMap;
  devices?: DeviceInfo[];
  // Optional bookmark list to render as small clickable markers. When
  // enabled, clicking a marker calls onTeleport at that coordinate.
  bookmarkPins?: Array<{ id?: string; name: string; lat: number; lng: number; country_code?: string }>;
  showBookmarkPins?: boolean;
  // Imperative escape hatch so non-map components (e.g. the StatusBar's
  // "Locate PC" pan-only flow) can move the map view without going
  // through React state.
  onMapReady?: (api: {
    panTo: (lat: number, lng: number, zoom?: number) => void;
    fitBounds: (points: { lat: number; lng: number }[]) => void;
    getCenter: () => { lat: number; lng: number } | null;
  }) => void;
  // Preview-only pin: rendered when the user previews a coord (camera-only
  // fly) so they can see exactly where they're looking on the map. Distinct
  // shape + amber color so it doesn't get confused with the red destination
  // marker. Cleared by the parent when a real teleport / clear runs.
  previewPin?: Position | null;
  onPreviewPinClear?: () => void;
  // Triggered by the coord-input overlay's "Preview" button. The parent
  // owns both the map-pan and the preview-pin state so we route through
  // it instead of touching mapRef locally.
  onCoordPreview?: (lat: number, lng: number) => void;
  // Recent destinations (last 20 teleport / navigate / search actions).
  // Rendered in a topright popover so the user can re-fly in one click.
  recentPlaces?: Array<{ lat: number; lng: number; kind: 'teleport' | 'navigate' | 'search' | 'coord_teleport' | 'coord_navigate'; name: string; ts: number }>;
  onRecentReFly?: (entry: { lat: number; lng: number; kind: 'teleport' | 'navigate' | 'search' | 'coord_teleport' | 'coord_navigate'; name: string }) => void;
  onRecentClear?: () => void;
  // Click handler for the topleft library shortcut. Opens the
  // bookmarks / routes panel without the user having to scroll down
  // to the ControlPanel's library button.
  onOpenLibrary?: () => void;
  // Transport (start / stop / pause) — moved from the sidebar to the
  // bottom-left of the map so they sit just above the coord-input strip.
  isRunning?: boolean;
  isPaused?: boolean;
  onStart?: () => void;
  onStop?: () => void;
  onPause?: () => void;
  onResume?: () => void;
  // Bulk-paste route shortcut on the map (next to the library star),
  // visible only in MultiStop / Loop modes.
  showBulkPasteOnMap?: boolean;
  onBulkPasteOpen?: () => void;
}

const MapView: React.FC<MapViewProps> = ({
  currentPosition,
  destination,
  waypoints,
  routePath,
  flowerPreview,
  randomWalkRadius,
  randomWalkCenter,
  randomWalkCenterMode = 'fixed',
  onMapClick,
  onTeleport,
  onNavigate,
  onAddBookmark,
  onAddWaypoint,
  onSetWpAsStart,
  onRemoveWaypoint,
  onInsertAfterWp,
  insertAfterActive,
  showWaypointOption,
  deviceConnected = true,
  onShowToast,
  userAvatarHtml,
  runtimes,
  devices,
  bookmarkPins,
  showBookmarkPins,
  onMapReady,
  previewPin,
  onPreviewPinClear,
  onCoordPreview,
  recentPlaces,
  onRecentReFly,
  onRecentClear,
  onOpenLibrary,
  isRunning,
  isPaused,
  onStart,
  onStop,
  onPause,
  onResume,
  showBulkPasteOnMap,
  onBulkPasteOpen,
}) => {
  // Dual-mode rendering disabled by design: with pre-sync (both devices
  // teleport to the same start before any group action) and shared random
  // seed, the two phones always sit at the exact same coordinate, so two
  // markers and two polylines just overlap and add visual noise. We keep
  // the dual data plumbing (devices, runtimes) for the dual cleanup effect
  // below but always render the single-device view (driven by the primary
  // device's currentPosition / routePath / destination passed in as props).
  const dualMode = false;
  // Suppress unused-prop warnings — kept for API compatibility and the
  // dual-marker cleanup effect that wipes any residual dual markers if a
  // user upgrades from an earlier 0.2.0 build that had them rendered.
  void devices; void runtimes;
  const t = useT();
  // The map-init useEffect only runs once, so its click handler captures the
  // first-render `t`. Language switches then don't reach the tooltip hint.
  // Route lookups through a ref that we keep in sync every render.
  const tRef = useRef(t);
  // onMapClick closure gets captured by the once-per-mount click handler;
  // route through a ref so toggling the prop mid-session takes effect.
  const onMapClickRef = useRef(onMapClick);
  useEffect(() => { onMapClickRef.current = onMapClick; }, [onMapClick]);
  tRef.current = t;
  const prevPositionRef = useRef<Position | null>(null);
  // Library button handler (bottom of the topleft stack). Wired to the
  // App-level callback that bumps `openLibraryToken`, triggering the
  // ControlPanel library panel to open.
  const openLibraryHandlerRef = useRef<() => void>(() => {});
  // followStateRef mirrors followMode so the dragstart handler (wired once
  // at map init) sees the latest value without a stale closure.
  const followStateRef = useRef(false);
  // onShowToast captured by once-mount handlers. Routed through a ref so
  // prop changes mid-session take effect.
  const onShowToastRef = useRef(onShowToast);
  useEffect(() => { onShowToastRef.current = onShowToast; }, [onShowToast]);
  // Bookmark-pin teleport handler routed through a ref so the clustering
  // effect doesn't rebuild the (potentially 50k-point) supercluster index
  // just because the parent passed a new onTeleport closure.
  const onTeleportRef = useRef(onTeleport);
  useEffect(() => { onTeleportRef.current = onTeleport; }, [onTeleport]);
  // Waypoint marker click handlers — kept in refs so the per-marker click
  // handler captured inside the waypoints useEffect always calls the
  // freshest prop without re-creating every marker on each prop change.
  const onSetWpAsStartRef = useRef(onSetWpAsStart);
  useEffect(() => { onSetWpAsStartRef.current = onSetWpAsStart; }, [onSetWpAsStart]);
  const onRemoveWaypointRef = useRef(onRemoveWaypoint);
  useEffect(() => { onRemoveWaypointRef.current = onRemoveWaypoint; }, [onRemoveWaypoint]);
  const onInsertAfterWpRef = useRef(onInsertAfterWp);
  useEffect(() => { onInsertAfterWpRef.current = onInsertAfterWp; }, [onInsertAfterWp]);
  // Mini context menu shown on left-click of a waypoint marker.
  // Independent from the right-click `contextMenu` so opening one does
  // not close / reposition the other.
  const [wpMenu, setWpMenu] = useState<{
    visible: boolean; x: number; y: number; index: number; isStart: boolean;
  }>({ visible: false, x: 0, y: 0, index: 0, isStart: false });
  const closeWpMenu = useCallback(() => {
    setWpMenu((prev) => prev.visible ? { ...prev, visible: false } : prev);
  }, []);
  // clickMarkerRef removed — left-click no longer drops a pin.

  const [followMode, setFollowMode] = useState(false);
  useEffect(() => { followStateRef.current = followMode; }, [followMode]);

  // S2 grid enabled/level state lives in useS2Grid below; only the picker
  // popover open/close flag stays here because the map-init contextmenu
  // handler needs its setter.
  const [s2PickerOpen, setS2PickerOpen] = useState(false);

  const [contextMenu, setContextMenu] = useState<ContextMenuState>({
    visible: false,
    x: 0,
    y: 0,
    lat: 0,
    lng: 0,
  });

  const closeContextMenu = useCallback(() => {
    setContextMenu((prev) => ({ ...prev, visible: false }));
  }, []);

  const {
    mapContainerRef,
    mapRef,
    recenterBtnRef,
    recenterHandlerRef,
    followBtnRef,
    followHandlerRef,
    s2GridBtnRef,
    s2GridHandlerRef,
  } = useMapInit({
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
  });

  usePositionMarker({ mapRef, currentPosition, dualMode, userAvatarHtml, prevPositionRef });

  useDestinationMarker({ mapRef, destination, dualMode, t });

  usePreviewPin({ mapRef, previewPin, onPreviewPinClear, tRef });

  useWaypointLayer({ mapRef, waypoints, tRef, setWpMenu });

  useBookmarkClusters({ mapRef, bookmarkPins, showBookmarkPins, onTeleportRef });

  useRoutePolylines({ mapRef, routePath, dualMode });

  useRadiusCircle({ mapRef, randomWalkRadius, currentPosition, randomWalkCenter, randomWalkCenterMode, dualMode });

  useFlowerPreview({ mapRef, flowerPreview, dualMode });

  useDualDeviceOverlays({ mapRef, dualMode, devices, runtimes, randomWalkRadius, t });

  // Close context menu on outside click
  useEffect(() => {
    const handler = () => closeContextMenu();
    document.addEventListener('click', handler);
    return () => document.removeEventListener('click', handler);
  }, [closeContextMenu]);

  // Close the waypoint mini-menu on any outside click. Same pattern as
  // closeContextMenu — clicking inside the menu calls stopPropagation
  // there so this fires only for clicks that miss the menu surface.
  useEffect(() => {
    const handler = () => closeWpMenu();
    document.addEventListener('click', handler);
    return () => document.removeEventListener('click', handler);
  }, [closeWpMenu]);

  useFollowMode({
    mapRef,
    recenterBtnRef,
    recenterHandlerRef,
    followBtnRef,
    followHandlerRef,
    currentPosition,
    followMode,
    setFollowMode,
    t,
  });

  // Keep the library-shortcut button ref in sync with the parent
  // callback. The button itself was mounted once in the map init
  // effect; this useEffect just rewires the target on each render.
  useEffect(() => {
    openLibraryHandlerRef.current = onOpenLibrary ?? (() => {});
  }, [onOpenLibrary]);


  const { s2Enabled, setS2Enabled, s2Level, setS2Level, s2Suppressed } = useS2Grid({ mapRef, s2GridBtnRef, s2GridHandlerRef, t });


  // Lifts the coord-input strip to clear the bottom status bar. The
  // status bar wraps to extra rows when the window narrows (flexWrap),
  // so its rendered height varies. We observe it directly so the strip
  // sits exactly 12px above whatever height it ends up.
  const [statusBarHeight, setStatusBarHeight] = useState<number>(38);
  useEffect(() => {
    const el = document.querySelector('.status-bar') as HTMLElement | null;
    if (!el) return;
    const update = () => setStatusBarHeight(Math.ceil(el.getBoundingClientRect().height));
    update();
    const ro = new ResizeObserver(update);
    ro.observe(el);
    window.addEventListener('resize', update);
    return () => {
      ro.disconnect();
      window.removeEventListener('resize', update);
    };
  }, []);

  // When insert-after-waypoint mode is armed, swap the leaflet drag/grab
  // cursor to a crosshair so the user has a visual cue that the next
  // map click drops a new waypoint (and isn't a no-op or a teleport).
  // Scoped via inline <style> so it only affects THIS map instance —
  // a global stylesheet rule would bleed into any other Leaflet map.
  return (
    <div
      className={`map-container${insertAfterActive ? ' wp-insert-mode' : ''}`}
      style={{ position: 'relative', flex: 1 }}
    >
      {insertAfterActive && (
        <style>{`
          .map-container.wp-insert-mode .leaflet-container,
          .map-container.wp-insert-mode .leaflet-grab,
          .map-container.wp-insert-mode .leaflet-interactive {
            cursor: crosshair !important;
          }
        `}</style>
      )}
      <div ref={mapContainerRef} style={{ width: '100%', height: '100%' }} />

      {/* Bottom-left stack: Bulk-paste (route/multi only) > Transport >
          Coord-input. Single flex column at bottom-left, fixed gap so the
          rows don't drift apart based on container height. Sits exactly
          above the bottom status bar — height tracked dynamically so a
          wrapped (multi-row) status bar doesn't overlap this strip when
          the window is narrow. */}
      <div
        style={{
          position: 'absolute',
          left: 12,
          bottom: statusBarHeight + 22,
          zIndex: 851,
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'flex-start',
          gap: 8,
          // The wrapper sits as a sibling of the Leaflet container (not a
          // child) so mousedown landing on its invisible bounding box —
          // the gap between rows, or the area to the right of a narrow
          // child like the lone Start button — never reaches Leaflet's
          // drag handler and the map appears un-draggable there (issue
          // #29). Make the wrapper transparent to pointer events and
          // re-enable on each actual button/input.
          pointerEvents: 'none',
        }}
      >
        {showBulkPasteOnMap && onBulkPasteOpen && (
          <button
            onClick={onBulkPasteOpen}
            onMouseDown={(e) => e.stopPropagation()}
            title={tRef.current('panel.route_paste_tooltip')}
            style={{
              display: 'inline-flex', alignItems: 'center', gap: 6,
              padding: '7px 12px', height: 32, fontSize: 12,
              color: '#e8eaff', fontWeight: 600,
              background: 'rgba(20, 23, 34, 0.88)',
              backdropFilter: 'blur(14px) saturate(160%)',
              WebkitBackdropFilter: 'blur(14px) saturate(160%)',
              border: '1px solid rgba(108, 140, 255, 0.32)',
              borderRadius: 10,
              boxShadow: '0 10px 26px rgba(8, 11, 22, 0.5)',
              cursor: 'pointer',
              pointerEvents: 'auto',
            }}
          >
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <rect x="9" y="2" width="6" height="4" rx="1"/>
              <path d="M9 4H6a2 2 0 00-2 2v14a2 2 0 002 2h12a2 2 0 002-2V6a2 2 0 00-2-2h-3"/>
            </svg>
            {tRef.current('panel.route_paste_button')}
          </button>
        )}
        <TransportButtons
          isRunning={!!isRunning}
          isPaused={!!isPaused}
          onStart={onStart}
          onStop={onStop}
          onPause={onPause}
          onResume={onResume}
          t={tRef}
        />
        {/* Coord input strip — relative positioning inside the flex
            column so the gap above is purely controlled by the parent. */}
        <CoordInputStrip
          mapRef={mapRef}
          t={t}
          tRef={tRef}
          deviceConnected={deviceConnected}
          onShowToast={onShowToast}
          onTeleport={onTeleport}
          onNavigate={onNavigate}
          onCoordPreview={onCoordPreview}
        />
      </div>

      {/* S2 cell grid level picker — opens via right-click on the S2 toggle
          button. Snaps to discrete levels 8..22, default 17 (Niantic decor
          cell). */}
      {s2PickerOpen && (
        <S2LevelPicker
          mapRef={mapRef}
          tRef={tRef}
          s2Enabled={s2Enabled}
          setS2Enabled={setS2Enabled}
          s2Level={s2Level}
          setS2Level={setS2Level}
          s2Suppressed={s2Suppressed}
          onClose={() => setS2PickerOpen(false)}
        />
      )}

      {/* Recent destinations button + popover (topright, below tile layer
          switcher). Open/close, clear-confirm, and drag state live inside
          map/RecentPlacesPopover. */}
      {(recentPlaces && recentPlaces.length > 0) && (
        <RecentPlacesPopover
          recentPlaces={recentPlaces}
          onRecentReFly={onRecentReFly}
          onRecentClear={onRecentClear}
          tRef={tRef}
        />
      )}

      {contextMenu.visible && (
        <MapContextMenu
          x={contextMenu.x}
          y={contextMenu.y}
          lat={contextMenu.lat}
          lng={contextMenu.lng}
          deviceConnected={deviceConnected}
          showWaypointOption={showWaypointOption}
          onTeleport={onTeleport}
          onNavigate={onNavigate}
          onAddBookmark={onAddBookmark}
          onAddWaypoint={onAddWaypoint}
          onShowToast={onShowToast}
          t={t}
          tRef={tRef}
          onClose={closeContextMenu}
        />
      )}

      {wpMenu.visible && (
        <WaypointMenu
          x={wpMenu.x}
          y={wpMenu.y}
          index={wpMenu.index}
          isStart={wpMenu.isStart}
          onSetWpAsStartRef={onSetWpAsStartRef}
          onRemoveWaypointRef={onRemoveWaypointRef}
          onInsertAfterWpRef={onInsertAfterWpRef}
          t={t}
          tRef={tRef}
          onClose={closeWpMenu}
        />
      )}
    </div>
  );
};

export default MapView;
