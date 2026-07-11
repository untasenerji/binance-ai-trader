export type RiskConfigPreview = {
  phase: 13;
  profile: "live_pilot_20_usdt";
  authority: "backend_hard_cap";
  live_trading_enabled: false;
  pilot_equity_cap_usdt: string;
  margin_type: "ISOLATED";
  position_mode: "ONE_WAY";
  max_leverage: number;
  max_concurrent_positions: number;
  max_active_strategy_count: number;
  max_stages: number;
  risk_per_trade_usdt: string;
  daily_loss_limit_usdt: string;
  weekly_drawdown_limit_usdt: string;
  consecutive_loss_limit: number;
  allow_market_entry: false;
  allow_back_loaded_ladder: false;
  require_server_side_stop: true;
  openai_mode: "advisory";
  openai_daily_budget_usd: string;
};

const defaultApiBaseUrl = "http://localhost:8000";

function isRiskConfigPreview(value: unknown): value is RiskConfigPreview {
  if (typeof value !== "object" || value === null) {
    return false;
  }

  const payload = value as Record<string, unknown>;
  return (
    payload.phase === 13 &&
    payload.profile === "live_pilot_20_usdt" &&
    payload.authority === "backend_hard_cap" &&
    payload.live_trading_enabled === false &&
    typeof payload.pilot_equity_cap_usdt === "string" &&
    payload.margin_type === "ISOLATED" &&
    payload.position_mode === "ONE_WAY" &&
    typeof payload.max_leverage === "number" &&
    typeof payload.max_concurrent_positions === "number" &&
    typeof payload.max_active_strategy_count === "number" &&
    typeof payload.max_stages === "number" &&
    typeof payload.risk_per_trade_usdt === "string" &&
    typeof payload.daily_loss_limit_usdt === "string" &&
    typeof payload.weekly_drawdown_limit_usdt === "string" &&
    typeof payload.consecutive_loss_limit === "number" &&
    payload.allow_market_entry === false &&
    payload.allow_back_loaded_ladder === false &&
    payload.require_server_side_stop === true &&
    payload.openai_mode === "advisory" &&
    typeof payload.openai_daily_budget_usd === "string"
  );
}

export async function fetchRiskConfigPreview(): Promise<RiskConfigPreview> {
  const configuredBaseUrl =
    import.meta.env.VITE_API_BASE_URL ?? defaultApiBaseUrl;
  const response = await fetch(
    `${configuredBaseUrl.replace(/\/$/, "")}/api/risk-config-preview`,
    {
      headers: { Accept: "application/json" },
    },
  );

  if (!response.ok) {
    throw new Error("Local risk preview endpoint returned an error.");
  }

  const payload: unknown = await response.json();
  if (!isRiskConfigPreview(payload)) {
    throw new Error(
      "Local risk preview endpoint returned an unexpected payload.",
    );
  }

  return payload;
}
