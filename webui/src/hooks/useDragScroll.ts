import { useCallback, useRef } from "react";

type DragAxis = "x" | "y" | "both";

interface UseDragScrollOptions {
  axis?: DragAxis;
  ignoreSelector?: string;
  onDrag?: () => void;
}

interface DragState {
  pointerId: number;
  startX: number;
  startY: number;
  scrollLeft: number;
  scrollTop: number;
  moved: boolean;
}

const DEFAULT_IGNORE_SELECTOR =
  "button, a, input, textarea, select, [contenteditable='true'], pre, code, [data-drag-scroll-ignore]";

export function useDragScroll<T extends HTMLElement>({
  axis = "y",
  ignoreSelector = DEFAULT_IGNORE_SELECTOR,
  onDrag,
}: UseDragScrollOptions = {}) {
  const ref = useRef<T>(null);
  const dragRef = useRef<DragState | null>(null);
  const suppressClickRef = useRef(false);

  const onPointerDown = useCallback(
    (event: React.PointerEvent<T>) => {
      if (event.pointerType !== "mouse" || event.button !== 0) return;
      if (event.target instanceof Element && event.target.closest(ignoreSelector)) return;

      const element = ref.current;
      if (!element) return;

      suppressClickRef.current = false;
      dragRef.current = {
        pointerId: event.pointerId,
        startX: event.clientX,
        startY: event.clientY,
        scrollLeft: element.scrollLeft,
        scrollTop: element.scrollTop,
        moved: false,
      };

      // Prevent native text/image dragging from winning the pointer gesture.
      event.preventDefault();
      element.setPointerCapture(event.pointerId);
    },
    [ignoreSelector]
  );

  const onPointerMove = useCallback(
    (event: React.PointerEvent<T>) => {
      const drag = dragRef.current;
      const element = ref.current;
      if (!drag || !element || drag.pointerId !== event.pointerId) return;

      const deltaX = event.clientX - drag.startX;
      const deltaY = event.clientY - drag.startY;
      if (!drag.moved && Math.hypot(deltaX, deltaY) < 4) return;

      if (!drag.moved) {
        drag.moved = true;
        element.classList.add("is-drag-scrolling");
        onDrag?.();
      }
      if (axis !== "y") element.scrollLeft = drag.scrollLeft - deltaX;
      if (axis !== "x") element.scrollTop = drag.scrollTop - deltaY;
      event.preventDefault();
    },
    [axis, onDrag]
  );

  const finishDrag = useCallback((event: React.PointerEvent<T>) => {
    const drag = dragRef.current;
    const element = ref.current;
    if (!drag || drag.pointerId !== event.pointerId) return;

    suppressClickRef.current = drag.moved;
    dragRef.current = null;
    element?.classList.remove("is-drag-scrolling");
    if (element?.hasPointerCapture(event.pointerId)) {
      element.releasePointerCapture(event.pointerId);
    }
  }, []);

  const onClickCapture = useCallback((event: React.MouseEvent<T>) => {
    if (!suppressClickRef.current) return;
    suppressClickRef.current = false;
    event.preventDefault();
    event.stopPropagation();
  }, []);

  const onDragStart = useCallback((event: React.DragEvent<T>) => {
    event.preventDefault();
  }, []);

  return {
    ref,
    dragScrollProps: {
      onPointerDown,
      onPointerMove,
      onPointerUp: finishDrag,
      onPointerCancel: finishDrag,
      onLostPointerCapture: finishDrag,
      onClickCapture,
      onDragStart,
    },
  };
}
