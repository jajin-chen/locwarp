import React, { useState, useEffect, useRef, useMemo } from 'react';
import { createPortal } from 'react-dom';
import { useT } from '../i18n';

export interface RouteCategory {
  id: string;
  name: string;
  color?: string;
  sort_order?: number;
}

export interface SavedRoute {
  id: string;
  name: string;
  waypoints: { lat: number; lng: number }[];
  profile?: string;
  category_id?: string;
  created_at?: string;
  updated_at?: string;
}

interface RouteListProps {
  routes: SavedRoute[];
  categories: RouteCategory[];
  currentWaypointsCount: number;

  onRouteLoad: (id: string) => void;
  onRouteSave: (name: string, opts?: { categoryId?: string; overwriteId?: string }) => void;
  onRouteRename: (id: string, name: string) => void;
  onRouteDelete: (id: string) => void;
  onRoutesBulkDelete?: (ids: string[]) => Promise<void> | void;
  onRouteMove?: (ids: string[], targetCategoryId: string) => Promise<void> | void;
  onRouteGpxExport?: (id: string) => void;
  onRouteGpxImport?: (file: File) => Promise<void> | void;

  onCategoryAdd?: (name: string, color?: string) => Promise<void> | void;
  onCategoryDelete?: (id: string) => Promise<void> | void;
  onCategoryRename?: (id: string, name: string) => Promise<void> | void;
  onCategoryRecolor?: (id: string, color: string) => Promise<void> | void;
  // Persist a new category order. Receives the full ordered list of category
  // IDs in the desired display order.
  onCategoryReorder?: (orderedIds: string[]) => Promise<void> | void;
  // Persist a new route order within a single category. Receives the
  // category ID and the ordered list of route IDs.
  onRouteReorder?: (categoryId: string, orderedRouteIds: string[]) => Promise<void> | void;

  routesExportAllUrl?: string;
  onRoutesImportAll?: (file: File) => Promise<void> | void;
}

const COLOR_PALETTE = [
  '#ef4444', '#f97316', '#eab308', '#22c55e',
  '#14b8a6', '#3b82f6', '#6366f1', '#a855f7',
  '#ec4899', '#64748b',
];

