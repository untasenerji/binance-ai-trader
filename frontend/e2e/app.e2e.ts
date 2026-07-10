import { expect, test } from "@playwright/test";

test("keeps the initial workspace safe and execution locked", async ({
  page,
}) => {
  await page.route("**/api/health", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        status: "ok",
        service: "binance-ai-trader",
        phase: 1,
        live_trading_enabled: false,
      }),
    });
  });

  await page.goto("/");

  await expect(page.getByText("Local API healthy")).toBeVisible();
  await expect(page.getByText("Live execution locked")).toBeVisible();
  await expect(page.getByText("No bot position")).toBeVisible();
  await expect(page.getByText("Credentials not loaded")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Refresh local API status" }),
  ).toBeVisible();
});
