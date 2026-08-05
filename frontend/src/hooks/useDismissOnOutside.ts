import { useEffect } from 'react';

// Dismiss a floating layer (context menu, color-picker popover) on ESC, or on
// any pointerdown / right-click that isn't on the layer itself. The layer is
// identified by a CSS selector (a data attribute on its root). Uses
// pointerdown so it fires before React click handlers inside the layer.
//
// `active` is the state value that opens the layer (menu object, category
// key, …) so the effect re-runs on every change, matching the inline
// implementations this was extracted from.
export function useDismissOnOutside(
  active: unknown,
  selector: string,
  onDismiss: () => void,
  opts?: { closeOnContextMenu?: boolean },
) {
  const closeOnContextMenu = opts?.closeOnContextMenu ?? false;
  useEffect(() => {
    if (!active) return;
    const onOutside = (e: Event) => {
      const target = e.target as Element | null;
      if (target && target.closest?.(selector)) return;
      onDismiss();
    };
    const onEsc = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onDismiss();
    };
    // Register on the next tick so the opening click's bubbling doesn't
    // dismiss the layer the moment we render it.
    const id = setTimeout(() => {
      document.addEventListener('pointerdown', onOutside);
      if (closeOnContextMenu) document.addEventListener('contextmenu', onOutside);
      document.addEventListener('keydown', onEsc);
    }, 0);
    return () => {
      clearTimeout(id);
      document.removeEventListener('pointerdown', onOutside);
      if (closeOnContextMenu) document.removeEventListener('contextmenu', onOutside);
      document.removeEventListener('keydown', onEsc);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, selector, closeOnContextMenu]);
}
