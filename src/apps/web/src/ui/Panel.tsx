import * as Dialog from "@radix-ui/react-dialog";
import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { Icon } from "./icons";
import { MEDIA, useMedia } from "./media";

/**
 * Level-2 panel, spec §3.2.
 * - ≥1280: inspector (role complementary) that pushes content; no scrim, no focus trap.
 * - 900–1279: floating glass dialog over content with a faint scrim.
 * - <900: bottom sheet; `detents` adds the 58% / 92% heights toggled by the grabber.
 *
 * Radix Dialog.Overlay is deliberately not used: it mounts react-remove-scroll, which injects a
 * <style> element that the deployed CSP (style-src 'self') blocks. The page never scrolls the
 * body, so the scrim below is enough.
 */
export interface PanelProps {
  title: string;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  footerClassName?: string;
  tabs?: ReactNode;
  detents?: boolean;
  bodyClassName?: string;
}

function isEditable(target: EventTarget | null): boolean {
  return target instanceof HTMLElement && (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName));
}

export function Panel(props: PanelProps) {
  const inspector = useMedia(MEDIA.inspector);
  return inspector ? <InspectorPanel {...props} /> : <SheetPanel {...props} />;
}

function PanelHeader({ title, onClose, titleId, closeRef, grabber }: {
  title: string; onClose: () => void; titleId?: string; closeRef: React.RefObject<HTMLButtonElement>; grabber?: ReactNode;
}) {
  return (
    <header>
      {grabber}
      <h2 id={titleId} title={title}>{title}</h2>
      <button ref={closeRef} type="button" className="icon-btn close" onClick={onClose} aria-label="关闭">
        <Icon name="close" size={16} />
      </button>
    </header>
  );
}

function PanelInner({ children, footer, footerClassName, tabs, bodyClassName }: PanelProps) {
  return (
    <>
      {tabs}
      <div className={`body${bodyClassName ? ` ${bodyClassName}` : ""}`}>{children}</div>
      {footer ? <div className={`foot${footerClassName ? ` ${footerClassName}` : ""}`}>{footer}</div> : null}
    </>
  );
}

function InspectorPanel(props: PanelProps) {
  const { title, onClose } = props;
  const closeRef = useRef<HTMLButtonElement>(null);
  const titleId = useId();
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    closeRef.current?.focus({ preventScroll: true });
    return () => {
      if (previous && previous.isConnected) previous.focus({ preventScroll: true });
    };
  }, []);

  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented) return;
      // Let an open popover or menu consume Escape first.
      if (document.querySelector("[data-radix-popper-content-wrapper], .action-sheet")) return;
      onCloseRef.current();
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, []);

  return createPortal(
    <aside className="panel" role="complementary" aria-labelledby={titleId} data-panel="inspector">
      <PanelHeader title={title} onClose={onClose} titleId={titleId} closeRef={closeRef} />
      <PanelInner {...props} />
    </aside>,
    document.body,
  );
}

function SheetPanel(props: PanelProps) {
  const { title, onClose, detents } = props;
  const narrow = !useMedia(MEDIA.wide);
  const closeRef = useRef<HTMLButtonElement>(null);
  const [large, setLarge] = useState(false);
  const detentClass = narrow && detents ? (large ? " large" : " medium") : "";

  return (
    <Dialog.Root open onOpenChange={(open) => { if (!open) onClose(); }}>
      <Dialog.Portal>
        <div className="scrim" aria-hidden="true" />
        <Dialog.Content
          className={`panel${detentClass}`}
          aria-describedby={undefined}
          data-panel={narrow ? "sheet" : "overlay"}
          onOpenAutoFocus={(event) => {
            event.preventDefault();
            closeRef.current?.focus({ preventScroll: true });
          }}
        >
          <header>
            {narrow && detents ? (
              <button type="button" className="grabber" onClick={() => setLarge((value) => !value)} aria-label={large ? "收起面板" : "展开面板"} />
            ) : null}
            <Dialog.Title asChild><h2 title={title}>{title}</h2></Dialog.Title>
            <Dialog.Close asChild>
              <button ref={closeRef} type="button" className="icon-btn close" aria-label="关闭">
                <Icon name="close" size={16} />
              </button>
            </Dialog.Close>
          </header>
          <PanelInner {...props} />
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

/** J/K style shortcuts that only fire while no text field has focus. */
export function useKeyShortcut(keys: Record<string, () => void>, enabled = true): void {
  const ref = useRef(keys);
  ref.current = keys;
  useEffect(() => {
    if (!enabled) return undefined;
    const handler = (event: KeyboardEvent) => {
      if (event.metaKey || event.ctrlKey || event.altKey || isEditable(event.target)) return;
      const action = ref.current[event.key.toLowerCase()];
      if (action) {
        event.preventDefault();
        action();
      }
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [enabled]);
}
