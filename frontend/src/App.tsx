import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type { LucideIcon } from "lucide-react";
import {
  Activity,
  Bot,
  Cable,
  CandlestickChart,
  CircleAlert,
  CircleCheck,
  CircleStop,
  CircleX,
  ClipboardList,
  Database,
  Gauge,
  HeartPulse,
  KeyRound,
  LayoutDashboard,
  LockKeyhole,
  Radar,
  Radio,
  RefreshCw,
  ScrollText,
  ShieldCheck,
  Signal,
  SlidersHorizontal,
  Table2,
  TestTube,
  Wifi,
} from "lucide-react";

import { useControlPlaneStream } from "./api/controlPlane";
import { fetchHealth } from "./api/health";
import {
  fetchRiskConfigPreview,
  type RiskConfigPreview,
} from "./api/riskPreview";
import "./App.css";

type WorkspaceView =
  | "overview"
  | "market"
  | "planner"
  | "positions"
  | "risk"
  | "strategy"
  | "ai"
  | "health"
  | "audit"
  | "connection";

type Scenario =
  | "nominal"
  | "loading"
  | "stale"
  | "disconnected"
  | "partial_fill"
  | "stop_missing"
  | "halted";

type Tone = "positive" | "warning" | "danger" | "info" | "muted";

type NavigationItem = {
  id: WorkspaceView;
  label: string;
  icon: LucideIcon;
};

type ScenarioProfile = {
  label: string;
  tone: Tone;
  market: string;
  plan: string;
  position: string;
  stop: string;
  system: string;
};

const navigationItems: NavigationItem[] = [
  { id: "overview", label: "Overview", icon: LayoutDashboard },
  { id: "market", label: "Market Radar", icon: Radar },
  { id: "planner", label: "Trade Planner", icon: CandlestickChart },
  { id: "positions", label: "Positions & Orders", icon: Table2 },
  { id: "risk", label: "Risk Center", icon: ShieldCheck },
  { id: "strategy", label: "Strategy Lab", icon: TestTube },
  { id: "ai", label: "AI Center", icon: Bot },
  { id: "health", label: "System Health", icon: HeartPulse },
  { id: "audit", label: "Audit Log", icon: ScrollText },
  { id: "connection", label: "Connection Wizard", icon: KeyRound },
];

const viewDescriptions: Record<WorkspaceView, string> = {
  overview: "Local safety and market-control status.",
  market: "Shadow market inputs and freshness gates.",
  planner: "Decimal plan preview with exchange filters still locked.",
  positions: "Local projection only. Exchange execution remains unavailable.",
  risk: "Backend-authoritative hard caps and containment state.",
  strategy: "Research candidates and no-trade baseline evidence.",
  ai: "Structured advisory boundary and local cost guard.",
  health: "Local service, stream, database, and clock status.",
  audit: "Append-only local safety events.",
  connection: "Local activation gate remains locked through Phase 13.",
};

const scenarioProfiles: Record<Scenario, ScenarioProfile> = {
  nominal: {
    label: "Nominal",
    tone: "positive",
    market: "Market data fresh",
    plan: "No plan armed",
    position: "No bot position",
    stop: "Stop not required",
    system: "Nominal",
  },
  loading: {
    label: "Loading",
    tone: "info",
    market: "Loading local workspace",
    plan: "Plan loading",
    position: "Position data loading",
    stop: "Protection state loading",
    system: "Loading",
  },
  stale: {
    label: "Stale data",
    tone: "warning",
    market: "Market data stale",
    plan: "New entries paused",
    position: "No bot position",
    stop: "Stop state unchanged",
    system: "Freshness gate active",
  },
  disconnected: {
    label: "Disconnected",
    tone: "danger",
    market: "Market stream disconnected",
    plan: "New entries paused",
    position: "Reconciliation required",
    stop: "Protection verification pending",
    system: "Connection recovery",
  },
  partial_fill: {
    label: "Partial fill",
    tone: "info",
    market: "Market data fresh",
    plan: "Plan monitored",
    position: "Partial fill simulated",
    stop: "Exit quantities refreshed",
    system: "Reconciliation queued",
  },
  stop_missing: {
    label: "Stop missing",
    tone: "danger",
    market: "Market data fresh",
    plan: "New entries blocked",
    position: "Risk reduction required",
    stop: "Server-side stop missing",
    system: "Protection incident",
  },
  halted: {
    label: "Halted",
    tone: "danger",
    market: "Market data monitored",
    plan: "Automation halted",
    position: "No new entries",
    stop: "Protection retained",
    system: "Containment halt active",
  },
};

