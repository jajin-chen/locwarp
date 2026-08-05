import { useState } from 'react';

// Multi-select mode: tick rows and batch-delete. When active, row clicks
// toggle selection instead of performing their normal action. The caller
// supplies the confirm message and the actual delete implementation.
export function useMultiSelect(opts: {
  confirmMessage: (n: number) => string;
  deleteIds: (ids: string[]) => Promise<unknown> | void;
}) {
  const [multiSelect, setMultiSelect] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const toggleSelected = (id: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };
  const exitMultiSelect = () => {
    setMultiSelect(false);
    setSelectedIds(new Set());
  };
  const handleBulkDelete = async () => {
    if (selectedIds.size === 0) return;
    if (!window.confirm(opts.confirmMessage(selectedIds.size))) return;
    await opts.deleteIds(Array.from(selectedIds));
    exitMultiSelect();
  };
  return {
    multiSelect,
    setMultiSelect,
    selectedIds,
    setSelectedIds,
    toggleSelected,
    exitMultiSelect,
    handleBulkDelete,
  };
}
