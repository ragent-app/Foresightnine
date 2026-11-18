import { QueryClient } from "@tanstack/react-query";
import { ApiError } from "../api/http";

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: (count, error) =>
        !(error instanceof ApiError && error.status >= 400 && error.status < 500 && error.status !== 429) && count < 2,
      refetchOnWindowFocus: false,
      staleTime: 5000,
    },
    mutations: { retry: false },
  },
});
