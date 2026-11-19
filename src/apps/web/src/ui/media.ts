import { useCallback, useEffect, useRef, useState, useSyncExternalStore, type RefObject } from "react";

// Breakpoints, spec §3.4.
export const MEDIA = {
  wide: "(min-width: 900px)",
  inspector: "(min-width: 1280px)",
  tablet: "(min-width: 600px)",
  reducedMotion: "(prefers-reduced-motion: reduce)",
} as const;

function query(media: string): MediaQueryList | null {
  return typeof window !== "undefined" && typeof window.matchMedia === "function" ? window.matchMedia(media) : null;
}

export function useMedia(media: string): boolean {
  const subscribe = useCallback(
    (onChange: () => void) => {
      const list = query(media);
      if (!list) return () => undefined;
      list.addEventListener("change", onChange);
      return () => list.removeEventListener("change", onChange);
    },
    [media],
  );
  return useSyncExternalStore(subscribe, () => query(media)?.matches ?? false, () => false);
}

export function prefersReducedMotion(): boolean {
  return query(MEDIA.reducedMotion)?.matches ?? false;
}

/** True once the element's scrollTop passes the threshold (large title collapsing into the toolbar). */
export function useScrolledPast(ref: RefObject<HTMLElement | null>, threshold = 56): boolean {
  const [past, setPast] = useState(false);
  useEffect(() => {
    const element = ref.current;
    if (!element) return undefined;
    const update = () => setPast(element.scrollTop > threshold);
    update();
    element.addEventListener("scroll", update, { passive: true });
    return () => element.removeEventListener("scroll", update);
  }, [ref, threshold]);
  return past;
}

/** Re-renders every `intervalMs` while `active`, returning Date.now(). */
export function useNow(active: boolean, intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return undefined;
    setNow(Date.now());
    const timer = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(timer);
  }, [active, intervalMs]);
  return now;
}

/** Closes floating layers when the window is resized (spec §3.3). */
export function useCloseOnResize(open: boolean, close: () => void): void {
  const closeRef = useRef(close);
  closeRef.current = close;
  useEffect(() => {
    if (!open) return undefined;
    const handler = () => closeRef.current();
    window.addEventListener("resize", handler);
    return () => window.removeEventListener("resize", handler);
  }, [open]);
}
