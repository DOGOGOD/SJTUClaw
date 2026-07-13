// @vitest-environment jsdom

import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, vi, describe, expect, it } from "vitest";
import { useDragScroll } from "./useDragScroll";

afterEach(cleanup);

function DragScroller({ onItemClick = vi.fn() }: { onItemClick?: () => void }) {
  const { ref, dragScrollProps } = useDragScroll<HTMLDivElement>({
    axis: "y",
    ignoreSelector: "[data-drag-scroll-ignore]",
  });

  return (
    <div ref={ref} data-testid="scroller" {...dragScrollProps}>
      <button data-testid="item" onClick={onItemClick}>Session</button>
      <button data-testid="ignored" data-drag-scroll-ignore>Rename</button>
    </div>
  );
}

function prepareScroller(element: HTMLElement) {
  Object.defineProperty(element, "scrollTop", { configurable: true, writable: true, value: 120 });
  Object.defineProperty(element, "scrollLeft", { configurable: true, writable: true, value: 0 });
  Object.assign(element, {
    setPointerCapture: vi.fn(),
    hasPointerCapture: vi.fn(() => true),
    releasePointerCapture: vi.fn(),
  });
}

describe("useDragScroll", () => {
  it("scrolls from a draggable item and suppresses its click after moving", () => {
    const onItemClick = vi.fn();
    const view = render(<DragScroller onItemClick={onItemClick} />);
    const scroller = view.getByTestId("scroller");
    const item = view.getByTestId("item");
    prepareScroller(scroller);

    fireEvent.pointerDown(item, { pointerId: 7, pointerType: "mouse", button: 0, clientX: 20, clientY: 100 });
    fireEvent.pointerMove(scroller, { pointerId: 7, pointerType: "mouse", clientX: 20, clientY: 45 });

    expect(scroller.scrollTop).toBe(175);
    expect(scroller.classList.contains("is-drag-scrolling")).toBe(true);

    fireEvent.pointerUp(scroller, { pointerId: 7, pointerType: "mouse", clientX: 20, clientY: 45 });
    fireEvent.click(item);

    expect(scroller.classList.contains("is-drag-scrolling")).toBe(false);
    expect(onItemClick).not.toHaveBeenCalled();
  });

  it("leaves ignored controls untouched", () => {
    const view = render(<DragScroller />);
    const scroller = view.getByTestId("scroller");
    prepareScroller(scroller);

    fireEvent.pointerDown(view.getByTestId("ignored"), {
      pointerId: 9,
      pointerType: "mouse",
      button: 0,
      clientX: 20,
      clientY: 100,
    });
    fireEvent.pointerMove(scroller, { pointerId: 9, pointerType: "mouse", clientX: 20, clientY: 20 });

    expect(scroller.scrollTop).toBe(120);
    expect(scroller.setPointerCapture).not.toHaveBeenCalled();
  });

  it("preserves a normal item click when the pointer did not move", () => {
    const onItemClick = vi.fn();
    const view = render(<DragScroller onItemClick={onItemClick} />);
    const scroller = view.getByTestId("scroller");
    const item = view.getByTestId("item");
    prepareScroller(scroller);

    fireEvent.pointerDown(item, { pointerId: 11, pointerType: "mouse", button: 0, clientX: 20, clientY: 100 });
    fireEvent.pointerUp(scroller, { pointerId: 11, pointerType: "mouse", clientX: 20, clientY: 100 });
    fireEvent.click(item);

    expect(onItemClick).toHaveBeenCalledTimes(1);
  });
});
