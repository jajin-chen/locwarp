import { useCallback, useEffect } from 'react';
import type { Dispatch, MutableRefObject, SetStateAction } from 'react';
import L from 'leaflet';
import type { Position } from './types';

// Recenter button sync + follow-mode toggle sync + auto-pan. The DOM
// buttons were mounted once by useMapInit; these effects keep their
// handlers and visual state in step with React state.
export function useFollowMode({
  mapRef,
  recenterBtnRef,
  recenterHandlerRef,
  followBtnRef,
  followHandlerRef,
  currentPosition,
  followMode,
  setFollowMode,
  t,
}: {
  mapRef: MutableRefObject<L.Map | null>;
  recenterBtnRef: MutableRefObject<HTMLButtonElement | null>;
  recenterHandlerRef: MutableRefObject<() => void>;
  followBtnRef: MutableRefObject<HTMLButtonElement | null>;
  followHandlerRef: MutableRefObject<() => void>;
  currentPosition: Position | null;
  followMode: boolean;
  setFollowMode: Dispatch<SetStateAction<boolean>>;
  t: (k: any, v?: any) => string;
}): void {
  const recenter = useCallback(() => {
    const map = mapRef.current;
    if (!map || !currentPosition) return;
    map.setView([currentPosition.lat, currentPosition.lng], Math.max(map.getZoom(), 16), {
      animate: true,
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentPosition]);

  // Keep the DOM recenter button's handler + disabled state in sync with
  // React state without re-creating the button on every render.
  useEffect(() => {
    recenterHandlerRef.current = recenter;
    const btn = recenterBtnRef.current;
    if (!btn) return;
    btn.disabled = !currentPosition;
    btn.style.background = currentPosition ? '#6c8cff' : 'var(--bg-surface, #2a2f3a)';
    btn.style.cursor = currentPosition ? 'pointer' : 'not-allowed';
    btn.style.opacity = currentPosition ? '1' : '0.55';
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [recenter, currentPosition]);

  const toggleFollow = useCallback(() => {
    setFollowMode((prev) => !prev);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Sync the follow button's visual state + handler with React state. Active
  // (blue) when on, neutral surface when off. Title flips between on/off
  // labels so hover tooltip mirrors current state.
  useEffect(() => {
    followHandlerRef.current = toggleFollow;
    const btn = followBtnRef.current;
    if (!btn) return;
    btn.style.background = followMode ? '#6c8cff' : 'var(--bg-surface, #2a2f3a)';
    btn.style.cursor = 'pointer';
    btn.style.opacity = '1';
    btn.title = t(followMode ? 'map.follow_on' : 'map.follow_off');
    btn.setAttribute('aria-pressed', followMode ? 'true' : 'false');
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [toggleFollow, followMode, t]);

  // Auto-pan the map to the current position whenever follow mode is on.
  // Uses panTo with a short animation so rapid backend ticks (random walk
  // can be ~10 Hz) blend into a smooth camera trail rather than jumpy
  // snaps. Programmatic panTo does NOT fire dragstart, so the auto-disable
  // wired at map init is safe.
  useEffect(() => {
    if (!followMode || !currentPosition) return;
    const map = mapRef.current;
    if (!map) return;
    map.panTo([currentPosition.lat, currentPosition.lng], {
      animate: true,
      duration: 0.4,
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentPosition, followMode]);
}
