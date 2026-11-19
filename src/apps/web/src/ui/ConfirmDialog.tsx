import * as Dialog from "@radix-ui/react-dialog";
import type { ReactNode } from "react";

export interface ConfirmAction {
  key: string;
  label: string;
  onSelect: () => void;
  tone?: "primary" | "danger" | "plain";
  disabled?: boolean;
}

/**
 * Centered alert dialog for decisions that cannot be skipped (spec §4.3 and §7 unsaved changes).
 * Like the panels it draws its own scrim instead of Dialog.Overlay, which would mount a <style>
 * element blocked by the deployed CSP.
 */
export function ConfirmDialog({ title, description, actions, onCancel, error }: {
  title: string;
  description?: ReactNode;
  actions: ConfirmAction[];
  onCancel: () => void;
  error?: ReactNode;
}) {
  return (
    <Dialog.Root open onOpenChange={(open) => { if (!open) onCancel(); }}>
      <Dialog.Portal>
        <div className="scrim" aria-hidden="true" />
        <Dialog.Content className="confirm glass" role="alertdialog" {...(description ? {} : { "aria-describedby": undefined })}>
          <Dialog.Title asChild><h2>{title}</h2></Dialog.Title>
          {description ? <Dialog.Description asChild><p>{description}</p></Dialog.Description> : null}
          {error ? <p className="inline-error" role="alert">{error}</p> : null}
          <div className="confirm-actions">
            {actions.map((action) => (
              <button
                key={action.key}
                type="button"
                className={`btn lg${action.tone === "primary" ? " primary" : action.tone === "danger" ? " danger" : action.tone === "plain" ? " plain" : ""}`}
                onClick={action.onSelect}
                disabled={action.disabled ?? false}
              >
                {action.label}
              </button>
            ))}
            <Dialog.Close asChild><button type="button" className="btn lg plain">取消</button></Dialog.Close>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
