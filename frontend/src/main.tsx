import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { ApiError } from "./api/client";
import App from "./App";
import "./styles.css";
import { ControlsProvider } from "./state/controls";
import { ThemeProvider } from "./theme";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Retry transient failures, but never retry a request the server understood and refused (4xx).
      retry: (count, error) => !(error instanceof ApiError && error.status >= 400 && error.status < 500) && count < 2,
      refetchOnWindowFocus: false,
      staleTime: 30_000,
    },
  },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>
        <ControlsProvider>
          <BrowserRouter>
            <App />
          </BrowserRouter>
        </ControlsProvider>
      </ThemeProvider>
    </QueryClientProvider>
  </StrictMode>,
);
