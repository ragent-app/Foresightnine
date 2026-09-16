import type { ButtonHTMLAttributes, InputHTMLAttributes, ReactNode, SelectHTMLAttributes, TextareaHTMLAttributes } from "react";

export function Button({ variant = "secondary", children, ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { readonly variant?: "primary" | "secondary" | "quiet" | "caution" | "danger" }) {
  return <button className={`button button-${variant}`} {...props}>{children}</button>;
}

export function Field({ label, error, helper, children }: { readonly label: string; readonly error?: string | undefined; readonly helper?: string | undefined; readonly children: ReactNode }) {
  return <label className="field"><span className="field-label">{label}</span>{children}{helper ? <span className="field-helper">{helper}</span> : null}{error ? <span className="field-error" role="alert">{error}</span> : null}</label>;
}

export function Textarea(props: TextareaHTMLAttributes<HTMLTextAreaElement>) { return <textarea className="text-input thesis-textarea" {...props} />; }
export function Input(props: InputHTMLAttributes<HTMLInputElement>) { return <input className="text-input" {...props} />; }
export function Select(props: SelectHTMLAttributes<HTMLSelectElement>) { return <select className="text-input select-input" {...props} />; }

export function StatusDot({ tone = "accent" }: { readonly tone?: "accent" | "sage" | "blue" | "warning" | "danger" }) { return <span aria-hidden="true" className={`status-dot status-${tone}`} />; }

export function Tag({ children, tone = "neutral" }: { readonly children: ReactNode; readonly tone?: "neutral" | "accent" | "weak" | "strong" | "warning" }) { return <span className={`tag tag-${tone}`}>{children}</span>; }
