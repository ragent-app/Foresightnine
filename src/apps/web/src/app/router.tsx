import { Navigate, Outlet, createBrowserRouter, type RouteObject } from "react-router-dom";
import { AdminKeysPage } from "../pages/AdminKeysPage";
import { HomePage } from "../pages/HomePage";
import { LoginPage } from "../pages/LoginPage";
import { NotFoundPage } from "../pages/NotFoundPage";
import { SettingsPage } from "../pages/SettingsPage";
import { TopicPage } from "../pages/TopicPage";
import { AppLayout } from "../ui/AppLayout";
import { RedirectIfSignedIn, RequireSession } from "./guards";
import { RootRedirect } from "./RootRedirect";

export const routes: RouteObject[] = [
  { path: "/", element: <RootRedirect /> },
  // Deployed OAuth return URI (and Android App Link); the callback handler checks the exact path.
  { path: "/oauth/complete", element: <RootRedirect /> },
  { path: "/login", element: <RedirectIfSignedIn><LoginPage /></RedirectIfSignedIn> },
  {
    element: <RequireSession><AppLayout><Outlet /></AppLayout></RequireSession>,
    children: [
      // The research home doubles as the new-research entry (design spec §4.2).
      { path: "/research", element: <HomePage /> },
      { path: "/research/new", element: <Navigate to="/research" replace /> },
      { path: "/research/:topicId", element: <TopicPage /> },
      { path: "/settings", element: <SettingsPage /> },
      { path: "/admin/keys", element: <AdminKeysPage /> },
    ],
  },
  { path: "*", element: <NotFoundPage /> },
];

/** Opt in to React Router v7 behaviour now so the upgrade does not change semantics later. */
export const routerFuture = {
  v7_relativeSplatPath: true,
  v7_fetcherPersist: true,
  v7_normalizeFormMethod: true,
  v7_partialHydration: true,
  v7_skipActionErrorRevalidation: true,
} as const;

export const router: ReturnType<typeof createBrowserRouter> = createBrowserRouter(routes, { future: routerFuture });