function StatusBadge({ tone, children }: { tone: Tone; children: string }) {
  return (
    <span className={`status-badge status-${tone}`}>
      <i aria-hidden="true" />
      {children}
    </span>
  );
}

function Panel({
  icon: Icon,
  title,
  detail,
  children,
  className = "",
}: {
  icon: LucideIcon;
  title: string;
  detail?: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <section className={`panel ${className}`} aria-label={title}>
      <header className="panel-header">
        <div className="panel-title">
          <Icon aria-hidden="true" size={17} strokeWidth={1.8} />
          <div>
            <h2>{title}</h2>
            {detail ? <p>{detail}</p> : null}
          </div>
        </div>
      </header>
      <div className="panel-body">{children}</div>
    </section>
  );
}

function Metric({
  label,
  value,
  detail,
  tone = "muted",
}: {
  label: string;
  value: string;
  detail: string;
  tone?: Tone;
}) {
  return (
    <section className="metric" aria-label={label}>
      <p>{label}</p>
      <strong>{value}</strong>
      <StatusBadge tone={tone}>{detail}</StatusBadge>
    </section>
  );
}

function PriceTrace({ scenario }: { scenario: Scenario }) {
  const profile = scenarioProfiles[scenario];
  const traceColor = profile.tone === "danger" ? "#e46d70" : "#5ec5a2";
  return (
    <div className="price-trace">
      <svg
        aria-label="Local BTCUSDT shadow price trace"
        role="img"
        viewBox="0 0 720 210"
        preserveAspectRatio="none"
      >
        <line x1="0" x2="720" y1="42" y2="42" className="chart-grid" />
        <line x1="0" x2="720" y1="105" y2="105" className="chart-grid" />
        <line x1="0" x2="720" y1="168" y2="168" className="chart-grid" />
        <polyline
          className="chart-line-muted"
          points="0,145 40,132 84,148 126,105 168,122 210,80 252,91 294,70 336,96 378,62 420,82 462,55 504,74 546,49 588,69 630,41 675,58 720,36"
        />
        <polyline
          points="0,145 40,132 84,148 126,105 168,122 210,80 252,91 294,70 336,96 378,62 420,82 462,55 504,74 546,49 588,69 630,41 675,58 720,36"
          fill="none"
          stroke={traceColor}
          strokeWidth="3"
        />
        <line x1="0" x2="720" y1="116" y2="116" className="chart-reference" />
      </svg>
      <div className="trace-meta">
        <span>Shadow reference 64,218.40</span>
        <span>Data path: public only</span>
      </div>
    </div>
  );
}

function Overview({ scenario }: { scenario: Scenario }) {
  const profile = scenarioProfiles[scenario];
  return (
    <div className="screen-grid overview-grid">
      <section className="metric-grid" aria-label="Core safety metrics">
        <Metric
          label="Pilot cap"
          value="20.00 USDT"
          detail="Backend hard cap"
          tone="positive"
        />
        <Metric label="Open risk" value="0.00 USDT" detail="No position" />
        <Metric
          label="Daily loss"
          value="0.00 / 0.30"
          detail="Within limit"
          tone="positive"
        />
        <Metric
          label="AI budget"
          value="0.000 / 0.020"
          detail="Model unavailable"
          tone="muted"
        />
        <Metric
          label="System"
          value={profile.system}
          detail={profile.label}
          tone={profile.tone}
        />
      </section>

      <Panel
        icon={CandlestickChart}
        title="BTCUSDT shadow market"
        detail="Public market data only"
        className="panel-wide"
      >
        <PriceTrace scenario={scenario} />
      </Panel>

      <Panel
        icon={ClipboardList}
        title="Active trade plan"
        detail="Local projection"
      >
        <dl className="key-value-list">
          <div>
            <dt>State</dt>
            <dd>{profile.plan}</dd>
          </div>
          <div>
            <dt>Variant</dt>
            <dd>Equal interval</dd>
          </div>
          <div>
            <dt>Maximum loss</dt>
            <dd>0.10 USDT</dd>
          </div>
          <div>
            <dt>Position state</dt>
            <dd>{profile.position}</dd>
          </div>
          <div>
            <dt>Server stop</dt>
            <dd>{profile.stop}</dd>
          </div>
        </dl>
      </Panel>

      <Panel
        icon={Table2}
        title="Recent local events"
        detail="Append-only ledger"
      >
        <div className="event-list">
          <div>
            <span>09:42:06</span>
            <strong>Candidate gate</strong>
            <StatusBadge tone="muted">No signal</StatusBadge>
          </div>
          <div>
            <span>09:41:58</span>
            <strong>Market snapshot</strong>
            <StatusBadge tone={profile.tone}>{profile.market}</StatusBadge>
          </div>
          <div>
            <span>09:41:52</span>
            <strong>Execution lock</strong>
            <StatusBadge tone="warning">Locked</StatusBadge>
          </div>
        </div>
      </Panel>
    </div>
  );
}

