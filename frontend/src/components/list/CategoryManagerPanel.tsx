import { useState } from 'react';
import { useT } from '../../i18n';
import { useDismissOnOutside } from '../../hooks/useDismissOnOutside';

// Preset palette for the color picker. Covers warm + cool + neutral so every
// category can find a visually distinct slot.
const COLOR_PALETTE = [
  '#ef4444', '#f97316', '#eab308', '#22c55e',
  '#14b8a6', '#3b82f6', '#6366f1', '#a855f7',
  '#ec4899', '#64748b',
];

// A category as seen by the shared manager panel. Bookmarks key categories by
// name, routes by id — `key` abstracts over that.
export interface ManagedCategory {
  key: string;         // stable identity passed back through the callbacks
  name: string;        // raw name (rename compares the input against this)
  displayName: string; // shown label (post-translation)
  color: string;       // resolved dot color
  canEdit: boolean;    // false for the built-in default category
}

interface CategoryManagerPanelProps {
  // Rendered unconditionally by the host so edit state survives toggling the
  // panel closed and open again; `open` controls visibility.
  open: boolean;
  title: string;
  categories: ManagedCategory[];
  onAdd?: (name: string) => void;
  onDelete?: (key: string) => void;
  onRename?: (key: string, newName: string) => void;
  onRecolor?: (key: string, color: string) => void;
}

// Category manager panel: rename / recolor / delete each category, plus an
// add row. Shared between the bookmark and route lists.
export function CategoryManagerPanel({
  open,
  title,
  categories,
  onAdd,
  onDelete,
  onRename,
  onRecolor,
}: CategoryManagerPanelProps) {
  const t = useT();
  const [newCategoryName, setNewCategoryName] = useState('');
  const [editingCategory, setEditingCategory] = useState<string | null>(null);
  const [editCategoryName, setEditCategoryName] = useState('');
  // Key of the category whose dot is currently being recolored (shows popover).
  const [colorPickerFor, setColorPickerFor] = useState<string | null>(null);

  // Dismiss the category color picker on outside click / ESC.
  useDismissOnOutside(colorPickerFor, '[data-category-color-picker]', () => setColorPickerFor(null));

  if (!open) return null;
  return (
    <div
      style={{
        background: '#2a2a2e',
        border: '1px solid #444',
        borderRadius: 6,
        padding: 12,
        marginBottom: 8,
      }}
    >
      <div style={{ fontSize: 11, fontWeight: 600, marginBottom: 6, opacity: 0.7 }}>
        {title}
      </div>
      {categories.map((cat) => (
        <div
          key={cat.key}
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 6,
            padding: '4px 0',
            fontSize: 12,
            position: 'relative',
          }}
        >
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              if (!onRecolor) return;
              setColorPickerFor((prev) => (prev === cat.key ? null : cat.key));
            }}
            title={t('bm.recolor_tooltip')}
            style={{
              width: 14,
              height: 14,
              borderRadius: '50%',
              background: cat.color,
              border: '1.5px solid rgba(255,255,255,0.15)',
              padding: 0,
              cursor: onRecolor ? 'pointer' : 'default',
              flexShrink: 0,
              boxShadow: '0 1px 2px rgba(0,0,0,0.3)',
            }}
          />
          {colorPickerFor === cat.key && onRecolor && (
            <div
              data-category-color-picker
              onClick={(e) => e.stopPropagation()}
              style={{
                position: 'absolute',
                top: 22, left: 0, zIndex: 50,
                background: '#1e1e22',
                border: '1px solid rgba(255,255,255,0.15)',
                borderRadius: 6,
                padding: 6,
                boxShadow: '0 6px 18px rgba(0,0,0,0.5)',
                display: 'grid',
                gridTemplateColumns: 'repeat(5, 22px)',
                gap: 4,
              }}
            >
              {COLOR_PALETTE.map((c) => {
                const selected = cat.color.toLowerCase() === c.toLowerCase();
                return (
                  <button
                    key={c}
                    type="button"
                    onClick={(e) => {
                      e.stopPropagation();
                      onRecolor(cat.key, c);
                      setColorPickerFor(null);
                    }}
                    style={{
                      width: 22, height: 22, borderRadius: '50%',
                      background: c,
                      border: selected
                        ? '2px solid #fff'
                        : '1.5px solid rgba(255,255,255,0.12)',
                      cursor: 'pointer', padding: 0,
                      transition: 'transform 0.1s',
                    }}
                    title={c}
                  />
                );
              })}
              <input
                type="color"
                value={cat.color}
                onChange={(e) => onRecolor(cat.key, e.target.value)}
                title={t('bm.recolor_custom')}
                style={{
                  gridColumn: '1 / span 5',
                  width: '100%', height: 22,
                  border: '1px solid rgba(255,255,255,0.12)',
                  borderRadius: 4, padding: 0, marginTop: 2,
                  background: '#1e1e22',
                  cursor: 'pointer',
                }}
              />
            </div>
          )}
          {editingCategory === cat.key ? (
            <input
              type="text"
              className="search-input"
              autoFocus
              value={editCategoryName}
              onChange={(e) => setEditCategoryName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  const next = editCategoryName.trim();
                  if (next && next !== cat.name && onRename) onRename(cat.key, next);
                  setEditingCategory(null);
                }
                if (e.key === 'Escape') setEditingCategory(null);
              }}
              onBlur={() => setEditingCategory(null)}
              style={{ flex: 1, padding: '2px 4px', fontSize: 12 }}
            />
          ) : (
            <span style={{ flex: 1 }}>{cat.displayName}</span>
          )}
          {cat.canEdit && onRename && editingCategory !== cat.key && (
            <button
              onClick={() => { setEditingCategory(cat.key); setEditCategoryName(cat.name); }}
              title={t('bm.rename_category')}
              style={{
                background: 'none',
                border: 'none',
                color: 'var(--fg-muted, #888)',
                cursor: 'pointer',
                padding: '2px 4px',
                fontSize: 11,
              }}
            >
              <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M11 4H4a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7" />
                <path d="M18.5 2.5a2.12 2.12 0 013 3L12 15l-4 1 1-4 9.5-9.5z" />
              </svg>
            </button>
          )}
          {cat.canEdit && onDelete && (
            <button
              onClick={() => onDelete(cat.key)}
              style={{
                background: 'none',
                border: 'none',
                color: '#f44336',
                cursor: 'pointer',
                padding: '2px 4px',
                fontSize: 11,
              }}
            >
              <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <line x1="18" y1="6" x2="6" y2="18" />
                <line x1="6" y1="6" x2="18" y2="18" />
              </svg>
            </button>
          )}
        </div>
      ))}
      {onAdd && (
        <div style={{ display: 'flex', gap: 6, marginTop: 8 }}>
          <input
            type="text"
            className="search-input"
            placeholder={t('bm.add_category')}
            value={newCategoryName}
            onChange={(e) => setNewCategoryName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && newCategoryName.trim()) {
                onAdd(newCategoryName.trim());
                setNewCategoryName('');
              }
            }}
            style={{ flex: 1 }}
          />
          <button
            className="action-btn"
            onClick={() => {
              if (newCategoryName.trim()) {
                onAdd(newCategoryName.trim());
                setNewCategoryName('');
              }
            }}
            style={{ fontSize: 11 }}
          >
            {t('bm.new_category')}
          </button>
        </div>
      )}
    </div>
  );
}
