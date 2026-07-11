# Phase 09 Plan: Advisory-Only OpenAI Boundary

## Scope

- Typed `AIProvider` contract for a `POST /v1/responses` shaped request.
- Strict `text.format` JSON Schema request using the project `ai_output_schema.json` contract.
- `off`, `advisory`, `veto_only`, and `post_trade_only` modes with a local mock provider.
- Allowlisted one-way veto guard, local model-availability evidence, timeout/refusal/invalid-output handling, and Decimal daily cost tracking.

## Non-goals

- No OpenAI API key, SDK client, HTTP request, account model-list request, hosted tool, function tool, MCP tool, web search, browser tool, Binance credential, order function, risk mutation, stop mutation, leverage mutation, or execution path.

## Exit Gate

- Responses payload exposes `tools: []`, `tool_choice: "none"`, `parallel_tool_calls: false`, and `store: false`.
- Invalid JSON, refusal, timeout, provider failure, unavailable model, cost-bound breach, and budget exhaustion fail safely.
- Advisory mode cannot change a candidate; `veto_only` can only reject an otherwise accepted pre-trade candidate using an allowlisted structured veto.
- The configured daily AI budget cannot exceed D-026 and all accounting remains Decimal-only.
