import * as Dialog from "@radix-ui/react-dialog";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { useState, type ReactNode } from "react";
import { Icon, type IconName } from "./icons";
import { MEDIA, useCloseOnResize, useMedia } from "./media";

export type MenuEntry =
  | { key: string; label: string; icon?: IconName; onSelect: () => void; disabled?: boolean }
  | "separator";

interface MenuProps {
  label: string;
  items: MenuEntry[];
  /** Trigger content and class; the trigger is always a native button. */
  trigger: ReactNode;
  triggerClassName: string;
  align?: "start" | "end";
}

/**
 * Spec §3.3 and §3.4 rule 6: a glass dropdown from 600px up, an iOS-style action sheet below.
 * DropdownMenu runs with modal={false}; modal menus mount react-remove-scroll, whose injected
 * <style> the deployed CSP blocks.
 */
export function Menu(props: MenuProps) {
  const tablet = useMedia(MEDIA.tablet);
  return tablet ? <DropdownVariant {...props} /> : <ActionSheetVariant {...props} />;
}

function DropdownVariant({ label, items, trigger, triggerClassName, align = "end" }: MenuProps) {
  const [open, setOpen] = useState(false);
  useCloseOnResize(open, () => setOpen(false));
  return (
    <DropdownMenu.Root modal={false} open={open} onOpenChange={setOpen}>
      <DropdownMenu.Trigger asChild>
        <button type="button" className={triggerClassName} aria-label={label} title={label}>{trigger}</button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content className="menu" align={align} sideOffset={6} collisionPadding={16} loop>
          {items.map((item, index) =>
            item === "separator" ? (
              <DropdownMenu.Separator key={`sep-${index}`} />
            ) : (
              <DropdownMenu.Item key={item.key} disabled={item.disabled ?? false} onSelect={() => item.onSelect()}>
                {item.icon ? <Icon name={item.icon} size={16} /> : null}
                {item.label}
              </DropdownMenu.Item>
            ),
          )}
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}

function ActionSheetVariant({ label, items, trigger, triggerClassName }: MenuProps) {
  const [open, setOpen] = useState(false);
  const entries = items.filter((item): item is Exclude<MenuEntry, "separator"> => item !== "separator");
  return (
    <Dialog.Root open={open} onOpenChange={setOpen}>
      <Dialog.Trigger asChild>
        <button type="button" className={triggerClassName} aria-label={label} title={label} aria-haspopup="menu">{trigger}</button>
      </Dialog.Trigger>
      <Dialog.Portal>
        <div className="action-sheet-scrim" aria-hidden="true" />
        <Dialog.Content className="action-sheet" aria-describedby={undefined}>
          <Dialog.Title className="sr-only">{label}</Dialog.Title>
          <div className="group" role="menu">
            {entries.map((item) => (
              <button
                key={item.key}
                type="button"
                role="menuitem"
                disabled={item.disabled}
                onClick={() => {
                  setOpen(false);
                  item.onSelect();
                }}
              >
                {item.label}
              </button>
            ))}
          </div>
          <div className="group">
            <Dialog.Close asChild>
              <button type="button" className="cancel">取消</button>
            </Dialog.Close>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
