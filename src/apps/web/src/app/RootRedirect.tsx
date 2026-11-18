import { useEffect, useState } from "react";
import { Navigate } from "react-router-dom";
import { completeOAuthCallback } from "../auth/oauth";
import { useSession } from "../auth/session";

type CallbackOutcome = Awaited<ReturnType<typeof completeOAuthCallback>>;

// Module-level promise guards against the React StrictMode double effect run
// so the one-time ticket is exchanged only once.
let callbackPromise: Promise<CallbackOutcome> | null = null;

function runCallbackOnce(): Promise<CallbackOutcome> {
  callbackPromise ??= completeOAuthCallback();
  return callbackPromise;
}

export function RootRedirect() {
  const session = useSession();
  const [outcome, setOutcome] = useState<CallbackOutcome | null>(null);

  useEffect(() => {
    let active = true;
    void runCallbackOnce().then((result) => {
      if (active) setOutcome(result);
    });
    return () => {
      active = false;
    };
  }, []);

  if (outcome === null) return null;
  if (session) return <Navigate to="/research" replace />;
  if (outcome === "failed") return <Navigate to="/login?oauth=failed" replace />;
  return <Navigate to="/login" replace />;
}