function MarketRadar({ scenario }: { scenario: Scenario }) {
  const profile = scenarioProfiles[scenario];
  return (
    <div className="screen-grid two-columns">
      <Panel
        icon={Radar}
        title="Market radar"
        detail="Whitelist and freshness controls"
        className="panel-wide"
      >
        <div className="data-table-wrap">
          <table>
            <thead>
              <tr>
                <th>Symbol</th>
                <th>Mark</th>
                <th>Spread</th>
                <th>ATR</th>
                <th>Funding</th>
                <th>Freshness</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td>BTCUSDT</td>
                <td>64,218.40</td>
                <td>0.8 bp</td>
                <td>612.20</td>
                <td>0.004%</td>
                <td>
                  <StatusBadge tone={profile.tone}>
                    {profile.market}
                  </StatusBadge>
                </td>
              </tr>
              <tr>
                <td>ETHUSDT</td>
                <td>3,486.10</td>
                <td>1.2 bp</td>
                <td>51.40</td>
                <td>0.006%</td>
                <td>
                  <StatusBadge tone="muted">Not selected</StatusBadge>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </Panel>
      <Panel
        icon={Signal}
        title="Freshness gate"
        detail="New entries require current data"
      >
        <div className="state-stack">
          <StatusBadge tone={profile.tone}>{profile.market}</StatusBadge>
          <p>
            Public REST and local WebSocket status are displayed separately from
            any private account stream.
          </p>
        </div>
      </Panel>
    </div>
  );
}

function Planner({ scenario }: { scenario: Scenario }) {
  const profile = scenarioProfiles[scenario];
  return (
    <div className="screen-grid two-columns">
      <Panel
        icon={SlidersHorizontal}
        title="Trade plan preview"
        detail="Decimal-only planner"
        className="panel-wide"
      >
        <div className="data-table-wrap">
          <table>
            <thead>
              <tr>
                <th>Stage</th>
                <th>Reference price</th>
                <th>Quantity</th>
                <th>Fill state</th>
                <th>Risk contribution</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td>1</td>
                <td>64,100.00</td>
                <td>0.000</td>
                <td>
                  <StatusBadge tone="muted">Not submitted</StatusBadge>
                </td>
                <td>0.00 USDT</td>
              </tr>
              <tr>
                <td>2</td>
                <td>63,820.00</td>
                <td>0.000</td>
                <td>
                  <StatusBadge
                    tone={scenario === "partial_fill" ? "info" : "muted"}
                  >
                    {scenario === "partial_fill"
                      ? "Partial simulation"
                      : "Not submitted"}
                  </StatusBadge>
                </td>
                <td>0.00 USDT</td>
              </tr>
            </tbody>
          </table>
        </div>
      </Panel>
      <Panel
        icon={ShieldCheck}
        title="Planner gate"
        detail="Filter failures skip the trade"
      >
        <dl className="key-value-list">
          <div>
            <dt>Hard-cap check</dt>
            <dd>Pass</dd>
          </div>
          <div>
            <dt>Exchange filters</dt>
            <dd>Preview only</dd>
          </div>
          <div>
            <dt>Entry state</dt>
            <dd>{profile.plan}</dd>
          </div>
          <div>
            <dt>Maximum loss</dt>
            <dd>0.10 USDT</dd>
          </div>
        </dl>
      </Panel>
    </div>
  );
}

