import { useCallback, useEffect, useRef, type MouseEvent, type PointerEvent } from "react";

const MOVE_TOLERANCE = 10;

/**
 * Touch and pen long-press (mouse users right-click instead). iOS Safari does not reliably fire
 * `contextmenu` on long-press, so this times the press itself, cancels on movement or scrolling,
 * and swallows the click that follows a completed long-press.
 */
export function useLongPress(onLongPress: () => void, delay = 500) {
  const timer = useRef<number | null>(null);
  const origin = useRef<{ x: number; y: number } | null>(null);
  const fired = useRef(false);

  const clear = useCallback(() => {
    if (timer.current !== null) window.clearTimeout(timer.current);
    timer.current = null;
    origin.current = null;
  }, []);

  useEffect(() => clear, [clear]);

  return {
    onPointerDown: (event: PointerEvent) => {
      if (event.pointerType === "mouse") return;
      fired.current = false;
      origin.current = { x: event.clientX, y: event.clientY };
      timer.current = window.setTimeout(() => {
        timer.current = null;
        fired.current = true;
        navigator.vibrate?.(10);
        onLongPress();
      }, delay);
    },
    onPointerMove: (event: PointerEvent) => {
      if (!origin.current) return;
      if (Math.hypot(event.clientX - origin.current.x, event.clientY - origin.current.y) > MOVE_TOLERANCE) clear();
    },
    onPointerUp: clear,
    onPointerCancel: clear,
    onPointerLeave: clear,
    onClickCapture: (event: MouseEvent) => {
      if (!fired.current) return;
      fired.current = false;
      event.preventDefault();
      event.stopPropagation();
    },
  };
}
