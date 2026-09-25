import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

// Du Bois ships two component stylesheets keyed by class prefix. In dark mode
// the runtime emits `du-bois-dark-*` classes, so index-dark.css MUST be present
// or every component renders unstyled. Import both so either theme works.
import "@databricks/design-system/index.css";
import "@databricks/design-system/index-dark.css";
import "@/styles/globals.css";
import { routeTree } from "@/types/routeTree.gen";

import { RouterProvider, createRouter } from "@tanstack/react-router";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const queryClient = new QueryClient();

const router = createRouter({
  routeTree,
  context: { queryClient },
  defaultPreload: "intent",
  defaultPreloadStaleTime: 0,
  scrollRestoration: true,
});

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}

const rootElement = document.getElementById("root")!;
if (!rootElement.innerHTML) {
  const root = createRoot(rootElement);
  root.render(
    <StrictMode>
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    </StrictMode>,
  );
}