function Positions({ scenario }: { scenario: Scenario }) {
  const profile = scenarioProfiles[scenario];
  return (
    <div className="screen-grid two-columns">
      <Panel
        icon={Table2}
        title="Positions and orders"
        detail="No exchange account is connected"
        className="panel-wide"
      >
        <div className="empty-state">
          <CircleStop aria-hidden="true" size={28} strokeWidth={1.5} />
          <strong>{profile.position}</strong>
          <span>
            Normal orders and protective algo orders remain unavailable in this
            local workspace.
          </span>
        </div>
      </Panel>
      <Panel
        icon={LockKeyhole}
        title="Protection"
        detail="Server-side stop policy"
      >
        <div className="state-stack">
          <StatusBadge
            tone={scenario === "stop_missing" ? "danger" : "warning"}
          >
            {profile.stop}
          </StatusBadge>
          <p>
            When protection cannot be confirmed, the local state blocks new
            entries and routes toward containment.
          </p>
        </div>
      </Panel>
    </div>
  );
}

function RiskCenter({
  scenario,
  riskPreview,
  riskPreviewLoading,
}: {
  scenario: Scenario;
  riskPreview: RiskConfigPreview | undefined;
  riskPreviewLoading: boolean;
}) {
  const profile = scenarioProfiles[scenario];
  const previewDetail = riskPreview
    ? "Read-only backend preview"
    : riskPreviewLoading
      ? "Loading backend preview"
      : "Backend preview unavailable";
  const previewState = riskPreview ? "Enforced" : "Unavailable";
  const previewTone: Tone = riskPreview ? "positive" : "warning";
  return (
    <div className="screen-grid two-columns">
      <Panel
        icon={ShieldCheck}
        title="Backend hard caps"
        detail={previewDetail}
        className="panel-wide"
      >
        <div className="data-table-wrap">
          <table>
            <thead>
              <tr>
                <th>Control</th>
                <th>Ceiling</th>
                <th>Current</th>
                <th>State</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td>Pilot equity</td>
                <td>
                  {riskPreview
                    ? `${riskPreview.pilot_equity_cap_usdt} USDT`
                    : "--"}
                </td>
                <td>Not activated</td>
                <td>
                  <StatusBadge tone={previewTone}>{previewState}</StatusBadge>
                </td>
              </tr>
              <tr>
                <td>Leverage</td>
                <td>{riskPreview ? `${riskPreview.max_leverage}x` : "--"}</td>
                <td>0x</td>
                <td>
                  <StatusBadge tone={previewTone}>{previewState}</StatusBadge>
                </td>
              </tr>
              <tr>
                <td>Risk per trade</td>
                <td>
                  {riskPreview
                    ? `${riskPreview.risk_per_trade_usdt} USDT`
                    : "--"}
                </td>
                <td>0.00 USDT</td>
                <td>
                  <StatusBadge tone={previewTone}>{previewState}</StatusBadge>
                </td>
              </tr>
              <tr>
                <td>Daily loss</td>
                <td>
                  {riskPreview
                    ? `${riskPreview.daily_loss_limit_usdt} USDT`
                    : "--"}
                </td>
                <td>0.00 USDT</td>
                <td>
                  <StatusBadge
                    tone={
                      scenario === "halted"
                        ? "danger"
                        : riskPreview
                          ? "positive"
                          : "warning"
                    }
                  >
                    {scenario === "halted"
                      ? "Halted"
                      : riskPreview
                        ? "Enforced"
                        : "Unavailable"}
                  </StatusBadge>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </Panel>
      <Panel
        icon={CircleAlert}
        title="Circuit breakers"
        detail="Unsafe states only reduce authority"
      >
        <div className="state-stack">
          <StatusBadge tone={profile.tone}>{profile.system}</StatusBadge>
          <p>
            UI values can lower limits but cannot make an above-cap request pass
            the backend policy.
          </p>
          <dl className="key-value-list compact-list">
            <div>
              <dt>Position mode</dt>
              <dd>{riskPreview?.position_mode ?? "--"}</dd>
            </div>
            <div>
              <dt>Margin</dt>
              <dd>{riskPreview?.margin_type ?? "--"}</dd>
            </div>
            <div>
              <dt>Server-side stop</dt>
              <dd>
                {riskPreview?.require_server_side_stop ? "Required" : "--"}
              </dd>
            </div>
          </dl>
        </div>
      </Panel>
    </div>
  );
}

