import { useEffect, useState } from "react";

export type ControlPlaneSnapshot = {
  phase: 13;
  generated_at_utc: string;
  live_trading_enabled: false;
  execution_status: "locked";
  market_data_status: "shadow_only";
  user_stream_status: "locked";
  database_status: "local_ready";
  ai_status: "model_unavailable";
};

export type StreamState = "connecting" | "live" | "disconnected";

const defaultApiBaseUrl = "http://localhost:8000";

const initialSnapshot: ControlPlaneSnapshot = {
  phase: 13,
  generated_at_utc: "",
  live_trading_enabled: false,
  execution_status: "locked",
  market_data_status: "shadow_only",
  user_stream_status: "locked",
  database_status: "local_ready",
  ai_status: "model_unavailable",
};

function isControlPlaneSnapshot(value: unknown): value is ControlPlaneSnapshot {
  if (typeof value !== "object" || value === null) {
    return false;
  }

  const payload = value as Record<string, unknown>;
  return (
    payload.phase === 13 &&
    typeof payload.generated_at_utc === "string" &&
    payload.live_trading_enabled === false &&
    payload.execution_status === "locked" &&
    payload.market_data_status === "shadow_only" &&
    payload.user_stream_status === "locked" &&
    payload.database_status === "local_ready" &&
    payload.ai_status === "model_unavailable"
  );
}

function controlPlaneStreamUrl(): string {
  const configuredBaseUrl =
    import.meta.env.VITE_API_BASE_URL ?? defaultApiBaseUrl;
  const endpoint = new URL(configuredBaseUrl);
  endpoint.protocol = endpoint.protocol === "https:" ? "wss:" : "ws:";
  endpoint.pathname = `${endpoint.pathname.replace(/\/$/, "")}/api/ws/control-plane`;
  endpoint.search = "";
  return endpoint.toString();
}

export function useControlPlaneStream(): {
  snapshot: ControlPlaneSnapshot;
  streamState: StreamState;
} {
  const [snapshot, setSnapshot] =
    useState<ControlPlaneSnapshot>(initialSnapshot);
  const [streamState, setStreamState] = useState<StreamState>("connecting");

  useEffect(() => {
    if (typeof WebSocket === "undefined") {
      setStreamState("disconnected");
      return undefined;
    }

    let disposed = false;
    let socket: WebSocket | undefined;
    let reconnectTimer: number | undefined;

    const connect = () => {
      if (disposed) {
        return;
      }
      setStreamState("connecting");
      socket = new WebSocket(controlPlaneStreamUrl());
      socket.onopen = () => setStreamState("live");
      socket.onmessage = (event) => {
        if (typeof event.data !== "string") {
          return;
        }
        try {
          const payload: unknown = JSON.parse(event.data);
          if (isControlPlaneSnapshot(payload)) {
            setSnapshot(payload);
            setStreamState("live");
          }
        } catch {
          setStreamState("disconnected");
        }
      };
      socket.onerror = () => setStreamState("disconnected");
      socket.onclose = () => {
        if (!disposed) {
          setStreamState("disconnected");
          reconnectTimer = window.setTimeout(connect, 5_000);
        }
      };
    };

    connect();

    return () => {
      disposed = true;
      if (reconnectTimer !== undefined) {
        window.clearTimeout(reconnectTimer);
      }
      socket?.close();
    };
  }, []);

  return { snapshot, streamState };
}
