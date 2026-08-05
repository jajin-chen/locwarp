import React from 'react';
import type { MutableRefObject } from 'react';
import { contextMenuItemStyle, highlightItem, unhighlightItem } from './menuStyles';
import type { TRef } from './types';

// Mini menu shown on left-click of a waypoint marker (set as start /
// insert after / delete). Handlers arrive as refs so the menu always
// calls the freshest parent callback without re-rendering the markers.
function WaypointMenu({
  x,
  y,
  index,
  isStart,
  onSetWpAsStartRef,
  onRemoveWaypointRef,
  onInsertAfterWpRef,
  t,
  tRef,
  onClose,
}: {
  x: number;
  y: number;
  index: number;
  isStart: boolean;
  onSetWpAsStartRef: MutableRefObject<((index: number) => void) | undefined>;
  onRemoveWaypointRef: MutableRefObject<((index: number) => void) | undefined>;
  onInsertAfterWpRef: MutableRefObject<((index: number) => void) | undefined>;
  t: (k: any, v?: any) => string;
  tRef: TRef;
  onClose: () => void;
}) {
  return (
    <div
      className="context-menu anim-scale-in-tl"
      style={{
        position: 'fixed',
        // Offset slightly so the cursor lands inside the menu rather
        // than on its edge (otherwise the document-level click handler
        // might immediately close it).
        left: Math.max(8, Math.min(x + 6, window.innerWidth - 188)),
        top: Math.max(8, Math.min(y + 6, window.innerHeight - 100)),
        zIndex: 1000,
        background: 'rgba(26, 29, 39, 0.96)',
        backdropFilter: 'blur(10px)',
        WebkitBackdropFilter: 'blur(10px)',
        border: '1px solid rgba(108, 140, 255, 0.18)',
        borderRadius: 10,
        padding: '4px 0',
        boxShadow: '0 10px 32px rgba(12, 18, 40, 0.55), 0 0 0 1px rgba(255, 255, 255, 0.04) inset',
        minWidth: 180,
      }}
      onClick={(e) => e.stopPropagation()}
    >
      <div
        style={{
          padding: '6px 14px 4px',
          fontSize: 11,
          opacity: 0.55,
          fontFamily: 'monospace',
          borderBottom: '1px solid rgba(255,255,255,0.05)',
          marginBottom: 2,
        }}
      >
        {isStart ? tRef.current('panel.waypoint_start') : `#${index}`}
      </div>
      {!isStart && onSetWpAsStartRef.current && (
        <div
          style={contextMenuItemStyle}
          onMouseEnter={highlightItem}
          onMouseLeave={unhighlightItem}
          onClick={() => {
            const fn = onSetWpAsStartRef.current;
            const idx = index;
            onClose();
            fn?.(idx);
          }}
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#43a047" strokeWidth="2" style={{ marginRight: 8 }}>
            <line x1="4" y1="22" x2="4" y2="3" />
            <path d="M4 4h12l-2 4 2 4H4" fill="#43a04733" />
          </svg>
          {t('map.wp_set_as_start')}
        </div>
      )}
      {onInsertAfterWpRef.current && (
        <div
          style={contextMenuItemStyle}
          onMouseEnter={highlightItem}
          onMouseLeave={unhighlightItem}
          onClick={() => {
            const fn = onInsertAfterWpRef.current;
            const idx = index;
            onClose();
            fn?.(idx);
          }}
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#6c8cff" strokeWidth="2" style={{ marginRight: 8 }}>
            <line x1="12" y1="5" x2="12" y2="19" />
            <line x1="5" y1="12" x2="19" y2="12" />
          </svg>
          {t('map.wp_insert_after')}
        </div>
      )}
      {onRemoveWaypointRef.current && (
        <div
          style={{ ...contextMenuItemStyle, color: '#ff6b6b' }}
          onMouseEnter={highlightItem}
          onMouseLeave={unhighlightItem}
          onClick={() => {
            const fn = onRemoveWaypointRef.current;
            const idx = index;
            onClose();
            fn?.(idx);
          }}
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ marginRight: 8 }}>
            <polyline points="3 6 5 6 21 6" />
            <path d="M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6" />
          </svg>
          {t('map.wp_delete')}
        </div>
      )}
    </div>
  );
}

export default WaypointMenu;
