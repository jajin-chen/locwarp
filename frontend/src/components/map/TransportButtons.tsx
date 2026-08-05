import React from 'react';
import type { TRef } from './types';

// Transport (Start / Stop / Pause / Resume) — bottom-left of the map,
// directly above the coord-input strip. Mockup S6 (玻璃膠囊) base with
// the active state filled D8 (sliding highlight). Width fits content
// only — we don't want the whole row to span the coord input below.
function TransportButtons({
  isRunning,
  isPaused,
  onStart,
  onStop,
  onPause,
  onResume,
  t,
}: {
  isRunning: boolean;
  isPaused: boolean;
  onStart?: () => void;
  onStop?: () => void;
  onPause?: () => void;
  onResume?: () => void;
  t: TRef;
}) {
  // Don't render if no callbacks were wired (defensive).
  if (!onStart && !onStop && !onPause && !onResume) return null;
  const label = (k: string) => {
    try { return t.current(k as any); } catch { return ''; }
  };
  return (
    <div
      onMouseDown={(e) => e.stopPropagation()}
      onClick={(e) => e.stopPropagation()}
      style={{
        // Static (not absolute): sits inside the bottom-left stack
        // wrapper so its vertical position is dictated by the flex
        // column rather than a hand-tuned bottom value.
        display: 'inline-flex',
        alignSelf: 'flex-start',
        alignItems: 'center',
        gap: 0,
        padding: 4,
        background: 'rgba(20, 23, 34, 0.78)',
        backdropFilter: 'blur(14px) saturate(160%)',
        WebkitBackdropFilter: 'blur(14px) saturate(160%)',
        border: '1px solid rgba(108, 140, 255, 0.22)',
        borderRadius: 10,
        boxShadow: '0 10px 26px rgba(8, 11, 22, 0.5)',
        pointerEvents: 'auto',
      }}
    >
      {!isRunning && (
        <button
          className="lw-transport-btn lw-transport-start"
          onClick={onStart}
          title={label('generic.start')}
        >
          <svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor"><polygon points="5,3 19,12 5,21" /></svg>
          {label('generic.start')}
        </button>
      )}
      {isRunning && (
        <button
          className="lw-transport-btn lw-transport-stop"
          onClick={onStop}
          title={label('generic.stop')}
        >
          <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><rect x="4" y="4" width="16" height="16" rx="2" /></svg>
          {label('generic.stop')}
        </button>
      )}
      {isRunning && !isPaused && (
        <button
          className="lw-transport-btn lw-transport-pause"
          onClick={onPause}
          title={label('generic.pause')}
        >
          <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><rect x="5" y="4" width="5" height="16" rx="1" /><rect x="14" y="4" width="5" height="16" rx="1" /></svg>
          {label('generic.pause')}
        </button>
      )}
      {isRunning && isPaused && (
        <button
          className="lw-transport-btn lw-transport-resume"
          onClick={onResume}
          title={label('generic.resume')}
        >
          <svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor"><polygon points="5,3 19,12 5,21" /></svg>
          {label('generic.resume')}
        </button>
      )}
    </div>
  );
}

export default TransportButtons;
