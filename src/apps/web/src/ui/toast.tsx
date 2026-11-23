import { useSyncExternalStore } from "react";

export interface ToastAction {
  label: string;
  onAction: () => void;
}

interface Toast {
  id: number;
  text: string;
  tone: "info" | "error";
  action: ToastAction | null;
}

let toasts: Toast[] = [];
let nextId = 1;
const listeners = new Set<() => void>();
const emit = () => listeners.forEach((listener) => listener());

export function dismissToast(id: number): void {
  toasts = toasts.filter((toast) => toast.id !== id);
  emit();
}

/**
 * A short confirmation at the bottom of the window. Toasts with an action (such as 撤销) stay
 * longer, so there is time to use it; only the latest three are kept.
 */
export function showToast(text: string, options: { tone?: "info" | "error"; action?: ToastAction } = {}): number {
  const id = nextId++;
  toasts = [...toasts.slice(-2), { id, text, tone: options.tone ?? "info", action: options.action ?? null }];
  emit();
  window.setTimeout(() => dismissToast(id), options.action ? 8000 : 3000);
  return id;
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function Toasts() {
  const items = useSyncExternalStore(subscribe, () => toasts, () => toasts);
  return (
    <div className="toasts" role="status" aria-live="polite">
      {items.map((toast) => (
        <div key={toast.id} className={`toast glass${toast.tone === "error" ? " error" : ""}`}>
          <span>{toast.text}</span>
          {toast.action ? (
            <button
              type="button"
              className="toast-action"
              onClick={() => {
                dismissToast(toast.id);
                toast.action?.onAction();
              }}
            >
              {toast.action.label}
            </button>
          ) : null}
        </div>
      ))}
    </div>
  );
}
