import { forwardRef, useImperativeHandle, useLayoutEffect, useRef, useState, type DragEvent, type ReactNode } from "react";
import { Token } from "./controls";
import { Icon } from "./icons";

export interface ComposerToken {
  key: string;
  label: string;
  removeLabel: string;
  onRemove: () => void;
}

export interface ComposerHandle {
  focus: () => void;
}

interface ComposerProps {
  variant: "home" | "dock";
  value: string;
  onChange: (value: string) => void;
  onSubmit: () => void;
  onOpenSettings: () => void;
  label: string;
  placeholder: string;
  submitLabel: string;
  disabled?: boolean;
  busy?: boolean;
  canSubmit: boolean;
  tokens?: ComposerToken[];
  error?: ReactNode;
  /** Opens the add-sources panel; omitted hides the button. */
  onAddSources?: () => void;
  addDisabled?: boolean;
  /** Files dropped onto the composer go to the add-sources panel. */
  onDropFiles?: (files: File[]) => void;
  /** While a run is active the send button becomes the stop button (spec v6). */
  onStop?: (() => void) | undefined;
  stopping?: boolean;
}

const MAX_HEIGHT = 200;

/**
 * Spec §4.3 输入框: glass, grows 1–8 lines, Enter inserts a newline, Ctrl/⌘+Enter sends.
 * The send button turns solid only once there is something to send.
 * “+” and dropped files open the add-sources panel (M3).
 */
export const Composer = forwardRef<ComposerHandle, ComposerProps>(function Composer(props, ref) {
  const { variant, value, onChange, onSubmit, onOpenSettings, label, placeholder, submitLabel, disabled, busy, canSubmit, tokens = [], error, onAddSources, addDisabled, onDropFiles, onStop, stopping = false } = props;
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const [dragging, setDragging] = useState(false);
  const acceptsDrop = Boolean(onDropFiles) && !addDisabled;
  const hasFiles = (event: DragEvent) => Array.from(event.dataTransfer.types).includes("Files");
  const dropHandlers = acceptsDrop ? {
    onDragOver: (event: DragEvent<HTMLDivElement>) => { if (!hasFiles(event)) return; event.preventDefault(); setDragging(true); },
    onDragLeave: (event: DragEvent<HTMLDivElement>) => { if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDragging(false); },
    onDrop: (event: DragEvent<HTMLDivElement>) => {
      if (!hasFiles(event)) return;
      event.preventDefault();
      setDragging(false);
      onDropFiles?.(Array.from(event.dataTransfer.files));
    },
  } : {};
  useImperativeHandle(ref, () => ({ focus: () => textareaRef.current?.focus() }), []);

  // Auto-grow through the CSSOM (allowed by the CSP, unlike style attributes in markup).
  useLayoutEffect(() => {
    const element = textareaRef.current;
    if (!element) return;
    element.style.height = "auto";
    element.style.height = `${Math.min(element.scrollHeight, MAX_HEIGHT)}px`;
  }, [value]);

  const ready = canSubmit && !disabled && !busy;
  const submit = () => {
    if (ready) onSubmit();
  };

  const addButton = onAddSources ? (
    <button type="button" className="icon-btn" onClick={onAddSources} disabled={addDisabled} aria-label="添加资料" title="添加资料">
      <Icon name="plus" />
    </button>
  ) : null;
  const settingsButton = (
    <button type="button" className="icon-btn" onClick={onOpenSettings} aria-label="研究设置" title="研究设置">
      <Icon name="tune" />
    </button>
  );
  const sendButton = onStop ? (
    <button type="button" className="icon-btn send ready stop" onClick={onStop} disabled={stopping} aria-label="停止研究" title={stopping ? "正在停止…" : "停止研究"}>
      <Icon name="stop" size={12} fill="currentColor" />
    </button>
  ) : (
    <button type="button" className={`icon-btn send${ready ? " ready" : ""}`} onClick={submit} disabled={!ready} aria-label={submitLabel} title={`${submitLabel}（Ctrl/⌘ + Enter）`}>
      <Icon name="send" size={16} />
    </button>
  );
  const textarea = (
    <textarea
      ref={textareaRef}
      rows={1}
      value={value}
      disabled={disabled}
      aria-label={label}
      placeholder={placeholder}
      onChange={(event) => onChange(event.target.value)}
      onKeyDown={(event) => {
        if (event.key === "Enter" && (event.metaKey || event.ctrlKey) && !event.nativeEvent.isComposing) {
          event.preventDefault();
          submit();
        }
      }}
    />
  );

  return (
    <div className={`composer glass${dragging ? " dragover" : ""}`} {...dropHandlers}>
      {tokens.length > 0 ? (
        <div className="tokens">
          {tokens.map((token) => <Token key={token.key} label={token.label} removeLabel={token.removeLabel} onRemove={token.onRemove} />)}
        </div>
      ) : null}
      {variant === "home" ? (
        <>
          {textarea}
          <div className="bar">
            {addButton}
            <span className="grow" />
            {settingsButton}
            {sendButton}
          </div>
        </>
      ) : (
        <div className="bar">
          {addButton}
          {textarea}
          {settingsButton}
          {sendButton}
        </div>
      )}
      {error ? <div className="error" role="alert">{error}</div> : null}
    </div>
  );
});
