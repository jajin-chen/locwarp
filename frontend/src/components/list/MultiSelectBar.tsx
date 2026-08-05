import React from 'react';
import { useT } from '../../i18n';

interface MultiSelectBarProps {
  // Every selectable id in the list (used by the select-all toggle).
  allIds: string[];
  // Denominator shown in the "selected / total" counter.
  totalCount: number;
  selectedIds: Set<string>;
  setSelectedIds: React.Dispatch<React.SetStateAction<Set<string>>>;
  onBulkDelete: () => void;
  // Let the row wrap on narrow panels (route list adds a move-to select).
  wrap?: boolean;
  // Extra controls rendered between the select-all button and the counter.
  children?: React.ReactNode;
}

// Multi-select toolbar — sticks to the bottom of the scroll area so the user
// can scroll through the list unchecking items to keep, then hit Delete
// without scrolling back up.
export function MultiSelectBar({
  allIds,
  totalCount,
  selectedIds,
  setSelectedIds,
  onBulkDelete,
  wrap,
  children,
}: MultiSelectBarProps) {
  const t = useT();
  return (
    <div
      style={{
        position: 'sticky',
        bottom: -12, zIndex: 10,
        marginLeft: -12, marginRight: -12,
        marginTop: 16,
        padding: '8px 12px',
        background: 'rgba(26, 29, 39, 0.98)',
        backdropFilter: 'blur(6px)',
        borderTop: '1px solid rgba(108,140,255,0.35)',
        boxShadow: '0 -6px 12px rgba(0,0,0,0.35)',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11, flexWrap: wrap ? 'wrap' : undefined }}>
        <button
          className="action-btn"
          onClick={() => {
            if (selectedIds.size === allIds.length) {
              setSelectedIds(new Set());
            } else {
              setSelectedIds(new Set(allIds));
            }
          }}
          style={{ padding: '3px 8px', fontSize: 11 }}
        >
          {selectedIds.size === totalCount && totalCount > 0
            ? t('bm.deselect_all')
            : t('bm.select_all')}
        </button>
        {children}
        <span style={{ opacity: 0.7, marginLeft: 'auto' }}>
          {selectedIds.size} / {totalCount}
        </span>
        <button
          className="action-btn"
          onClick={onBulkDelete}
          disabled={selectedIds.size === 0}
          style={{
            padding: '3px 10px', fontSize: 11, fontWeight: 600,
            color: selectedIds.size === 0 ? '#888' : '#ff6b6b',
            borderColor: selectedIds.size === 0 ? undefined : 'rgba(255,107,107,0.4)',
            cursor: selectedIds.size === 0 ? 'not-allowed' : 'pointer',
          }}
        >
          {t('bm.delete_selected').replace('{n}', String(selectedIds.size))}
        </button>
      </div>
    </div>
  );
}
