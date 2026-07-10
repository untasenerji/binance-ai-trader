import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  ClipboardList,
  LockKeyhole,
  Radio,
  RefreshCw,
  ShieldCheck,
} from "lucide-react";

import { fetchHealth } from "./api/health";
import "./App.css";

type StatusTone = "healthy" | "checking" | "offline";

type WorkspacePanelProps = {
  icon: typeof Activity;
  label: string;
  value: string;
  detail: string;
};

function WorkspacePanel({
  icon: Icon,
  label,
  value,
  detail,
}: WorkspacePanelProps) {
  return (
    <section className="workspace-panel" aria-label={label}>
      <div className="panel-heading">
        <Icon aria-hidden="true" size={17} strokeWidth={1.8} />
        <span>{label}</span>
      </div>
      <strong>{value}</strong>
      <p>{detail}</p>
    </section>
  );
}

function App() {
  const healthQuery = useQuery({
    queryKey: ["api-health"],
    queryFn: fetchHealth,
  });

  const apiTone: StatusTone = healthQuery.isPending
    ? "checking"
    : healthQuery.isError
      ? "offline"
      : "healthy";
  const apiLabel = healthQuery.isPending
    ? "Checking local API"
    : healthQuery.isError
      ? "Local API unavailable"
      : "Local API healthy";

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand" aria-label="Binance AI Trader">
          <span className="brand-mark" aria-hidden="true">
            BT
          </span>
          <div>
            <p>Binance AI Trader</p>
            <span>Local control plane</span>
          </div>
        </div>

        <div className="topbar-statuses" aria-label="System status">
          <span className={`status-item status-${apiTone}`} aria-live="polite">
            <i aria-hidden="true" />
            {apiLabel}
          </span>
          <span className="status-item status-locked">
            <LockKeyhole aria-hidden="true" size={14} strokeWidth={1.8} />
            Live execution locked
          </span>
          <button
            type="button"
            className="icon-button"
            aria-label="Refresh local API status"
            title="Refresh local API status"
            onClick={() => {
              void healthQuery.refetch();
            }}
          >
            <RefreshCw
              aria-hidden="true"
              size={17}
              strokeWidth={1.8}
              className={healthQuery.isFetching ? "is-spinning" : undefined}
            />
          </button>
        </div>
      </header>

      <main className="workspace">
        <section className="workspace-intro" aria-labelledby="workspace-title">
          <div>
            <p className="eyebrow">Phase 1</p>
            <h1 id="workspace-title">System workspace</h1>
          </div>
          <div className="safety-state">
            <ShieldCheck aria-hidden="true" size={19} strokeWidth={1.8} />
            <span>Safeguards active</span>
          </div>
        </section>

        <section
          className="workspace-grid"
          aria-label="Initial workspace panels"
        >
          <WorkspacePanel
            icon={Radio}
            label="Market data"
            value="Not connected"
            detail="Public market streams begin in Phase 3."
          />
          <WorkspacePanel
            icon={ClipboardList}
            label="Trade plan"
            value="No plan armed"
            detail="Planning and risk controls begin in later phases."
          />
          <WorkspacePanel
            icon={Activity}
            label="Positions"
            value="No bot position"
            detail="Execution remains disabled in this environment."
          />
          <WorkspacePanel
            icon={ShieldCheck}
            label="System health"
            value={
              healthQuery.data?.status === "ok" ? "Nominal" : "Awaiting API"
            }
            detail="Local health endpoint is the only active backend integration."
          />
        </section>
      </main>

      <footer className="app-footer">
        <span>Phase 1 scaffold</span>
        <span>Credentials not loaded</span>
      </footer>
    </div>
  );
}

export default App;
