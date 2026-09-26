import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

// Du Bois ships two component stylesheets keyed by class prefix. In dark mode
// the runtime emits `du-bois-dark-*` classes, so index-dark.css MUST be present
// or every component renders unstyled. Import both so either theme works.
import "@databricks/design-system/index.css";
import "@databricks/design-system/index-dark.css";
// DM Sans / DM Mono @font-face — Du Bois's component CSS *references* these
// fonts but does not bundle them; without these imports the UI falls back to a
// serif system font and loses the Databricks look entirely.
import "@databricks/design-system/fonts/dm-sans.css";
import "@databricks/design-system/fonts/dm-mono.css";
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
