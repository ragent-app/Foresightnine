import { Outlet, createBrowserRouter, type RouteObject } from "react-router-dom";
import { LoginPage } from "../pages/LoginPage";
import { NewResearchPage } from "../pages/NewResearchPage";
import { NotFoundPage } from "../pages/NotFoundPage";
import { ResearchListPage } from "../pages/ResearchListPage";
import { SettingsPage } from "../pages/SettingsPage";
import { TopicPage } from "../pages/TopicPage";
import { AppLayout } from "../ui/AppLayout";
import { RedirectIfSignedIn, RequireSession } from "./guards";
import { RootRedirect } from "./RootRedirect";

export const routes: RouteObject[] = [
  { path: "/", element: <RootRedirect /> },
  { path: "/login", element: <RedirectIfSignedIn><LoginPage /></RedirectIfSignedIn> },
  {
    element: <RequireSession><AppLayout><Outlet /></AppLayout></RequireSession>,
    children: [
      { path: "/research", element: <ResearchListPage /> },
      { path: "/research/new", element: <NewResearchPage /> },
      { path: "/research/:topicId", element: <TopicPage /> },
      { path: "/settings", element: <SettingsPage /> },
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
