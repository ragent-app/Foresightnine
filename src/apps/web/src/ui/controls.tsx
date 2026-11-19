import type { ReactNode } from "react";
import { Icon } from "./icons";

export interface SegmentOption<T extends string> {
  value: T;
  label: string;
  disabled?: boolean;
}

/** Segmented control used both as a choice (aria-pressed) and as panel tabs (role tab). */
export function Segmented<T extends string>({ options, value, onChange, label, inline, asTabs }: {
  options: SegmentOption<T>[];
  value: T;
  onChange: (value: T) => void;
  label: string;
  inline?: boolean;
  asTabs?: boolean;
}) {
  return (
    <div className={`seg${inline ? " inline" : ""}`} role={asTabs ? "tablist" : "group"} aria-label={label}>
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          disabled={option.disabled}
          {...(asTabs ? { role: "tab", "aria-selected": option.value === value } : { "aria-pressed": option.value === value })}
          onClick={() => onChange(option.value)}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}

export function Disclosure({ summary, children, defaultOpen }: { summary: string; children: ReactNode; defaultOpen?: boolean }) {
  return (
    <details className="more" open={defaultOpen}>
      <summary><Icon name="chev" size={14} />{summary}</summary>
      <div className="inner">{children}</div>
    </details>
  );
}

export function Token({ label, onRemove, removeLabel }: { label: string; onRemove: () => void; removeLabel: string }) {
  return (
    <span className="token">
      <span title={label}>{label}</span>
      <button type="button" onClick={onRemove} aria-label={removeLabel}>
        <Icon name="close" size={12} />
      </button>
    </span>
  );
}

export function StatusDot({ tone }: { tone: "running" | "done" | "partial" | "failed" | "none" }) {
  return <span className={`dot${tone === "none" ? "" : ` ${tone}`}`} aria-hidden="true" />;
}
