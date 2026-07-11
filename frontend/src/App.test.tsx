import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
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
  it("renders the locked Phase 13 workspace and its safety states", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: string | URL | Request) => {
        const isRiskPreview = input.toString().includes("risk-config-preview");
        const payload = isRiskPreview
          ? {
              phase: 13,
              profile: "live_pilot_20_usdt",
              authority: "backend_hard_cap",
              live_trading_enabled: false,
              pilot_equity_cap_usdt: "20",
              margin_type: "ISOLATED",
              position_mode: "ONE_WAY",
              max_leverage: 2,
              max_concurrent_positions: 1,
              max_active_strategy_count: 1,
              max_stages: 2,
              risk_per_trade_usdt: "0.1",
              daily_loss_limit_usdt: "0.3",
              weekly_drawdown_limit_usdt: "0.8",
              consecutive_loss_limit: 3,
              allow_market_entry: false,
              allow_back_loaded_ladder: false,
              require_server_side_stop: true,
              openai_mode: "advisory",
              openai_daily_budget_usd: "0.02",
            }
          : {
              status: "ok",
              service: "binance-ai-trader",
              phase: 13,
              live_trading_enabled: false,
            };
        return Promise.resolve(
          new Response(JSON.stringify(payload), { status: 200 }),
        );
      }),
    );

    renderApp();

    expect(await screen.findByText("Local API healthy")).toBeInTheDocument();
    expect(screen.getByText("Live execution locked")).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "Overview" }),
    ).toBeInTheDocument();
    expect(screen.getByText("No plan armed")).toBeInTheDocument();
    expect(screen.getByText("Credentials locked")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Risk Center" }));
    expect(
      await screen.findByText("Read-only backend preview"),
    ).toBeInTheDocument();
    expect(screen.getByText("20 USDT")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Overview" }));

    fireEvent.change(screen.getByLabelText("Workspace scenario"), {
      target: { value: "stale" },
    });
    expect(screen.getAllByText("Market data stale").length).toBeGreaterThan(0);

    fireEvent.click(screen.getByRole("button", { name: "Connection Wizard" }));
    expect(
      screen.getByRole("heading", { name: "Connection Wizard" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Phase 14 lock active")).toBeInTheDocument();
  });
});
