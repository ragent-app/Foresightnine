import { render } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { storeSession } from "../test/fixtures";
import { queryClient } from "./queryClient";
import { routerFuture, routes } from "./router";

function renderAt(path: string) {
  const router = createMemoryRouter(routes, { future: routerFuture, initialEntries: [path] });
  return render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} future={{ v7_startTransition: true }} />
    </QueryClientProvider>,
  );
}

describe("routes", () => {
  it("redirects unauthenticated /research to LoginPage", () => {
    const { container } = renderAt("/research");
    expect(container.querySelector('[data-page="LoginPage"]')).not.toBeNull();
  });

  it("redirects authenticated /login to HomePage inside the layout", () => {
    storeSession();
    const { container } = renderAt("/login");
    expect(container.querySelector('[data-page="HomePage"]')).not.toBeNull();
    expect(container.querySelector('[data-layout="app"]')).not.toBeNull();
  });

  it("renders NotFoundPage for unknown paths", () => {
    const { container } = renderAt("/nope/nothing");
    expect(container.querySelector('[data-page="NotFoundPage"]')).not.toBeNull();
  });
});
