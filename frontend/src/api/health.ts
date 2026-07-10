export type HealthResponse = {
  status: "ok";
  service: "binance-ai-trader";
  phase: 1;
  live_trading_enabled: false;
};

const defaultApiBaseUrl = "http://localhost:8000";

function isHealthResponse(value: unknown): value is HealthResponse {
  if (typeof value !== "object" || value === null) {
    return false;
  }

  const payload = value as Record<string, unknown>;
  return (
    payload.status === "ok" &&
    payload.service === "binance-ai-trader" &&
    payload.phase === 1 &&
    payload.live_trading_enabled === false
  );
}

export async function fetchHealth(): Promise<HealthResponse> {
  const configuredBaseUrl =
    import.meta.env.VITE_API_BASE_URL ?? defaultApiBaseUrl;
  const response = await fetch(
    `${configuredBaseUrl.replace(/\/$/, "")}/api/health`,
    {
      headers: { Accept: "application/json" },
    },
  );

  if (!response.ok) {
    throw new Error("Local health endpoint returned an error.");
  }

  const payload: unknown = await response.json();
  if (!isHealthResponse(payload)) {
    throw new Error("Local health endpoint returned an unexpected payload.");
  }

  return payload;
}
