import { useRef, useState } from 'react';

// Reorder-mode state machine shared by the bookmark and route lists.
//
// Off by default — the user opts in via a toolbar button. While on, category
// headers and item rows expose a drag handle that persists a new order.
// Reorder only makes sense when sorted by the underlying 'default' order
// (other sort modes would re-rank items and the drag results would feel
// broken), so entering forces sort to the default mode and the user's
// previous sort is restored on exit.
export function useReorderMode<M extends string>(opts: {
  sortMode: M;
  setSortMode: (m: M) => void;
  defaultMode: M;
  // Invoked while entering (e.g. to exit a mutually-exclusive multi-select).
  onEnter?: () => void;
}) {
  const [reorderMode, setReorderMode] = useState(false);
  // Saved sort mode from before entering reorder mode; restored on exit.
  const sortBeforeReorderRef = useRef<M | null>(null);
  // Drag-and-drop state for in-list reordering. We track the dragged id so we
  // can highlight it (lower opacity), and the hover target so we can draw a
  // drop indicator line. Categories and items have separate slots so a
  // category drag doesn't reach into the item list.
  const [draggedItemId, setDraggedItemId] = useState<string | null>(null);
  const [dragOverItemId, setDragOverItemId] = useState<string | null>(null);
  const [dragItemCat, setDragItemCat] = useState<string | null>(null);
  const [draggedCatKey, setDraggedCatKey] = useState<string | null>(null);
  const [dragOverCatKey, setDragOverCatKey] = useState<string | null>(null);

  const enterReorderMode = () => {
    if (opts.sortMode !== opts.defaultMode) {
      sortBeforeReorderRef.current = opts.sortMode;
      opts.setSortMode(opts.defaultMode);
    } else {
      sortBeforeReorderRef.current = null;
    }
    opts.onEnter?.();
    setReorderMode(true);
  };
  const exitReorderMode = () => {
    setReorderMode(false);
    setDraggedItemId(null);
    setDragOverItemId(null);
    setDragItemCat(null);
    setDraggedCatKey(null);
    setDragOverCatKey(null);
    const prev = sortBeforeReorderRef.current;
    if (prev && prev !== opts.defaultMode) opts.setSortMode(prev);
    sortBeforeReorderRef.current = null;
  };

  return {
    reorderMode,
    enterReorderMode,
    exitReorderMode,
    draggedItemId, setDraggedItemId,
    dragOverItemId, setDragOverItemId,
    dragItemCat, setDragItemCat,
    draggedCatKey, setDraggedCatKey,
    dragOverCatKey, setDragOverCatKey,
  };
}

// Drop ONTO target = the moved item takes the target's display position;
// everything between them shifts to fill the gap.
export function moveInArray<T>(arr: readonly T[], srcIdx: number, dstIdx: number): T[] {
  const copy = [...arr];
  const [moved] = copy.splice(srcIdx, 1);
  copy.splice(dstIdx, 0, moved);
  return copy;
}
