import { useEffect, useRef } from 'react';

// Auto-scroll the surrounding scrollable container while a drag is in
// progress: HTML5 native DnD's built-in edge-scroll is sluggish and barely
// triggers on long lists, so we drive it from a rAF loop ourselves. Wheel
// events keep working as normal (Chromium delivers them through dragover).
//
// Returns the ref to attach to the list's wrapper element; the nearest
// scrollable ancestor of that element is the one scrolled.
export function useDragAutoScroll(dragging: boolean) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const dragCursorYRef = useRef(0);
  useEffect(() => {
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
  }, [dragging]);
  return wrapRef;
}
