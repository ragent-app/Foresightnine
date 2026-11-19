import { forwardRef, useImperativeHandle, useLayoutEffect, useRef, type ReactNode } from "react";
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
}

const MAX_HEIGHT = 200;

/**
 * Spec §4.3 输入框: glass, grows 1–8 lines, Enter inserts a newline, Ctrl/⌘+Enter sends.
 * The send button turns solid only once there is something to send.
 * The add-sources button arrives with source management (M3).
 */
export const Composer = forwardRef<ComposerHandle, ComposerProps>(function Composer(props, ref) {
  const { variant, value, onChange, onSubmit, onOpenSettings, label, placeholder, submitLabel, disabled, busy, canSubmit, tokens = [], error } = props;
  const textareaRef = useRef<HTMLTextAreaElement>(null);
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

  const settingsButton = (
    <button type="button" className="icon-btn" onClick={onOpenSettings} aria-label="研究设置" title="研究设置">
      <Icon name="tune" />
    </button>
  );
  const sendButton = (
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
    <div className="composer glass">
      {tokens.length > 0 ? (
        <div className="tokens">
          {tokens.map((token) => <Token key={token.key} label={token.label} removeLabel={token.removeLabel} onRemove={token.onRemove} />)}
        </div>
      ) : null}
      {variant === "home" ? (
        <>
          {textarea}
          <div className="bar">
            <span className="grow" />
            {settingsButton}
            {sendButton}
          </div>
        </>
      ) : (
        <div className="bar">
          {textarea}
          {settingsButton}
          {sendButton}
        </div>
      )}
      {error ? <div className="error" role="alert">{error}</div> : null}
    </div>
  );
});