function StrategyLab({ scenario }: { scenario: Scenario }) {
  const profile = scenarioProfiles[scenario];
  return (
    <div className="screen-grid two-columns">
      <Panel
        icon={TestTube}
        title="Strategy laboratory"
        detail="Research-only candidate evaluation"
        className="panel-wide"
      >
        <div className="strategy-grid">
          <div>
            <span>Trend pullback</span>
            <StatusBadge tone="muted">No candidate</StatusBadge>
          </div>
          <div>
            <span>Volatility breakout</span>
            <StatusBadge tone={scenario === "stale" ? "warning" : "muted"}>
              {scenario === "stale" ? "Freshness blocked" : "No candidate"}
            </StatusBadge>
          </div>
          <div>
            <span>Mean reversion</span>
            <StatusBadge tone="muted">No candidate</StatusBadge>
          </div>
          <div>
            <span>No-trade baseline</span>
            <StatusBadge tone="positive">Active</StatusBadge>
          </div>
        </div>
      </Panel>
      <Panel
        icon={Gauge}
        title="Candidate gate"
        detail="D-027 no-trade behavior"
      >
        <div className="state-stack">
          <StatusBadge tone={profile.tone}>{profile.plan}</StatusBadge>
          <p>
            Scan cadence changes how often evidence is reviewed. It never
            creates a trade obligation.
          </p>
        </div>
      </Panel>
    </div>
  );
}

function AICenter() {
  return (
    <div className="screen-grid two-columns">
      <Panel
        icon={Bot}
        title="AI assessment boundary"
        detail="Structured advisory only"
        className="panel-wide"
      >
        <div className="ai-boundary-grid">
          <div>
            <span>Mode</span>
            <strong>Advisory</strong>
          </div>
          <div>
            <span>Model</span>
            <strong>Unavailable until authorized account check</strong>
          </div>
          <div>
            <span>Tools</span>
            <strong>None</strong>
          </div>
          <div>
            <span>Daily budget</span>
            <strong>0.000 / 0.020 USD</strong>
          </div>
        </div>
      </Panel>
      <Panel
        icon={LockKeyhole}
        title="Authority lock"
        detail="No execution capability"
      >
        <div className="state-stack">
          <StatusBadge tone="warning">Model unavailable</StatusBadge>
          <p>
            Structured output can explain risk. It cannot access credentials,
            change a limit, remove protection, or call an order function.
          </p>
        </div>
      </Panel>
    </div>
  );
}

function SystemHealth({
  scenario,
  streamState,
}: {
  scenario: Scenario;
  streamState: string;
}) {
  const profile = scenarioProfiles[scenario];
  return (
    <div className="screen-grid two-columns">
      <Panel
        icon={HeartPulse}
        title="Local service health"
        detail="Control-plane components"
        className="panel-wide"
      >
        <div className="health-grid">
          <div>
            <Database aria-hidden="true" size={18} />
            <span>Local database</span>
            <StatusBadge tone="positive">Ready</StatusBadge>
          </div>
          <div>
            <Wifi aria-hidden="true" size={18} />
            <span>Control stream</span>
            <StatusBadge tone={streamState === "live" ? "positive" : "warning"}>
              {streamState}
            </StatusBadge>
          </div>
          <div>
            <Cable aria-hidden="true" size={18} />
            <span>User stream</span>
            <StatusBadge tone="warning">Locked</StatusBadge>
          </div>
          <div>
            <Activity aria-hidden="true" size={18} />
            <span>Market path</span>
            <StatusBadge tone={profile.tone}>{profile.market}</StatusBadge>
          </div>
        </div>
      </Panel>
      <Panel
        icon={Radio}
        title="Recovery posture"
        detail="Unsafe data blocks entry"
      >
        <div className="state-stack">
          <StatusBadge tone={profile.tone}>{profile.system}</StatusBadge>
          <p>
            Reconnect, clock, database, and protection faults remain visible
            before any future live activation is eligible.
          </p>
        </div>
      </Panel>
    </div>
  );
}

