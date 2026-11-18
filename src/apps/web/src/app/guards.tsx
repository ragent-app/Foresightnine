import { useEffect, type ReactNode } from "react";
import { Navigate } from "react-router-dom";
import { SESSION_CHANGED_EVENT, useSession } from "../auth/session";
import { queryClient } from "./queryClient";

const CLEARING_REASONS = new Set(["expired", "signed_out", "deleted"]);

function useClearCacheOnSessionEnd(): void {
  useEffect(() => {
    const handler = (event: Event) => {
      const reason = (event as CustomEvent<{ reason?: string }>).detail?.reason;
      if (reason && CLEARING_REASONS.has(reason)) queryClient.clear();
    };
    window.addEventListener(SESSION_CHANGED_EVENT, handler);
    return () => window.removeEventListener(SESSION_CHANGED_EVENT, handler);
  }, []);
}

export function RequireSession({ children }: { children: ReactNode }) {
  const session = useSession();
  useClearCacheOnSessionEnd();
  if (!session) return <Navigate to="/login" replace />;
  return <>{children}</>;
}

export function RedirectIfSignedIn({ children }: { children: ReactNode }) {
  const session = useSession();
  useClearCacheOnSessionEnd();
  if (session) return <Navigate to="/research" replace />;
  return <>{children}</>;
}
