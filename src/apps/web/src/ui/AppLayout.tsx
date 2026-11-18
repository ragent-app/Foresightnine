import type { ReactNode } from "react";

// UI-owned placeholder; replaced by the design implementation.
export function AppLayout({ children }: { children: ReactNode }) {
  return <div data-layout="app">{children}</div>;
}