function AuditLog({ scenario }: { scenario: Scenario }) {
  const profile = scenarioProfiles[scenario];
  return (
    <Panel
      icon={ScrollText}
      title="Immutable audit log"
      detail="Local events only"
    >
      <div className="data-table-wrap">
        <table>
          <thead>
            <tr>
              <th>UTC</th>
              <th>Event</th>
              <th>Source</th>
              <th>Result</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td>2026-07-11 09:42:06</td>
              <td>Candidate evaluation</td>
              <td>Strategy lab</td>
              <td>
                <StatusBadge tone="muted">No candidate</StatusBadge>
              </td>
            </tr>
            <tr>
              <td>2026-07-11 09:42:02</td>
              <td>Risk hard-cap check</td>
              <td>Backend</td>
              <td>
                <StatusBadge tone="positive">Pass</StatusBadge>
              </td>
            </tr>
            <tr>
              <td>2026-07-11 09:41:58</td>
              <td>Market freshness</td>
              <td>Public stream</td>
              <td>
                <StatusBadge tone={profile.tone}>{profile.market}</StatusBadge>
              </td>
            </tr>
            <tr>
              <td>2026-07-11 09:41:52</td>
              <td>Execution boundary</td>
              <td>Adapter</td>
              <td>
                <StatusBadge tone="warning">Locked</StatusBadge>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </Panel>
  );
}

function ConnectionWizard() {
  return (
    <div className="screen-grid two-columns">
      <Panel
        icon={KeyRound}
        title="Activation gate"
        detail="Local activation path"
        className="panel-wide"
      >
        <div className="wizard-lock">
          <LockKeyhole aria-hidden="true" size={32} strokeWidth={1.5} />
          <div>
            <strong>Phase 14 lock active</strong>
            <span>
              Credential and permission surfaces are not available in this
              phase.
            </span>
          </div>
        </div>
        <ol className="wizard-steps">
          <li>
            <CircleCheck aria-hidden="true" size={17} />
            <span>Local safety foundation</span>
            <StatusBadge tone="positive">Complete</StatusBadge>
          </li>
          <li>
            <LockKeyhole aria-hidden="true" size={17} />
            <span>Credential boundary</span>
            <StatusBadge tone="warning">Locked</StatusBadge>
          </li>
          <li>
            <LockKeyhole aria-hidden="true" size={17} />
            <span>Read-only verification</span>
            <StatusBadge tone="warning">Locked</StatusBadge>
          </li>
          <li>
            <LockKeyhole aria-hidden="true" size={17} />
            <span>Activation review</span>
            <StatusBadge tone="warning">Locked</StatusBadge>
          </li>
        </ol>
      </Panel>
      <Panel icon={LockKeyhole} title="Execution state" detail="Feature lock">
        <div className="state-stack">
          <StatusBadge tone="warning">Live execution locked</StatusBadge>
          <button
            type="button"
            className="locked-command"
            disabled
            title="Execution remains locked"
          >
            <LockKeyhole aria-hidden="true" size={16} />
            Connection lock active
          </button>
        </div>
      </Panel>
    </div>
  );
}

function WorkspaceScreen({
  activeView,
  scenario,
  streamState,
  riskPreview,
  riskPreviewLoading,
}: {
  activeView: WorkspaceView;
  scenario: Scenario;
  streamState: string;
  riskPreview: RiskConfigPreview | undefined;
  riskPreviewLoading: boolean;
}) {
  switch (activeView) {
    case "overview":
      return <Overview scenario={scenario} />;
    case "market":
      return <MarketRadar scenario={scenario} />;
    case "planner":
      return <Planner scenario={scenario} />;
    case "positions":
      return <Positions scenario={scenario} />;
    case "risk":
      return (
        <RiskCenter
          scenario={scenario}
          riskPreview={riskPreview}
          riskPreviewLoading={riskPreviewLoading}
        />
      );
    case "strategy":
      return <StrategyLab scenario={scenario} />;
    case "ai":
      return <AICenter />;
    case "health":
      return <SystemHealth scenario={scenario} streamState={streamState} />;
    case "audit":
      return <AuditLog scenario={scenario} />;
    case "connection":
      return <ConnectionWizard />;
  }
}

