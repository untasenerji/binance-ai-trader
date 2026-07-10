import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "./App";

function renderApp() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
    },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("App", () => {
  it("renders the safe Phase 1 workspace after a healthy API response", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            status: "ok",
            service: "binance-ai-trader",
            phase: 1,
            live_trading_enabled: false,
          }),
          { status: 200 },
        ),
      ),
    );

    renderApp();

    expect(await screen.findByText("Local API healthy")).toBeInTheDocument();
    expect(screen.getByText("Live execution locked")).toBeInTheDocument();
    expect(screen.getByText("No bot position")).toBeInTheDocument();
    expect(screen.getByText("Credentials not loaded")).toBeInTheDocument();
  });
});