const RouteList: React.FC<RouteListProps> = ({
  routes,
  categories,
  currentWaypointsCount,
  onRouteLoad,
  onRouteSave,
  onRouteRename,
  onRouteDelete,
  onRoutesBulkDelete,
  onRouteMove,
  onRouteGpxExport,
  onRouteGpxImport,
  onCategoryAdd,
  onCategoryDelete,
  onCategoryRename,
  onCategoryRecolor,
  onCategoryReorder,
  onRouteReorder,
  routesExportAllUrl,
  onRoutesImportAll,
}) => {
  const t = useT();

  const displayCat = (name: string) => (name === '預設' ? t('bm.default') : name);
  const resolveColor = (cat: RouteCategory): string => cat.color || '#6c8cff';
  const findCategoryById = (id: string | undefined): RouteCategory | undefined =>
    categories.find((c) => c.id === id);

  // ── Save bar state ────────────────────────────────────
  const [routeName, setRouteName] = useState('');
  const [saveCategoryId, setSaveCategoryId] = useState<string>(() => {
    const def = categories.find((c) => c.id === 'default') ?? categories[0];
    return def?.id ?? 'default';
  });
  // Keep the chosen save-target in sync if categories load after first render.
  useEffect(() => {
    if (!categories.find((c) => c.id === saveCategoryId)) {
      setSaveCategoryId(categories.find((c) => c.id === 'default')?.id ?? categories[0]?.id ?? 'default');
    }
  }, [categories, saveCategoryId]);

  // Overwrite-on-save dialog state.
  const [overwritePrompt, setOverwritePrompt] = useState<{
    name: string;
    matches: SavedRoute[];
    chosenId: string;
  } | null>(null);

  const triggerSave = () => {
    const name = routeName.trim();
    if (!name) return;
    if (currentWaypointsCount === 0) return;
    const matches = routes.filter((r) => r.name === name);
    if (matches.length === 0) {
      onRouteSave(name, { categoryId: saveCategoryId });
      setRouteName('');
      return;
    }
    setOverwritePrompt({ name, matches, chosenId: matches[0].id });
  };

  const confirmOverwrite = () => {
    if (!overwritePrompt) return;
    onRouteSave(overwritePrompt.name, { overwriteId: overwritePrompt.chosenId });
    setOverwritePrompt(null);
    setRouteName('');
  };
  const confirmSaveAsNew = () => {
    if (!overwritePrompt) return;
    onRouteSave(overwritePrompt.name, { categoryId: saveCategoryId });
    setOverwritePrompt(null);
    setRouteName('');
  };

  // ── Category panel state ─────────────────────────────
  const [showCategoryMgr, setShowCategoryMgr] = useState(false);
  const [newCategoryName, setNewCategoryName] = useState('');
  const [editingCategory, setEditingCategory] = useState<string | null>(null);
  const [editCategoryName, setEditCategoryName] = useState('');
  const [colorPickerFor, setColorPickerFor] = useState<string | null>(null);

  // ── Sorting ──────────────────────────────────────────
  type SortMode = 'default' | 'name' | 'date_added' | 'date_updated';
  const [sortMode, setSortModeRaw] = useState<SortMode>(() => {
    try {
      const v = localStorage.getItem('locwarp.route_sort') as SortMode | null;
      if (v === 'default' || v === 'name' || v === 'date_added' || v === 'date_updated') return v;
    } catch { /* ignore */ }
    return 'default';
  });
  const setSortMode = (m: SortMode) => {
    setSortModeRaw(m);
    try { localStorage.setItem('locwarp.route_sort', m); } catch { /* ignore */ }
  };
  const sortRoutes = (list: SavedRoute[]): SavedRoute[] => {
    if (sortMode === 'default') return list;
    const copy = [...list];
    if (sortMode === 'name') {
      copy.sort((a, b) => a.name.localeCompare(b.name, 'zh-Hant'));
    } else if (sortMode === 'date_added') {
      copy.sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''));
    } else if (sortMode === 'date_updated') {
      const stamp = (r: SavedRoute) => r.updated_at || r.created_at || '';
      copy.sort((a, b) => stamp(b).localeCompare(stamp(a)));
    }
    return copy;
  };

  // ── Search ───────────────────────────────────────────
  const [search, setSearch] = useState('');

  // ── Collapse persistence (localStorage; simpler than bookmark) ──
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>(() => {
    try {
      const raw = localStorage.getItem('locwarp.route_collapsed');
      return raw ? JSON.parse(raw) : {};
    } catch { return {}; }
  });
  const toggleCategory = (id: string) => {
    setCollapsed((prev) => {
      const next = { ...prev, [id]: !prev[id] };
      try { localStorage.setItem('locwarp.route_collapsed', JSON.stringify(next)); } catch { /* ignore */ }
      return next;
    });
  };

  // ── Reorder mode ─────────────────────────────────────
  // Off by default — opt-in via the toolbar button. When on, the category
  // headers and route rows expose up/down arrows + a drag handle that
  // persist a new order.
  const [reorderMode, setReorderMode] = useState(false);
  const sortBeforeReorderRef = useRef<SortMode | null>(null);
  // Drag-and-drop state.
  const [draggedRouteId, setDraggedRouteId] = useState<string | null>(null);
  const [dragOverRouteId, setDragOverRouteId] = useState<string | null>(null);
  const [dragRouteCat, setDragRouteCat] = useState<string | null>(null);
  const [draggedCatId, setDraggedCatId] = useState<string | null>(null);
  const [dragOverCatId, setDragOverCatId] = useState<string | null>(null);

  // ── Multi-select ─────────────────────────────────────
  const [multiSelect, setMultiSelect] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const toggleSelected = (id: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };
  const exitMultiSelect = () => { setMultiSelect(false); setSelectedIds(new Set()); };
  const handleBulkDelete = async () => {
    if (selectedIds.size === 0) return;
    const msg = t('route.bulk_delete_confirm').replace('{n}', String(selectedIds.size));
    if (!window.confirm(msg)) return;
    const ids = Array.from(selectedIds);
    if (onRoutesBulkDelete) {
      await onRoutesBulkDelete(ids);
    } else {
      await Promise.all(ids.map((id) => Promise.resolve(onRouteDelete(id))));
    }
    exitMultiSelect();
  };

  // ── Context menu ─────────────────────────────────────
  const [contextMenu, setContextMenu] = useState<{ route: SavedRoute; x: number; y: number } | null>(null);
  const [editingRouteId, setEditingRouteId] = useState<string | null>(null);
  const [editingRouteName, setEditingRouteName] = useState('');

  useEffect(() => {
    if (!contextMenu) return;
    const onOutside = (e: Event) => {
      const target = e.target as Element | null;
      if (target && target.closest?.('[data-route-context-menu]')) return;
      setContextMenu(null);
    };
    const onEsc = (e: KeyboardEvent) => { if (e.key === 'Escape') setContextMenu(null); };
    const id = setTimeout(() => {
      document.addEventListener('pointerdown', onOutside);
      document.addEventListener('contextmenu', onOutside);
      document.addEventListener('keydown', onEsc);
    }, 0);
    return () => {
      clearTimeout(id);
      document.removeEventListener('pointerdown', onOutside);
      document.removeEventListener('contextmenu', onOutside);
      document.removeEventListener('keydown', onEsc);
    };
  }, [contextMenu]);

  useEffect(() => {
    if (!colorPickerFor) return;
    const onOutside = (e: Event) => {
      const target = e.target as Element | null;
      if (target && target.closest?.('[data-route-color-picker]')) return;
      setColorPickerFor(null);
    };
    const onEsc = (e: KeyboardEvent) => { if (e.key === 'Escape') setColorPickerFor(null); };
    const id = setTimeout(() => {
      document.addEventListener('pointerdown', onOutside);
      document.addEventListener('keydown', onEsc);
    }, 0);
    return () => {
      clearTimeout(id);
      document.removeEventListener('pointerdown', onOutside);
      document.removeEventListener('keydown', onEsc);
    };
  }, [colorPickerFor]);

  // ── Grouping ────────────────────────────────────────
  const routesByCategory = useMemo(() => {
    const buckets: Record<string, SavedRoute[]> = {};
    for (const cat of categories) buckets[cat.id] = [];
    for (const r of routes) {
      const cid = r.category_id || 'default';
      if (!buckets[cid]) buckets[cid] = [];
      buckets[cid].push(r);
    }
    return buckets;
  }, [routes, categories]);

  // Reorder mode forces sort to 'default' (so the drag / arrow results match
  // the displayed order). User's previous sort is restored on exit.
  const enterReorderMode = () => {
    if (sortMode !== 'default') {
      sortBeforeReorderRef.current = sortMode;
      setSortMode('default');
    } else {
      sortBeforeReorderRef.current = null;
    }
    if (multiSelect) exitMultiSelect();
    setReorderMode(true);
  };
  const exitReorderMode = () => {
    setReorderMode(false);
    setDraggedRouteId(null);
    setDragOverRouteId(null);
    setDragRouteCat(null);
    setDraggedCatId(null);
    setDragOverCatId(null);
    const prev = sortBeforeReorderRef.current;
    if (prev && prev !== 'default') setSortMode(prev);
    sortBeforeReorderRef.current = null;
  };
  const reorderAvailable = reorderMode;
  const dropCategoryOn = (srcId: string, dstId: string) => {
    if (!onCategoryReorder || srcId === dstId) return;
    const srcIdx = categories.findIndex((c) => c.id === srcId);
    const dstIdx = categories.findIndex((c) => c.id === dstId);
    if (srcIdx < 0 || dstIdx < 0) return;
    const arr = categories.map((c) => c.id);
    const [moved] = arr.splice(srcIdx, 1);
    arr.splice(dstIdx, 0, moved);
    void onCategoryReorder(arr);
  };
  const dropRouteOn = (catId: string, srcId: string, dstId: string) => {
    if (!onRouteReorder || srcId === dstId) return;
    const list = routesByCategory[catId] || [];
    const srcIdx = list.findIndex((r) => r.id === srcId);
    const dstIdx = list.findIndex((r) => r.id === dstId);
    if (srcIdx < 0 || dstIdx < 0) return;
    const arr = list.map((r) => r.id);
    const [moved] = arr.splice(srcIdx, 1);
    arr.splice(dstIdx, 0, moved);
    void onRouteReorder(catId, arr);
  };

  // Auto-scroll the surrounding scrollable container during a drag. Native
  // edge-scroll is sluggish on long lists, so we drive a rAF loop ourselves.
  const wrapRef = useRef<HTMLDivElement>(null);
  const dragCursorYRef = useRef(0);
  useEffect(() => {
    const dragging = !!(draggedRouteId || draggedCatId);
    if (!dragging) return;
    let scroller: HTMLElement | null = wrapRef.current?.parentElement ?? null;
    while (scroller) {
      const cs = getComputedStyle(scroller);
      const scrollable = (cs.overflowY === 'auto' || cs.overflowY === 'scroll')
        && scroller.scrollHeight > scroller.clientHeight + 1;
      if (scrollable) break;
      scroller = scroller.parentElement;
    }
    if (!scroller) return;
    const onDragOver = (e: DragEvent) => {
      dragCursorYRef.current = e.clientY;
    };
    window.addEventListener('dragover', onDragOver);
    const EDGE = 80;
    const MAX_SPEED = 18;
    let raf = 0;
    const tick = () => {
      const el = scroller!;
      const r = el.getBoundingClientRect();
      const y = dragCursorYRef.current;
      if (y > 0) {
        const fromTop = y - r.top;
        const fromBottom = r.bottom - y;
        if (fromTop >= 0 && fromTop < EDGE) {
          el.scrollTop -= MAX_SPEED * (1 - fromTop / EDGE);
        } else if (fromBottom >= 0 && fromBottom < EDGE) {
          el.scrollTop += MAX_SPEED * (1 - fromBottom / EDGE);
        }
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener('dragover', onDragOver);
    };
  }, [draggedRouteId, draggedCatId]);

  // ── Render ──────────────────────────────────────────
  return (
    <div ref={wrapRef}>
      {/* Save current bar */}
      <div style={{ fontSize: 11, opacity: 0.6, marginBottom: 6 }}>
        {t('panel.route_save_hint', { n: currentWaypointsCount })}
      </div>
      <div style={{ display: 'flex', gap: 6, marginBottom: 6 }}>
        <input
          type="text"
          className="search-input"
          placeholder={t('panel.route_name')}
          value={routeName}
          onChange={(e) => setRouteName(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') triggerSave(); }}
          style={{ flex: 1 }}
        />
        <select
          value={saveCategoryId}
          onChange={(e) => setSaveCategoryId(e.target.value)}
          style={{
            background: '#1e1e22', color: '#e0e0e0',
            border: '1px solid #444', borderRadius: 4,
            padding: '0 6px', fontSize: 11,
          }}
          title={t('route.save_category_tooltip')}
        >
          {categories.map((c) => (
            <option key={c.id} value={c.id} style={{ background: '#1e1e22', color: '#e0e0e0' }}>
              {displayCat(c.name)}
            </option>
          ))}
        </select>
        <button
          className="action-btn primary"
          disabled={!routeName.trim() || currentWaypointsCount === 0}
          onClick={triggerSave}
        >{t('generic.save')}</button>
      </div>

      {/* Action toolbar */}
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 8, alignItems: 'center' }}>
        {onRouteGpxImport && (
          <label
            className="action-btn"
            title={t('panel.route_gpx_import')}
            style={{
              display: 'inline-flex', alignItems: 'center', gap: 4,
              padding: '4px 10px', fontSize: 11, cursor: 'pointer',
            }}
          >
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4" />
              <polyline points="17 8 12 3 7 8" />
              <line x1="12" y1="3" x2="12" y2="15" />
            </svg>
            {t('panel.route_gpx_import')}
            <input
              type="file"
              accept=".gpx,application/gpx+xml"
              style={{ display: 'none' }}
              onChange={async (e) => {
                const f = e.target.files?.[0];
                if (f) await onRouteGpxImport(f);
                e.target.value = '';
              }}
            />
          </label>
        )}
        {routesExportAllUrl && (
          routes.length > 0 ? (
            <a
              className="action-btn"
              href={routesExportAllUrl}
              download="locwarp-routes.json"
              title={t('panel.routes_export_all_tooltip')}
              style={{
                display: 'inline-flex', alignItems: 'center', gap: 4,
                padding: '4px 10px', fontSize: 11, cursor: 'pointer',
                textDecoration: 'none', color: '#4ecdc4',
                background: 'rgba(78, 205, 196, 0.12)',
                border: '1px solid rgba(78, 205, 196, 0.35)',
              }}
            >
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4" />
                <polyline points="7 10 12 15 17 10" />
                <line x1="12" y1="15" x2="12" y2="3" />
              </svg>
              {t('panel.routes_export_all')}
            </a>
          ) : (
            <button
              className="action-btn"
              disabled
              title={t('panel.routes_export_all_disabled')}
              style={{
                display: 'inline-flex', alignItems: 'center', gap: 4,
                padding: '4px 10px', fontSize: 11, cursor: 'not-allowed',
                color: 'rgba(78, 205, 196, 0.45)',
                background: 'rgba(78, 205, 196, 0.05)',
                border: '1px solid rgba(78, 205, 196, 0.15)',
                opacity: 0.55,
              }}
            >
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4" />
                <polyline points="7 10 12 15 17 10" />
                <line x1="12" y1="15" x2="12" y2="3" />
              </svg>
              {t('panel.routes_export_all')}
            </button>
          )
        )}
        {onRoutesImportAll && (
          <label
            className="action-btn"
            title={t('panel.routes_import_all_tooltip')}
            style={{
              display: 'inline-flex', alignItems: 'center', gap: 4,
              padding: '4px 10px', fontSize: 11, cursor: 'pointer',
              color: '#4ecdc4',
              background: 'rgba(78, 205, 196, 0.12)',
              border: '1px solid rgba(78, 205, 196, 0.35)',
            }}
          >
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4" />
              <polyline points="17 8 12 3 7 8" />
              <line x1="12" y1="3" x2="12" y2="15" />
            </svg>
            {t('panel.routes_import_all')}
            <input
              type="file"
              accept=".json,application/json"
              style={{ display: 'none' }}
              onChange={async (e) => {
                const f = e.target.files?.[0];
                if (f) await onRoutesImportAll(f);
                e.target.value = '';
              }}
            />
          </label>
        )}
        <button
          className="action-btn"
          onClick={() => {
            if (multiSelect) exitMultiSelect();
            else { setShowCategoryMgr(false); setMultiSelect(true); if (reorderMode) exitReorderMode(); }
          }}
          style={{
            padding: '3px 8px', fontSize: 11, display: 'inline-flex', alignItems: 'center',
            background: multiSelect ? 'rgba(108,140,255,0.2)' : undefined,
            borderColor: multiSelect ? 'rgba(108,140,255,0.6)' : undefined,
          }}
          title={multiSelect ? t('bm.exit_multi_select') : t('bm.multi_select_tooltip')}
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <polyline points="9 11 12 14 22 4" />
            <path d="M21 12v7a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2h11" />
          </svg>
        </button>
        {(onCategoryReorder || onRouteReorder) && (
          <button
            className="action-btn"
            onClick={() => {
              if (reorderMode) exitReorderMode();
              else enterReorderMode();
            }}
            style={{
              padding: '3px 8px', fontSize: 11, display: 'inline-flex', alignItems: 'center',
              background: reorderMode ? 'rgba(108,140,255,0.2)' : undefined,
              borderColor: reorderMode ? 'rgba(108,140,255,0.6)' : undefined,
            }}
            title={t('bm.reorder_tooltip')}
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <polyline points="17 11 12 6 7 11" />
              <polyline points="17 17 12 22 7 17" />
              <line x1="12" y1="6" x2="12" y2="22" />
            </svg>
          </button>
        )}
        {onCategoryAdd && (
          <button
            className="action-btn"
            onClick={() => {
              setShowCategoryMgr((prev) => {
                const next = !prev;
                if (next && multiSelect) exitMultiSelect();
                return next;
              });
            }}
            style={{ padding: '3px 8px', fontSize: 11 }}
            title={t('route.manage_categories')}
          >
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <circle cx="12" cy="12" r="3" />
              <path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 01-2.83 2.83l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 01-4 0v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 01-2.83-2.83l.06-.06A1.65 1.65 0 004.68 15a1.65 1.65 0 00-1.51-1H3a2 2 0 010-4h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 012.83-2.83l.06.06A1.65 1.65 0 009 4.68a1.65 1.65 0 001-1.51V3a2 2 0 014 0v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 012.83 2.83l-.06.06A1.65 1.65 0 0019.4 9a1.65 1.65 0 001.51 1H21a2 2 0 010 4h-.09a1.65 1.65 0 00-1.51 1z" />
            </svg>
          </button>
        )}
      </div>

      {/* Search */}
      <div style={{ position: 'relative', marginBottom: 8 }}>
        <svg
          width="12" height="12" viewBox="0 0 24 24" fill="none"
          stroke="currentColor" strokeWidth="2"
          style={{ position: 'absolute', left: 8, top: '50%', transform: 'translateY(-50%)', opacity: 0.4, pointerEvents: 'none' }}
        >
          <circle cx="11" cy="11" r="8" />
          <line x1="21" y1="21" x2="16.65" y2="16.65" />
        </svg>
        <input
          type="text"
          className="search-input"
          placeholder={t('route.search_placeholder')}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          style={{ width: '100%', paddingLeft: 26, paddingRight: search ? 24 : 8, fontSize: 12 }}
        />
        {search && (
          <button
            onClick={() => setSearch('')}
            title={t('bm.search_clear')}
            style={{
              position: 'absolute', right: 4, top: '50%', transform: 'translateY(-50%)',
              background: 'none', border: 'none', color: '#bbb',
              cursor: 'pointer', padding: '2px 6px', fontSize: 14, lineHeight: 1,
            }}
          >×</button>
        )}
      </div>

      {/* Sort */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 8, fontSize: 11, color: '#bbb' }}>
        <span style={{ opacity: 0.7 }}>{t('bm.sort_label')}</span>
        <select
          value={sortMode}
          onChange={(e) => setSortMode(e.target.value as SortMode)}
          style={{
            flex: 1, background: '#1e1e22', color: '#e0e0e0',
            border: '1px solid rgba(255,255,255,0.12)', borderRadius: 4,
            padding: '3px 6px', fontSize: 11,
          }}
        >
          <option value="default" style={{ background: '#1e1e22', color: '#e0e0e0' }}>{t('bm.sort_default')}</option>
          <option value="name" style={{ background: '#1e1e22', color: '#e0e0e0' }}>{t('bm.sort_name')}</option>
          <option value="date_added" style={{ background: '#1e1e22', color: '#e0e0e0' }}>{t('bm.sort_date_added')}</option>
          <option value="date_updated" style={{ background: '#1e1e22', color: '#e0e0e0' }}>{t('route.sort_date_updated')}</option>
        </select>
      </div>

      {/* Category manager */}
      {showCategoryMgr && (
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
            {t('route.manage_categories')}
          </div>
          {categories.map((cat) => (
            <div
              key={cat.id}
              style={{
                display: 'flex', alignItems: 'center', gap: 6,
                padding: '4px 0', fontSize: 12, position: 'relative',
              }}
            >
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation();
                  if (!onCategoryRecolor) return;
                  setColorPickerFor((prev) => (prev === cat.id ? null : cat.id));
                }}
                title={t('bm.recolor_tooltip')}
                style={{
                  width: 14, height: 14, borderRadius: '50%',
                  background: resolveColor(cat),
                  border: '1.5px solid rgba(255,255,255,0.15)', padding: 0,
                  cursor: onCategoryRecolor ? 'pointer' : 'default',
                  flexShrink: 0,
                  boxShadow: '0 1px 2px rgba(0,0,0,0.3)',
                }}
              />
              {colorPickerFor === cat.id && onCategoryRecolor && (
                <div
                  data-route-color-picker
                  onClick={(e) => e.stopPropagation()}
                  style={{
                    position: 'absolute', top: 22, left: 0, zIndex: 50,
                    background: '#1e1e22',
                    border: '1px solid rgba(255,255,255,0.15)',
                    borderRadius: 6, padding: 6,
                    boxShadow: '0 6px 18px rgba(0,0,0,0.5)',
                    display: 'grid', gridTemplateColumns: 'repeat(5, 22px)',
                    gap: 4,
                  }}
                >
                  {COLOR_PALETTE.map((c) => {
                    const selected = resolveColor(cat).toLowerCase() === c.toLowerCase();
                    return (
                      <button
                        key={c}
                        type="button"
                        onClick={(e) => {
                          e.stopPropagation();
                          onCategoryRecolor(cat.id, c);
                          setColorPickerFor(null);
                        }}
                        style={{
                          width: 22, height: 22, borderRadius: '50%',
                          background: c,
                          border: selected ? '2px solid #fff' : '1.5px solid rgba(255,255,255,0.12)',
                          cursor: 'pointer', padding: 0,
                          transition: 'transform 0.1s',
                        }}
                        title={c}
                      />
                    );
                  })}
                  <input
                    type="color"
                    value={resolveColor(cat)}
                    onChange={(e) => onCategoryRecolor(cat.id, e.target.value)}
                    title={t('bm.recolor_custom')}
                    style={{
                      gridColumn: '1 / span 5',
                      width: '100%', height: 22,
                      border: '1px solid rgba(255,255,255,0.12)',
                      borderRadius: 4, padding: 0, marginTop: 2,
                      background: '#1e1e22', cursor: 'pointer',
                    }}
                  />
                </div>
              )}
              {editingCategory === cat.id ? (
                <input
                  type="text"
                  className="search-input"
                  autoFocus
                  value={editCategoryName}
                  onChange={(e) => setEditCategoryName(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') {
                      const next = editCategoryName.trim();
                      if (next && next !== cat.name && onCategoryRename) onCategoryRename(cat.id, next);
                      setEditingCategory(null);
                    }
                    if (e.key === 'Escape') setEditingCategory(null);
                  }}
                  onBlur={() => setEditingCategory(null)}
                  style={{ flex: 1, padding: '2px 4px', fontSize: 12 }}
                />
              ) : (
                <span style={{ flex: 1 }}>{displayCat(cat.name)}</span>
              )}
              {cat.id !== 'default' && onCategoryRename && editingCategory !== cat.id && (
                <button
                  onClick={() => { setEditingCategory(cat.id); setEditCategoryName(cat.name); }}
                  title={t('bm.rename_category')}
                  style={{
                    background: 'none', border: 'none',
                    color: 'var(--fg-muted, #888)',
                    cursor: 'pointer', padding: '2px 4px', fontSize: 11,
                  }}
                >
                  <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <path d="M11 4H4a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7" />
                    <path d="M18.5 2.5a2.12 2.12 0 013 3L12 15l-4 1 1-4 9.5-9.5z" />
                  </svg>
                </button>
              )}
              {cat.id !== 'default' && onCategoryDelete && (
                <button
                  onClick={() => onCategoryDelete(cat.id)}
                  style={{
                    background: 'none', border: 'none', color: '#f44336',
                    cursor: 'pointer', padding: '2px 4px', fontSize: 11,
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
          {onCategoryAdd && (
            <div style={{ display: 'flex', gap: 6, marginTop: 8 }}>
              <input
                type="text"
                className="search-input"
                placeholder={t('bm.add_category')}
                value={newCategoryName}
                onChange={(e) => setNewCategoryName(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && newCategoryName.trim()) {
                    onCategoryAdd(newCategoryName.trim());
                    setNewCategoryName('');
                  }
                }}
                style={{ flex: 1 }}
              />
              <button
                className="action-btn"
                onClick={() => {
                  if (newCategoryName.trim()) {
                    onCategoryAdd(newCategoryName.trim());
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
      )}

      {/* Search mode: flat list */}
      {search.trim() !== '' && (() => {
        const q = search.trim().toLowerCase();
        const matches = sortRoutes(routes.filter((r) => r.name.toLowerCase().includes(q)));
        if (matches.length === 0) {
          return (
            <div style={{ fontSize: 12, opacity: 0.5, padding: '10px 0', textAlign: 'center' }}>
              {t('route.search_no_results')}
            </div>
          );
        }
        return (
          <div>
            {matches.map((r) => renderRouteRow(r, true))}
          </div>
        );
      })()}

      {/* Grouped list */}
      {search.trim() === '' && (
        <>
          {routes.length === 0 && (
            <div style={{ fontSize: 12, opacity: 0.5, padding: '8px 0', textAlign: 'center' }}>
              {t('panel.route_empty')}
            </div>
          )}
          {categories.map((cat) => {
            const inCat = routesByCategory[cat.id] || [];
            const catIds = inCat.map((r) => r.id);
            const selectedInCat = catIds.filter((id) => selectedIds.has(id)).length;
            const allSelectedInCat = catIds.length > 0 && selectedInCat === catIds.length;
            const someSelectedInCat = selectedInCat > 0 && !allSelectedInCat;
            const isCollapsed = !!collapsed[cat.id];
            return (
              <div key={cat.id} style={{ marginBottom: 4 }}>
                <div
                  draggable={reorderAvailable && !!onCategoryReorder}
                  onDragStart={(e) => {
                    if (!reorderAvailable || !onCategoryReorder) return;
                    e.dataTransfer.effectAllowed = 'move';
                    e.dataTransfer.setData('application/x-locwarp-route-cat', cat.id);
                    setDraggedCatId(cat.id);
                  }}
                  onDragOver={(e) => {
                    if (!reorderAvailable || !draggedCatId) return;
                    e.preventDefault();
                    e.dataTransfer.dropEffect = 'move';
                    setDragOverCatId(cat.id);
                  }}
                  onDragLeave={() => { if (dragOverCatId === cat.id) setDragOverCatId(null); }}
                  onDrop={(e) => {
                    if (!reorderAvailable) return;
                    e.preventDefault();
                    const src = draggedCatId || e.dataTransfer.getData('application/x-locwarp-route-cat');
                    if (src) dropCategoryOn(src, cat.id);
                    setDraggedCatId(null);
                    setDragOverCatId(null);
                  }}
                  onDragEnd={() => { setDraggedCatId(null); setDragOverCatId(null); }}
                  style={{
                    display: 'flex', alignItems: 'center', gap: 6,
                    padding: '5px 4px',
                    cursor: reorderAvailable ? 'grab' : 'pointer',
                    fontSize: 12, fontWeight: 600,
                    opacity: draggedCatId === cat.id ? 0.4 : 0.85,
                    borderTop: dragOverCatId === cat.id && draggedCatId && draggedCatId !== cat.id
                      ? '2px solid #6c8cff' : '2px solid transparent',
                    userSelect: reorderAvailable ? 'none' : undefined,
                  }}
                  onClick={() => toggleCategory(cat.id)}
                >
                  {reorderAvailable && onCategoryReorder && (
                    <span style={{ opacity: 0.55, flexShrink: 0, display: 'inline-flex', alignItems: 'center', cursor: 'grab' }} title={t('bm.reorder_drag_hint')}>
                      <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                        <line x1="4" y1="8" x2="20" y2="8" />
                        <line x1="4" y1="12" x2="20" y2="12" />
                        <line x1="4" y1="16" x2="20" y2="16" />
                      </svg>
                    </span>
                  )}
                  {multiSelect && (
                    <input
                      type="checkbox"
                      checked={allSelectedInCat}
                      ref={(el) => { if (el) el.indeterminate = someSelectedInCat; }}
                      onChange={() => {
                        setSelectedIds((prev) => {
                          const next = new Set(prev);
                          if (allSelectedInCat) catIds.forEach((id) => next.delete(id));
                          else catIds.forEach((id) => next.add(id));
                          return next;
                        });
                      }}
                      onClick={(e) => e.stopPropagation()}
                      style={{ margin: 0, flexShrink: 0, cursor: 'pointer' }}
                    />
                  )}
                  <svg
                    width="10" height="10" viewBox="0 0 24 24"
                    fill="none" stroke="currentColor" strokeWidth="2"
                    style={{ transform: isCollapsed ? 'rotate(0deg)' : 'rotate(90deg)', transition: 'transform 0.2s' }}
                  >
                    <polyline points="9,18 15,12 9,6" />
                  </svg>
                  <div
                    style={{
                      width: 8, height: 8, borderRadius: '50%',
                      background: resolveColor(cat), flexShrink: 0,
                    }}
                  />
                  <span>{displayCat(cat.name)}</span>
                  <span style={{ marginLeft: 'auto', opacity: 0.4, fontWeight: 400, fontSize: 10 }}>
                    {inCat.length}
                  </span>
                </div>
                {!isCollapsed && (
                  <div style={{ paddingLeft: 16 }}>
                    {inCat.length === 0 && (
                      <div style={{ fontSize: 11, opacity: 0.4, padding: '4px 0' }}>{t('bm.blank')}</div>
                    )}
                    {sortRoutes(inCat).map((r) => renderRouteRow(r, false, cat.id))}
                  </div>
                )}
              </div>
            );
          })}
        </>
      )}

      {/* Multi-select bottom bar */}
      {multiSelect && (
        <div
          style={{
            position: 'sticky', bottom: -12, zIndex: 10,
            marginLeft: -12, marginRight: -12, marginTop: 16,
            padding: '8px 12px',
            background: 'rgba(26, 29, 39, 0.98)',
            backdropFilter: 'blur(6px)',
            borderTop: '1px solid rgba(108,140,255,0.35)',
            boxShadow: '0 -6px 12px rgba(0,0,0,0.35)',
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11, flexWrap: 'wrap' }}>
            <button
              className="action-btn"
              onClick={() => {
                const allIds = routes.map((r) => r.id);
                if (selectedIds.size === allIds.length) setSelectedIds(new Set());
                else setSelectedIds(new Set(allIds));
              }}
              style={{ padding: '3px 8px', fontSize: 11 }}
            >
              {selectedIds.size === routes.length && routes.length > 0
                ? t('bm.deselect_all') : t('bm.select_all')}
            </button>
            {onRouteMove && categories.length > 1 && (
              <select
                value=""
                onChange={async (e) => {
                  const targetId = e.target.value;
                  if (!targetId) return;
                  const ids = Array.from(selectedIds);
                  if (ids.length === 0) return;
                  await onRouteMove(ids, targetId);
                  exitMultiSelect();
                }}
                disabled={selectedIds.size === 0}
                style={{
                  background: '#1e1e22', color: '#e0e0e0',
                  border: '1px solid #444', borderRadius: 4,
                  padding: '3px 6px', fontSize: 11,
                }}
              >
                <option value="" style={{ background: '#1e1e22', color: '#e0e0e0' }}>
                  {t('route.move_to_placeholder')}
                </option>
                {categories.map((c) => (
                  <option key={c.id} value={c.id} style={{ background: '#1e1e22', color: '#e0e0e0' }}>
                    {displayCat(c.name)}
                  </option>
                ))}
              </select>
            )}
            <span style={{ opacity: 0.7, marginLeft: 'auto' }}>
              {selectedIds.size} / {routes.length}
            </span>
            <button
              className="action-btn"
              onClick={handleBulkDelete}
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
      )}

      {/* Context menu */}
      {contextMenu && createPortal(
        <div
          data-route-context-menu
          style={{
            position: 'fixed',
            left: Math.min(contextMenu.x, window.innerWidth - 180),
            top: Math.min(contextMenu.y, window.innerHeight - 240),
            zIndex: 9999,
            background: '#2a2a2e', border: '1px solid #444', borderRadius: 6,
            padding: '4px 0', boxShadow: '0 4px 12px rgba(0,0,0,0.4)',
            minWidth: 160,
          }}
        >
          <div
            style={ctxItemStyle}
            onMouseEnter={ctxHighlight} onMouseLeave={ctxUnhighlight}
            onClick={() => { onRouteLoad(contextMenu.route.id); setContextMenu(null); }}
          >
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ marginRight: 6 }}>
              <polyline points="22,12 18,12 15,21 9,3 6,12 2,12" />
            </svg>
            {t('route.load')}
          </div>
          <div
            style={ctxItemStyle}
            onMouseEnter={ctxHighlight} onMouseLeave={ctxUnhighlight}
            onClick={() => {
              setEditingRouteId(contextMenu.route.id);
              setEditingRouteName(contextMenu.route.name);
              setContextMenu(null);
            }}
          >
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ marginRight: 6 }}>
              <path d="M11 4H4a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7" />
              <path d="M18.5 2.5a2.12 2.12 0 013 3L12 15l-4 1 1-4 9.5-9.5z" />
            </svg>
            {t('bm.edit')}
          </div>
          {onRouteGpxExport && (
            <div
              style={ctxItemStyle}
              onMouseEnter={ctxHighlight} onMouseLeave={ctxUnhighlight}
              onClick={() => { onRouteGpxExport(contextMenu.route.id); setContextMenu(null); }}
            >
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ marginRight: 6 }}>
                <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4" />
                <polyline points="7 10 12 15 17 10" />
                <line x1="12" y1="15" x2="12" y2="3" />
              </svg>
              {t('route.export_gpx')}
            </div>
          )}
          <div
            style={ctxItemStyle}
            onMouseEnter={ctxHighlight} onMouseLeave={ctxUnhighlight}
            onClick={() => { onRouteDelete(contextMenu.route.id); setContextMenu(null); }}
          >
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#f44336" strokeWidth="2" style={{ marginRight: 6 }}>
              <polyline points="3,6 5,6 21,6" />
              <path d="M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2" />
            </svg>
            <span style={{ color: '#f44336' }}>{t('generic.delete')}</span>
          </div>
          {onRouteMove && categories.length > 1 && (
            <>
              <div style={{ height: 1, background: '#444', margin: '4px 0' }} />
              <div style={{ padding: '4px 12px', fontSize: 10, opacity: 0.5 }}>{t('bm.move_to')}</div>
              {categories
                .filter((c) => c.id !== (contextMenu.route.category_id || 'default'))
                .map((cat) => (
                  <div
                    key={cat.id}
                    style={ctxItemStyle}
                    onMouseEnter={ctxHighlight} onMouseLeave={ctxUnhighlight}
                    onClick={() => {
                      onRouteMove([contextMenu.route.id], cat.id);
                      setContextMenu(null);
                    }}
                  >
                    <div
                      style={{
                        width: 6, height: 6, borderRadius: '50%',
                        background: resolveColor(cat), marginRight: 6,
                      }}
                    />
                    {displayCat(cat.name)}
                  </div>
                ))}
            </>
          )}
        </div>,
        document.body,
      )}

      {/* Overwrite-on-save dialog */}
      {overwritePrompt && createPortal(
        <div
          onClick={() => setOverwritePrompt(null)}
          className="anim-fade-in"
          style={{
            position: 'fixed', top: 0, left: 0, right: 0, bottom: 0,
            background: 'rgba(8, 10, 20, 0.55)',
            backdropFilter: 'blur(4px)', WebkitBackdropFilter: 'blur(4px)',
            zIndex: 1000,
            display: 'flex', alignItems: 'center', justifyContent: 'center',
          }}
        >
          <div
            onClick={(e) => e.stopPropagation()}
            className="anim-scale-in"
            style={{
              background: 'rgba(26, 29, 39, 0.96)',
              backdropFilter: 'blur(14px)', WebkitBackdropFilter: 'blur(14px)',
              border: '1px solid rgba(108, 140, 255, 0.2)',
              borderRadius: 12, padding: 18, width: 360, color: '#e0e0e0',
              boxShadow: '0 20px 60px rgba(12, 18, 40, 0.65), 0 0 0 1px rgba(255, 255, 255, 0.05) inset',
            }}
          >
            <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 10 }}>
              {t('route.overwrite_title')}
            </div>
            <div style={{ fontSize: 12, marginBottom: 12, lineHeight: 1.55 }}>
              {t('route.overwrite_body', { name: overwritePrompt.name, n: overwritePrompt.matches.length })}
            </div>
            {overwritePrompt.matches.length > 1 && (
              <select
                value={overwritePrompt.chosenId}
                onChange={(e) => setOverwritePrompt({ ...overwritePrompt, chosenId: e.target.value })}
                style={{
                  width: '100%', marginBottom: 12, padding: '6px 8px',
                  background: '#1e1e22', color: '#e0e0e0',
                  border: '1px solid #444', borderRadius: 4, fontSize: 12,
                }}
              >
                {overwritePrompt.matches.map((r) => {
                  const cat = findCategoryById(r.category_id || 'default');
                  return (
                    <option key={r.id} value={r.id} style={{ background: '#1e1e22', color: '#e0e0e0' }}>
                      {r.name} · {cat ? displayCat(cat.name) : '?'} · {r.waypoints.length} {t('route.points_unit')}
                    </option>
                  );
                })}
              </select>
            )}
            {overwritePrompt.matches.length === 1 && (() => {
              const r = overwritePrompt.matches[0];
              const cat = findCategoryById(r.category_id || 'default');
              return (
                <div style={{ fontSize: 11, opacity: 0.7, marginBottom: 12 }}>
                  {cat ? displayCat(cat.name) : '?'} · {r.waypoints.length} {t('route.points_unit')}
                </div>
              );
            })()}
            <div style={{ display: 'flex', gap: 6 }}>
              <button
                className="action-btn primary"
                style={{ flex: 1 }}
                onClick={confirmOverwrite}
              >{t('route.overwrite_yes')}</button>
              <button
                className="action-btn"
                style={{ flex: 1 }}
                onClick={confirmSaveAsNew}
              >{t('route.overwrite_save_as_new')}</button>
              <button
                className="action-btn"
                onClick={() => setOverwritePrompt(null)}
              >{t('generic.cancel')}</button>
            </div>
          </div>
        </div>,
        document.body,
      )}
    </div>
  );

  function renderRouteRow(
    route: SavedRoute,
    flat: boolean,
    inCategoryId?: string,
  ) {
    const isEditing = editingRouteId === route.id;
    const isSelected = selectedIds.has(route.id);
    const cat = findCategoryById(route.category_id || 'default');
    const canDragRow = reorderAvailable && !!onRouteReorder && !!inCategoryId && !flat;

    const commitRename = () => {
      const n = editingRouteName.trim();
      if (n && n !== route.name) onRouteRename(route.id, n);
      setEditingRouteId(null);
    };

    return (
      <div
        key={route.id}
        className="bookmark-item"
        draggable={canDragRow}
        onDragStart={(e) => {
          if (!canDragRow || !inCategoryId) return;
          e.dataTransfer.effectAllowed = 'move';
          e.dataTransfer.setData('application/x-locwarp-route', `${inCategoryId}::${route.id}`);
          setDraggedRouteId(route.id);
          setDragRouteCat(inCategoryId);
        }}
        onDragOver={(e) => {
          if (!canDragRow || !draggedRouteId || dragRouteCat !== inCategoryId) return;
          e.preventDefault();
          e.dataTransfer.dropEffect = 'move';
          setDragOverRouteId(route.id);
        }}
        onDragLeave={() => { if (dragOverRouteId === route.id) setDragOverRouteId(null); }}
        onDrop={(e) => {
          if (!canDragRow || !inCategoryId) return;
          e.preventDefault();
          const src = draggedRouteId;
          if (src && dragRouteCat === inCategoryId) dropRouteOn(inCategoryId, src, route.id);
          setDraggedRouteId(null);
          setDragOverRouteId(null);
          setDragRouteCat(null);
        }}
        onDragEnd={() => { setDraggedRouteId(null); setDragOverRouteId(null); setDragRouteCat(null); }}
        style={{
          display: 'flex', alignItems: 'center', gap: 6,
          padding: '6px 6px', borderRadius: 4, fontSize: 12,
          cursor: canDragRow ? 'grab' : 'pointer', transition: 'background 0.15s',
          background: multiSelect && isSelected ? 'rgba(108,140,255,0.18)' : 'transparent',
          opacity: draggedRouteId === route.id ? 0.4 : 1,
          borderTop: dragOverRouteId === route.id && draggedRouteId && draggedRouteId !== route.id
            ? '2px solid #6c8cff' : '2px solid transparent',
          userSelect: canDragRow ? 'none' : undefined,
        }}
        onClick={() => {
          if (multiSelect) toggleSelected(route.id);
          else if (!isEditing && !reorderAvailable) onRouteLoad(route.id);
        }}
        onContextMenu={(e) => {
          if (multiSelect || reorderAvailable) { e.preventDefault(); return; }
          e.preventDefault();
          e.stopPropagation();
          setContextMenu({ route, x: e.clientX, y: e.clientY });
        }}
        onMouseEnter={(e) => {
          if (!(multiSelect && isSelected)) (e.currentTarget as HTMLDivElement).style.background = '#3a3a3e';
        }}
        onMouseLeave={(e) => {
          (e.currentTarget as HTMLDivElement).style.background = multiSelect && isSelected
            ? 'rgba(108,140,255,0.18)' : 'transparent';
        }}
      >
        {canDragRow && (
          <span style={{ opacity: 0.55, flexShrink: 0, display: 'inline-flex', alignItems: 'center', cursor: 'grab' }} title={t('bm.reorder_drag_hint')}>
            <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <line x1="4" y1="8" x2="20" y2="8" />
              <line x1="4" y1="12" x2="20" y2="12" />
              <line x1="4" y1="16" x2="20" y2="16" />
            </svg>
          </span>
        )}
        {multiSelect && (
          <input
            type="checkbox"
            checked={isSelected}
            onChange={() => toggleSelected(route.id)}
            onClick={(e) => e.stopPropagation()}
            style={{ margin: 0, flexShrink: 0 }}
          />
        )}
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ flexShrink: 0, opacity: 0.7 }}>
          <polyline points="22,12 18,12 15,21 9,3 6,12 2,12" />
        </svg>
        {flat && cat && (
          <div
            style={{
              width: 6, height: 6, borderRadius: '50%',
              background: resolveColor(cat), flexShrink: 0,
            }}
            title={displayCat(cat.name)}
          />
        )}
        {isEditing ? (
          <input
            type="text"
            className="search-input"
            autoFocus
            value={editingRouteName}
            onChange={(e) => setEditingRouteName(e.target.value)}
            onClick={(e) => e.stopPropagation()}
            onBlur={commitRename}
            onKeyDown={(e) => {
              if (e.key === 'Enter') commitRename();
              if (e.key === 'Escape') setEditingRouteId(null);
            }}
            style={{ flex: 1, padding: '2px 4px', fontSize: 12 }}
          />
        ) : (
          <div style={{ flex: 1, overflow: 'hidden', display: 'flex', flexDirection: 'column', minWidth: 0 }}>
            <span style={{ whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
              {route.name}
            </span>
            <span style={{ fontSize: 10, opacity: 0.5, fontFamily: 'monospace' }}>
              {route.waypoints.length} {t('route.points_unit')}
              {route.profile ? ` · ${route.profile}` : ''}
            </span>
          </div>
        )}
      </div>
    );
  }
};

const ctxItemStyle: React.CSSProperties = {
  padding: '6px 12px',
  cursor: 'pointer',
  fontSize: 12,
  display: 'flex',
  alignItems: 'center',
  color: '#e0e0e0',
  transition: 'background 0.15s',
};

function ctxHighlight(e: React.MouseEvent<HTMLDivElement>) {
  (e.currentTarget as HTMLDivElement).style.background = '#3a3a3e';
}
function ctxUnhighlight(e: React.MouseEvent<HTMLDivElement>) {
  (e.currentTarget as HTMLDivElement).style.background = 'transparent';
}

export default RouteList;