function App() {
  const [activeView, setActiveView] = useState<WorkspaceView>("overview");
  const [scenario, setScenario] = useState<Scenario>("nominal");
  const [haltStep, setHaltStep] = useState<0 | 1 | 2>(0);
  const healthQuery = useQuery({
    queryKey: ["api-health"],
    queryFn: fetchHealth,
  });
  const riskPreviewQuery = useQuery({
    queryKey: ["risk-config-preview"],
    queryFn: fetchRiskConfigPreview,
  });
  const { snapshot, streamState } = useControlPlaneStream();
  const profile = scenarioProfiles[scenario];
  const activeNavigation = useMemo(
    () =>
      navigationItems.find((item) => item.id === activeView) ??
      navigationItems[0],
    [activeView],
  );

  const apiTone: Tone = healthQuery.isPending
    ? "info"
    : healthQuery.isError
      ? "danger"
      : "positive";
  const apiLabel = healthQuery.isPending
    ? "Checking local API"
    : healthQuery.isError
      ? "Local API unavailable"
      : "Local API healthy";
  const streamLabel =
    streamState === "live"
      ? "Control stream live"
      : `Control stream ${streamState}`;
  const haltLabel =
    haltStep === 0
      ? "Containment halt"
      : haltStep === 1
        ? "Confirm local halt"
        : "Containment halted";

  return (
    <div className="control-shell">
      <aside className="sidebar">
        <div className="brand" aria-label="Binance AI Trader">
          <span className="brand-mark" aria-hidden="true">
            BT
          </span>
          <div>
            <strong>Binance AI Trader</strong>
            <span>Local control plane</span>
          </div>
        </div>
        <nav className="workspace-nav" aria-label="Workspace sections">
          {navigationItems.map((item) => {
            const Icon = item.icon;
            return (
              <button
                key={item.id}
                type="button"
                className={
                  item.id === activeView ? "nav-item is-active" : "nav-item"
                }
                aria-current={item.id === activeView ? "page" : undefined}
                title={item.label}
                onClick={() => setActiveView(item.id)}
              >
                <Icon aria-hidden="true" size={18} strokeWidth={1.8} />
                <span>{item.label}</span>
              </button>
            );
          })}
        </nav>
        <div className="sidebar-lock">
          <LockKeyhole aria-hidden="true" size={16} />
          <span>Phase 14 lock</span>
        </div>
      </aside>

      <div className="main-column">
        <header className="topbar">
          <div className="topbar-statuses" aria-label="System status">
            <StatusBadge tone={apiTone}>{apiLabel}</StatusBadge>
            <StatusBadge tone={streamState === "live" ? "positive" : "warning"}>
              {streamLabel}
            </StatusBadge>
            <StatusBadge tone="warning">Live execution locked</StatusBadge>
          </div>
          <div className="topbar-actions">
            <button
              type="button"
              className={
                haltStep === 2
                  ? "icon-text-button is-halted"
                  : "icon-text-button"
              }
              aria-label={haltLabel}
              title={haltLabel}
              onClick={() =>
                setHaltStep((step) => (step === 2 ? 0 : ((step + 1) as 1 | 2)))
              }
            >
              <CircleStop aria-hidden="true" size={16} strokeWidth={1.8} />
              {haltLabel}
            </button>
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
          <section
            className="workspace-heading"
            aria-labelledby="workspace-title"
          >
            <div>
              <p className="eyebrow">
                Phase {snapshot.phase} / {snapshot.execution_status}
              </p>
              <h1 id="workspace-title">{activeNavigation.label}</h1>
              <p className="workspace-description">
                {viewDescriptions[activeView]}
              </p>
            </div>
            <label className="scenario-select">
              <span>Workspace scenario</span>
              <select
                value={scenario}
                onChange={(event) =>
                  setScenario(event.target.value as Scenario)
                }
                aria-label="Workspace scenario"
              >
                {Object.entries(scenarioProfiles).map(([value, item]) => (
                  <option key={value} value={value}>
                    {item.label}
                  </option>
                ))}
              </select>
            </label>
          </section>

          <div className="scenario-banner" data-scenario={scenario}>
            {profile.tone === "positive" ? (
              <CircleCheck aria-hidden="true" size={17} />
            ) : profile.tone === "danger" ? (
              <CircleX aria-hidden="true" size={17} />
            ) : (
              <CircleAlert aria-hidden="true" size={17} />
            )}
            <span>{profile.system}</span>
            <StatusBadge tone={profile.tone}>{profile.label}</StatusBadge>
          </div>

          <WorkspaceScreen
            activeView={activeView}
            scenario={scenario}
            streamState={streamState}
            riskPreview={riskPreviewQuery.data}
            riskPreviewLoading={riskPreviewQuery.isPending}
          />
        </main>

        <footer className="app-footer">
          <span>Phase 10 local workspace</span>
          <span>Credentials locked</span>
          <span>Execution disabled</span>
        </footer>
      </div>
    </div>
  );
}

export default App;
