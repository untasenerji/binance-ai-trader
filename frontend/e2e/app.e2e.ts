import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";

async function mockLocalApi(page: Page) {
  await page.route("**/api/health", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        status: "ok",
        service: "binance-ai-trader",
        phase: 13,
        live_trading_enabled: false,
      }),
    });
  });
  await page.route("**/api/risk-config-preview", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
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
      }),
    });
  });
}

test("keeps the Phase 13 workspace safe across operational states", async ({
  page,
}) => {
  await mockLocalApi(page);

  await page.goto("/");

  await expect(page.getByText("Local API healthy")).toBeVisible();
  await expect(page.getByText("Live execution locked")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();
  await expect(page.getByText("No plan armed")).toBeVisible();
  await expect(page.getByText("Credentials locked")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Refresh local API status" }),
  ).toBeVisible();

  const scenario = page.getByLabel("Workspace scenario");
  for (const [value, expected] of [
    ["loading", "Loading local workspace"],
    ["stale", "Market data stale"],
    ["disconnected", "Market stream disconnected"],
    ["partial_fill", "Partial fill simulated"],
    ["stop_missing", "Server-side stop missing"],
    ["halted", "Containment halt active"],
  ]) {
    await scenario.selectOption(value);
    await expect(page.getByText(expected).first()).toBeVisible();
  }

  await page.getByRole("button", { name: "Risk Center" }).click();
  await expect(page.getByText("Read-only backend preview")).toBeVisible();
  await expect(page.getByText("20 USDT")).toBeVisible();

  await page.getByRole("button", { name: "Connection Wizard" }).click();
  await expect(
    page.getByRole("heading", { name: "Connection Wizard" }),
  ).toBeVisible();
  await expect(page.getByText("Phase 14 lock active")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Connection lock active" }),
  ).toBeDisabled();
});

test("renders all workspace views at a tablet viewport", async ({ page }) => {
  await page.setViewportSize({ width: 768, height: 1024 });
  await mockLocalApi(page);
  await page.goto("/");

  for (const label of [
    "Overview",
    "Market Radar",
    "Trade Planner",
    "Positions & Orders",
    "Risk Center",
    "Strategy Lab",
    "AI Center",
    "System Health",
    "Audit Log",
    "Connection Wizard",
  ]) {
    await page.getByRole("button", { name: label }).click();
    await expect(page.locator("#workspace-title")).toHaveText(label);
  }

  await expect(page.getByLabel("Workspace scenario")).toBeVisible();
  await expect(page.getByText("Live execution locked").first()).toBeVisible();
});
