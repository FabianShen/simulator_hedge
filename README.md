# sim_hedge

`sim_hedge` is for simulating hedge, able to do actual hedging.

Commands load the nearest `.env` found from the working directory upward.
Existing PowerShell environment variables take precedence. Copy `.env.example`
to `.env`, fill in local values, and never commit the resulting `.env` file.

The market-data boundary subscribes to `ymm_live_data_sdk` tick channels and
converts vendor dictionaries into internal `MarketQuote` values. The SDK
callback only places batches into a bounded queue; normalization and application
work happen on a separate consumer thread. A thread-safe `MarketState` keeps one
latest quote per instrument and reports whether all required quotes are fresh.
Normal startup loads the active option chain through Data SDK, closes that SDK,
then subscribes through Live SDK to the underlying and every active option.
The first underlying quote selects one maturity and nearby complete call/put
pairs as the strategy universe. Only those instruments must be fresh before
pricing is allowed to run; other fresh, two-sided contracts from that maturity
are then added opportunistically as hedge candidates.
During the run, a monitor reports coverage/readiness once per second and writes
`outputs/market_state.json` atomically for diagnostics. Pricing will consume the
in-memory `MarketState`, not this JSON file.

The external pricing boundary is defined independently in
`protocols/pricing/v1/pricing.proto`. It contains no YMM types, credentials,
positions, or order operations, so compatible Python and C++ engines can run
locally, remotely, or against recorded requests.

Run the dependency-free SABR/Black-76 reference engine without live data:

```powershell
.\.venv\Scripts\python.exe -m pricing_engine
```

Run the same engine behind the local versioned gRPC boundary:

```powershell
# Terminal 1
.\.venv\Scripts\python.exe -m pricing_engine.grpc_server

# Terminal 2
.\.venv\Scripts\python.exe -m sim_hedge.pricing_client.remote `
  protocols\pricing\v1\examples\price_request.json
```

The gRPC client enforces a deadline and verifies the service protocol version.

For continuous live pricing, keep the server running and start the market
gateway with an explicit target:

```powershell
.\.venv\Scripts\python.exe -m sim_hedge `
  --pricing-target 127.0.0.1:50051 `
  --pricing-interval 1 `
  --pricing-timeout 0.5 `
  --pricing-max-age 2
```

Quote callbacks only signal a dedicated pricing worker. The worker coalesces
updates, permits one RPC at a time, retains the latest valid result, and rejects
responses whose input snapshot has exceeded the freshness limit. Continuous
pricing is disabled when `--pricing-target` is omitted. Pricing-only diagnostics
do not require a hedge service.

Once the strategy ledger contains broker-confirmed Alpha/Beta positions, the
same long-running market process can publish live portfolio Greeks and a
versioned incremental Beta proposal after every accepted pricing result:

```powershell
# Terminal 1
.\.venv\Scripts\python.exe -m pricing_engine.grpc_server

# Terminal 2
.\.venv\Scripts\python.exe -m hedge_service.grpc_server `
  --capital 100000000 `
  --delta-entry-risk-band 30000 --delta-target-risk-band 10000 `
  --gamma-entry-risk-band 10000 --gamma-target-risk-band 10000 `
  --depth-excess-penalty 20

# Terminal 3
.\.venv\Scripts\python.exe -m sim_hedge `
  --pricing-target 127.0.0.1:50051 `
  --hedge-target 127.0.0.1:50052 `
  --strategy-ledger outputs\strategy_ledger.json `
  --risk-output outputs\live_risk.json
```

The hedge service routes to its cost-, displayed-depth-, and margin-aware D/G
MILP whenever either notional scenario-risk entry band is breached; otherwise
it runs the stateless MSH policy. Capital and risk bands are service-side
configuration. The emitted proposal identifies the selected D/G-MILP or MSH
policy and includes incremental trades, Gamma-risk improvement, estimated
transaction costs (including D/G depth excess penalties), side-specific displayed
depth and the MSH depth cap, and
short-margin diagnostics. These estimates do not guarantee execution or broker
margin treatment.

D/G displayed depth is a soft cost: each action contract beyond its direction's
displayed depth costs 20 by default, configurable with `--depth-excess-penalty`
on both the service and offline planner. For a synthetic action, direction depth
is the minimum executable size across its legs, and the excess is charged once
per action contract; diagnostics split that charge equally across the legs.
Missing sizes use the trade cap; an explicit zero size incurs excess cost on
every contract. A zero penalty ignores depth cost; a large penalty favors
depth-feasible solutions when available, but does not override the main solver's
hard D/G target bands. Trade (200 per leg), margin, and Alpha's 30% limits
remain hard. In both D/G and MSH, each hedge position is bounded by
`max(position_limit, abs(current_position))`: existing positions above the
default 300 may remain or shrink, but cannot grow in absolute size; new or
smaller positions retain the configured ceiling. D/G enforces nonworsening
of each unbreached Delta/Gamma dimension inside the main MILP, then checks
the proposed hedge's benefit and costs. MSH retains its hard cap at 50% of displayed
depth. D/G proposals can exceed visible liquidity: simulated COUNTERPARTY fills
are an optimistic assumption and real-market fills are not guaranteed.

To express a nonzero hedge center in raw, multiplier-scaled portfolio Greeks,
set the center and its absolute tolerance together. For example,
`--target-delta 5000 --delta-limit 1000` means keep raw Delta within 4000 to 6000;
the raw limit overrides the scenario-risk Delta bands. Gamma works the same way.
Without these flags the engine retains its zero-centered scenario-risk bands.

The ledger is reloaded after every pricing response, so a separately reconciled
fill becomes part of the next risk calculation without restarting the live
feed. The pricing request also reloads the ledger: every fresh, two-sided
contract in the selected near expiry is priced and can become a Beta hedge
candidate. Held contracts without a usable quote are valuation-only (no market
price required), receive Greeks, and cannot become new hedge candidates.
Currently all held options must share that pricing expiry; another expiry
blocks risk until multi-expiry pricing is implemented. Restart an already
running pricing service to pick up the valuation-only behavior.
`live_risk.json` is replaced atomically and contains a proposal ID, its
base ledger revision, confirmed Beta positions, integer incremental trades, the
resulting target Beta positions, and current portfolio Greeks. The required
identity and position equation are documented in
`protocols/hedging/v1/README.md`. It always records `orders_generated: false`.
This market/pricing/risk process never calls the trading API. A separate order
application may take as long as necessary to execute one accepted
`incremental_trades` proposal; only broker-confirmed fills update the ledger.
`target_beta_positions` is a derived audit equation, not the execution goal.
Later live proposals must not make the order application chase a changing
absolute target.
The pricing worker passes the exact market input and its matching pricing result
to the hedge client. Hedge RPC failures publish `NOT_READY` and do not stop the
live feed. Publishing live portfolio hedge proposals with `--strategy-ledger`
requires `--hedge-target`; there is no in-process hedge-policy fallback.

Automatic trading is a separate small coordinator. It reads the atomically
published available-chain Alpha market and hedge proposal, queries authoritative
broker state, reconciles first, and performs at most one Alpha or Beta batch per
cycle. Bind it once to the intended account at startup; individual batches do
not require typed confirmation:

```powershell
# Read-only check before the market opens
.\.venv\Scripts\python.exe -m sim_hedge.auto_trader `
  --enable-live-orders ETO202609151523232103 `
  --max-alpha-contracts YOUR_HARD_ALPHA_LIMIT `
  --max-beta-contracts 10 `
  --preflight

# Remove --preflight only when automatic simulated-account orders are intended
.\.venv\Scripts\python.exe -m sim_hedge.auto_trader `
  --enable-live-orders ETO202609151523232103 `
  --max-alpha-contracts YOUR_HARD_ALPHA_LIMIT `
  --max-beta-contracts 10
```

On an empty account/ledger it initializes Alpha once from the 30% margin rule.
When `--ledger` points to an account-specific directory, the default Alpha
market and risk inputs are `alpha_market.json` and `live_risk.json` beside that
ledger. You can override either with `--alpha-market` or `--risk`. The trader
prints the resolved paths at startup. Always use a registry from the same
account.

If a stale input left **unsubmitted** Alpha intents in the registry, stop the
trader, then run this recovery-only check against the simulator before retrying:

```powershell
.\.venv\Scripts\python.exe -m sim_hedge.auto_trader `
  --enable-live-orders YOUR_ACCOUNT_ID `
  --registry outputs\new_account\order_registry.json `
  --ledger outputs\new_account\strategy_ledger.json `
  --max-alpha-contracts YOUR_HARD_ALPHA_LIMIT `
  --max-beta-contracts 10 `
  --recover-unsubmitted-alpha
```

Recovery sends no orders. It keeps the old intents as abandoned audit history
and refuses to proceed if the broker has positions, active orders, or any order
history on the intents' trading day. Do not delete or hand-edit the registry.

For a partially filled Alpha batch, inspect the pinned target against the
confirmed ledger and latest reconciliation without placing more orders:

```powershell
.\.venv\Scripts\python.exe -m sim_hedge.execution.alpha.continuation `
  outputs\auto\alpha_plan.json `
  outputs\new_account\order_registry.json `
  outputs\new_account\strategy_ledger.json `
  outputs\new_account\reconciliation.json `
  --output outputs\new_account\alpha_continuation.json
```

`WAITING_FOR_BROKER` means at least one registered order is not terminal,
even if the account snapshot omits it from `active_orders`.
`NEEDS_TOP_UP` shows the remaining sells by original option, not a new
margin-sized basket. This assessment command does not submit or cancel anything.
For an existing partially initialized account, `auto_trader` can continue one
previously registered Alpha leg per cycle when broker positions reconcile and
the Alpha snapshot is fresh. A working order for one option does not block a
different option, but the trader will not submit twice for the same option
while its order remains working. It sends
`COUNTERPARTY` without locally checking bid or ask; the simulator resolves or
rejects the order. `BID1_MISSING` delays that leg for 30 seconds while other
legs may proceed, but it does not count as a fill or complete Alpha. Other
broker rejections stop the trader. A cancelled or partially cancelled Alpha order is not topped
up automatically yet; the trader remains blocked rather than guessing a new
order quantity. Restarting `auto_trader` without `--preflight` may submit orders.
After confirmed fills reconcile, it automatically accepts and submits fresh
incremental Beta proposals with `COUNTERPARTY`. It waits while orders are active
and blocks on stale inputs, position mismatch, unhealthy account state, or an
unknown submission outcome. The hard limits are independent safety ceilings;
they do not alter the margin formula. A proposal larger than
`--max-beta-contracts` is blocked, not silently split or resized. Review the
fresh proposal and choose an explicit cap before enabling live simulated
orders.

If you manually cancel a Beta order in the simulator, stop `auto_trader` and
reconcile before doing anything else. For a broker-confirmed `CANCELLED` order
with **zero** fills, this recovery command retires just that unfilled intent:

```powershell
.\.venv\Scripts\python.exe -m sim_hedge.execution.beta.cancelled `
  outputs\new_account\order_registry.json `
  outputs\new_account\strategy_ledger.json `
  outputs\new_account\reconciliation.json `
  --order-id O20260922C4A7E61CE27C4922
```

It sends no orders and retains the broker order ID for audit. It refuses a
partial fill, an active order, changed positions/trades, or stale local files.
After it succeeds, reconcile again; only a new, fresh hedge proposal may
decide whether another Beta order is needed. Do not hand-edit the registry or
use this command for an order with any fills.

To keep the Alpha positions already filled and stop pursuing unsubmitted Alpha
legs, first stop `auto_trader` and run a fresh broker reconciliation.

If reconciliation reports an unknown Alpha submission after a simulator `5xx`,
do **not** retry the order just to clear the flag. For the current account,
inspect and retire that single ID only after fresh, complete broker checks:

```powershell
.\.venv\Scripts\python.exe -m sim_hedge.execution.alpha.unknown `
  outputs\new_account\order_registry.json `
  outputs\new_account\strategy_ledger.json `
  outputs\auto\alpha_continuation_submission.json `
  --client-order-id alpha-EXAMPLE_ID
```

This command sends no orders. It requires matching broker/ledger positions,
no active orders, no matching broker order, and no trades for unregistered
orders; otherwise it leaves the registry unchanged. It writes an audit beside
the registry. If it succeeds, reconcile again before finalizing Alpha:

```powershell
.\.venv\Scripts\python.exe -m sim_hedge.reconcile `
  outputs\new_account\order_registry.json `
  --ledger outputs\new_account\strategy_ledger.json `
  --report-output outputs\new_account\reconciliation.json
```

`safe_for_hedging=False` is expected until the other unsubmitted Alpha intents
are retired. With the refreshed report, adopt only the filled Alpha holdings:

```powershell
.\.venv\Scripts\python.exe -m sim_hedge.execution.alpha.finalize `
  outputs\auto\alpha_plan.json `
  outputs\new_account\order_registry.json `
  outputs\new_account\strategy_ledger.json `
  outputs\new_account\reconciliation.json `
  --output outputs\auto\alpha_adoption.json
```

This command never trades. It preserves the broker-filled Alpha positions,
records the unsubmitted legs as abandoned, and prevents `auto_trader` from
resuming the original Alpha target. It fails if any submission outcome is
unknown, an order is active or not fully filled, or the broker/ledger snapshot
does not match. Do not hand-edit the registry to bypass those checks. The
adoption does **not** fix missing pricing Greeks: live risk and Beta orders stay
blocked until the pricing request covers every held instrument.

Portfolio state comes from the simulated-trading system, not from the market
feed. The first read-only boundary fetches the authoritative absolute snapshot
from `GET /api/accounts/{account_id}/trading-snapshot`, converts monetary and
quantity values to `Decimal`, and replaces the in-memory `PortfolioState` in one
operation. It does not expose any order or account-mutating operation.

Inspect accessible accounts and a normalized portfolio without live market data:

```powershell
$env:SIM_REST_BASE_URL = "http://your-simulator:8000"
$env:SIM_USERNAME = "your-user"
$env:SIM_PASSWORD = "your-password"

.\.venv\Scripts\python.exe -m sim_hedge.state.portfolio_remote --list-accounts
$env:SIM_ACCOUNT_ID = "your-etf-option-account-id"
.\.venv\Scripts\python.exe -m sim_hedge.state.portfolio_remote `
  --output outputs\portfolio_state.json
```

To verify WebSocket recovery without processing incremental events yet:

```powershell
$env:SIM_WS_URL = "ws://your-simulator:8001/ws/trading"
.\.venv\Scripts\python.exe -m sim_hedge.state.portfolio_remote `
  --output outputs\portfolio_state.json
```

This loads REST revision 1, requires the documented first WebSocket business
event to be `SNAPSHOT`, replaces the state as revision 2, and exits. Individual
incremental events are intentionally not applied until their real payloads have
been observed.

After snapshot bootstrap has been verified, keep the position stream running:

```powershell
.\.venv\Scripts\python.exe -m sim_hedge.state.portfolio_remote --watch `
  --output outputs\portfolio_state.json
```

The watcher applies absolute `POSITION_UPDATED` and `POSITION_CLOSED` events in
`business_version` order. On disconnect or Ctrl+C, the in-memory state is marked
unsynchronized. Other event types are not interpreted by this position-only
increment.

The initial Alpha target is built separately from both the strategy universe and
the broker portfolio. `build_short_otm_alpha_plan()` selects the maximum balanced
set of OTM calls and puts from the nearest maturity, excludes ATM, and assigns
one common short quantity to every option. The quantity uses 30% of captured
initial cash as an opening-margin budget. It requires the Live SDK's distinct
`prev_close` and `prev_settlement` fields and never substitutes a current price:

```text
call margin = [option previous settlement + max(12% * underlying previous close
              - call OTM amount, 7% * underlying previous close)] * multiplier
put margin  = min[option previous settlement + max(12% * underlying previous close
              - put OTM amount, 7% * strike), strike] * multiplier
margin capacity = floor(30% * initial cash / sum(one-contract leg margins))
contracts per option = margin capacity
```

The normal live gateway atomically refreshes `outputs/live-alpha-market.json`
from the maximum available nearest-expiry chain after a 10-second collection
window. This is deliberately separate from the
smaller pricing/hedging universe.

Build an inspectable Alpha plan entirely from recorded inputs:

```powershell
.\.venv\Scripts\python.exe -m sim_hedge.execution.alpha.plan `
  outputs\live-alpha-market.json `
  outputs\portfolio_state.json `
  --output outputs\alpha_plan.json
```

The output explicitly records `SHORT_OPTION_OPENING_MARGIN` and
`orders_generated: false`; it is an offline target, not permission to trade.
The standalone inspection command defaults to `--max-contracts-per-option 1`;
the automatic coordinator uses the uncapped margin capacity and applies a
separate total-contract safety limit before submitting.

Generate registered Alpha order intents from saved data without submitting:

```powershell
.\.venv\Scripts\python.exe -m sim_hedge.execution.alpha.orders `
  outputs\alpha_plan.json `
  --exchange-id SZSE `
  --output outputs\alpha_order_dry_run.json `
  --registry-output outputs\order_registry.json
```

Alpha execution uses `COUNTERPARTY`; the simulator resolves its protected price.
The output explicitly says `submission_allowed: false` and `orders_submitted: 0`;
execution must rebuild or validate prices against a fresh live market snapshot.
For this workflow, record a fresh snapshot with
`--record-pricing PATH --stop-after-recording`, then immediately rebuild the
Alpha plan and dry-run.

The standalone submission command remains useful for diagnostics:

```powershell
.\.venv\Scripts\python.exe -m sim_hedge.execution.alpha.submit `
  outputs\alpha_order_dry_run.json `
  outputs\order_registry.json `
  --max-total-contracts 4
```

Before the first POST it independently checks that the source snapshot is at
most 10 seconds old, the account and risk states are `NORMAL`, and the broker
has no positions or active orders. It submits sequentially, saves each returned
`order_id` before continuing, and stops on the first rejection. A timeout,
connection loss, `5xx`, invalid response, or missing `order_id` is recorded as
`SUBMISSION_UNKNOWN`; that client ID cannot be submitted again until the order
is recovered through a read-only query and bound to the registry.

After submission, recover order IDs and rebuild the Alpha ledger only from the
simulator's authoritative order, trade, and portfolio queries:

```powershell
.\.venv\Scripts\python.exe -m sim_hedge.execution.alpha.reconcile `
  outputs\order_registry.json `
  --ledger outputs\strategy_ledger.json `
  --initialize-ledger
```

This command performs only `GET` requests. It pages through the current trading
day's orders and trades, verifies each broker order against its saved intent,
recovers an ambiguous submission by exact `client_order_id`, and applies each
confirmed `trade_id` once. It then checks that broker positions equal the Alpha
plus Beta ledgers. `safe_for_hedging` becomes true only when the account is
healthy, every Alpha intent is fully filled, no submission remains unknown, no
broker order is active, and positions reconcile. A not-ready result is still
written for diagnosis and exits with status 2. Use `--initialize-ledger` only
for this first broker replay; omit it on later runs so the confirmed ledger is
loaded and each `trade_id` remains idempotent across restarts.

If a registered Alpha order is cancelled with zero fills and you
deliberately replace it manually, record that ownership explicitly:

```powershell
.\.venv\Scripts\python.exe -m sim_hedge.execution.alpha.adopt `
  outputs\order_registry.json `
  outputs\strategy_ledger.json `
  --supersede alpha-90007076-9ce8f22e804c3d77 `
  --replacement-order O20260921C6C08EAEDB7849C1
```

This command does not place or cancel an order. It reads both broker orders and
confirmed trades, requires the original to be fully cancelled with zero fills,
and requires the replacement to be fully filled with the same account,
exchange, instrument, side, offset, type, and quantity. The different manual
limit price is retained. The original intent remains in the registry with an
explicit supersession link, and an adoption audit is written to
`outputs/alpha_adoption.json`. Never use adoption merely to force an
unexplained broker/ledger mismatch to pass.

Persist order ownership before any future submission:

```powershell
.\.venv\Scripts\python.exe -m sim_hedge.state.registry register `
  outputs\order-intent.json `
  --output outputs\order_registry.json
```

After the simulator accepts that intent, bind its returned `order_id` using a
small JSON object containing `client_order_id` and `order_id`:

```powershell
.\.venv\Scripts\python.exe -m sim_hedge.state.registry bind `
  outputs\order-binding.json `
  --registry outputs\order_registry.json `
  --output outputs\order_registry.json
```

Registration and binding are idempotent. A reused ID with different contents,
binding before registration, or a second broker order for one client ID is
rejected. This registry still performs no submission; it establishes the
ownership and retry boundary required by a later order adapter.

Replay normalized, broker-confirmed fills into the separate Alpha/Beta ledger:

```powershell
.\.venv\Scripts\python.exe -m sim_hedge.state.ledger `
  outputs\confirmed-fills.json `
  --output outputs\strategy_ledger.json
```

For later fill batches, add `--ledger outputs\strategy_ledger.json`. Each fill
must carry the broker `trade_id`, `order_id`, account, signed integer quantity,
price, execution time, and an `ALPHA` or `BETA` ownership label inherited from
the originating order. Replaying the same `trade_id` is idempotent; conflicting
contents or cross-book ownership are rejected. This command is an offline replay
boundary and does not claim that a manually authored file came from the broker.

The simulated-trading adapter reads the documented
`GET /api/trades/page` cursor endpoint and normalizes its actual
`trade_id/order_id/order_book_id/direction/trade_volume/trade_price/trade_time`
fields. The WebSocket adapter handles the same facts from `TRADE_CREATED`.
Both require a saved `order_id -> ALPHA/BETA` ownership mapping. REST page
replay ignores trades outside this project's registry, while direct ingestion
rejects an unknown order; neither assigns ownership by inference. REST remains
the authoritative backfill source and WebSocket is only the low-latency path.

Build an offline Delta/Gamma hedge decision from the same pricing request and
a broker portfolio plus a fill-confirmed Alpha/Beta strategy ledger:

```powershell
.\.venv\Scripts\python.exe -m sim_hedge.hedge_client.plan `
  outputs\live-pricing-request.json `
  outputs\portfolio_state.json `
  outputs\strategy_ledger.json `
  --target-delta 5000 --delta-limit 1000 `
  --target-gamma 0 --gamma-limit 3000 `
  --output outputs\hedge_plan.json
```

The offline command replays the recorded request through the pricing engine,
validates the broker portfolio and market observations, then calls the same
`ReferenceHedgeEngine` used by the live hedge service. Either notional
scenario-risk entry-band breach selects the bounded, cost/depth/margin-aware
D/G MILP; otherwise the stateless MSH policy selects a minimum-sufficient
inventory adjustment. Alpha instruments may
carry Beta hedges, but each resulting Beta leg is limited to 30% of its frozen
Alpha quantity. An inside-band portfolio produces a
no-trade proposal. `outputs/strategy_ledger.json` keeps fill-confirmed Alpha
and Beta positions separate. Hedge planning accepts only a `CONFIRMED` ledger,
requires `broker positions == Alpha actual positions + Beta actual positions`, and
rejects active broker orders. Alpha instruments contribute risk and may remain
in the nearest-DTE Beta universe subject to that per-leg limit. A pricing failure on a held Alpha/Beta
contract stops planning; a failure on an unheld contract removes only that
candidate and is recorded under `pricing_exclusions`. The hedge-plan file
creates no broker orders.

The hedge-plan output implements `sim-hedge/hedge-proposal/v1`. Its signed
integer quantities obey:

```text
target_beta_positions
    = confirmed_beta_positions + incremental_trades
```

The deterministic `proposal_id` binds that decision to its pricing request,
account, and `base_strategy_ledger_revision`. An execution application must
reject the proposal if either the revision or confirmed Beta base no longer
matches its ledger.

The same proposal boundary is available as a stateless local gRPC service. It
uses normalized priced Greeks and positions and has no access to market-data or
trading SDKs:

```powershell
# Terminal 1
.\.venv\Scripts\python.exe -m hedge_service.grpc_server `
  --capital 100000000 `
  --delta-entry-risk-band 30000 --delta-target-risk-band 10000 `
  --gamma-entry-risk-band 10000 --gamma-target-risk-band 10000

# Terminal 2: recorded request; no live data or broker connection
.\.venv\Scripts\python.exe -m sim_hedge.hedge_client.remote `
  protocols\hedging\v1\examples\hedge_request.json
```

The reference server routes between the bounded multi-leg Delta/Gamma solver
and stateless MSH. A future Python or C++ optimizer can implement
`protocols/hedging/v1/hedging.proto` without changing the market gateway or
execution application.

Convert that reviewed hedge plan into registered Beta order intents without
contacting the simulator:

```powershell
.\.venv\Scripts\python.exe -m sim_hedge.execution.beta.orders `
  outputs\accepted_hedge_proposal.json `
  outputs\strategy_ledger.json `
  outputs\order_registry.json `
  --exchange-id SZSE `
  --max-total-contracts 10 `
  --output outputs\beta_order_dry_run.json
```

The explicit contract limit must be chosen for the test being reviewed. The
command verifies the accepted proposal and its ledger revision, requires fully
confirmed Alpha fills, excludes every Alpha instrument,
and saves each Beta intent in the existing registry. A trade that crosses an
existing Beta position through zero is split into a `CLOSE` order followed by
an `OPEN` order. The output always has `submission_allowed: false` and performs
no REST requests. Beta execution uses only `COUNTERPARTY`, omits `limit_price`,
and delegates one-time price resolution and protection to the simulator. The
standalone pricing-request file is not an execution input.

If an unsubmitted Beta proposal becomes stale, rebuild pricing and the hedge
plan, then regenerate it explicitly with `--replace-unsubmitted`. The old
intents remain in the registry as abandoned audit history and can never be
bound or submitted:

```powershell
.\.venv\Scripts\python.exe -m sim_hedge.execution.beta.orders `
  outputs\accepted_hedge_proposal.json `
  outputs\strategy_ledger.json `
  outputs\order_registry.json `
  --exchange-id SZSE `
  --max-total-contracts 10 `
  --replace-unsubmitted `
  --output outputs\beta_order_dry_run.json
```

Submit a fresh Beta proposal with a contract cap:

```powershell
.\.venv\Scripts\python.exe -m sim_hedge.execution.beta.submit `
  outputs\beta_order_dry_run.json `
  outputs\order_registry.json `
  outputs\strategy_ledger.json `
  --max-total-contracts 10 `
  --max-snapshot-age 120
```

Unlike Alpha submission, this requires broker positions to equal the confirmed
Alpha-plus-Beta ledger rather than requiring an empty account. It also requires
healthy account/risk state, no active orders, fully confirmed Alpha ownership,
a fresh hedge source-market timestamp, exact registered requests, and no
unknown outcomes. The simulator resolves and validates the current
counterparty price. The command persists
every accepted order ID before continuing and stops on the first rejected or
unknown result.

After submission, reconcile all registered Alpha and Beta orders and fills:

```powershell
.\.venv\Scripts\python.exe -m sim_hedge.reconcile `
  outputs\order_registry.json `
  --ledger outputs\strategy_ledger.json
```

The next hedge cycle is allowed only when `safe_for_hedging`, `position_match`,
and `all_fills_complete` are all true. The older
`python -m sim_hedge.execution.alpha.reconcile` command remains as a compatible alias.
The combined report defaults to `outputs/portfolio_reconciliation.json`.

The pure `execution_engine` package models an accepted incremental proposal
without connecting to the broker. `start_execution_batch()` validates and
freezes the proposal against its base ledger revision. `assess_execution()`
then subtracts proposal-attributed confirmed fills and signed working
remainders from that frozen increment. Its `uncovered_trades` are the only
quantities eligible for a later order-building step. Re-reading the same
proposal therefore cannot duplicate a working order. Working orders belonging
to an older proposal produce `BLOCKED_BY_PRIOR_PROPOSAL` and explicit
cancellation candidate IDs, but the model never cancels or replaces them.

Every newly generated Beta `OrderIntent` stores its hedge `proposal_id` in the
order registry. `sim_hedge.state.execution_state.assess_broker_execution()` joins
that ownership with the confirmed strategy ledger and the read-only portfolio
snapshot. It refuses unknown submissions, unregistered active orders,
unreconciled partial fills, broker/ledger position differences, and Beta fills
from another proposal. Legacy registry JSON remains readable; replaying the
same old proposal does not infer ownership that was never recorded. An old
active Beta order without proposal ownership is rejected for manual review.

Freeze one reviewed live proposal against the current ledger before generating
orders. The destination is write-once: repeating the same acceptance is
idempotent, while a different proposal cannot replace it.

```powershell
.\.venv\Scripts\python.exe -m sim_hedge.state.monitor accept `
  outputs\live_risk.json `
  outputs\strategy_ledger.json `
  --output outputs\accepted_hedge_proposal.json
```

Use `outputs\accepted_hedge_proposal.json`—not the continually replaced
`live_risk.json`—as the hedge-plan argument to `sim_hedge.execution.beta.orders`.
After that batch completes, retain it as audit history and accept the next
proposal under a new filename; acceptance never overwrites a previous batch.

After reconciling broker fills, inspect the frozen batch with one read-only
portfolio request:

```powershell
.\.venv\Scripts\python.exe -m sim_hedge.state.monitor status `
  outputs\accepted_hedge_proposal.json `
  outputs\order_registry.json `
  outputs\strategy_ledger.json `
  --output outputs\execution_status.json
```

The status is `READY_TO_SUBMIT`, `WORKING`, `COMPLETE`, `NO_ACTION`, or
`BLOCKED_BY_PRIOR_PROPOSAL`. Both accepted and status files explicitly contain
`submission_allowed: false`; this command performs no submission, cancellation,
or repricing. If broker positions or traded volume are ahead of the saved
ledger, run the read-only reconciliation command first.

The complete execution lifecycle can be verified while the market is closed:

```powershell
.\.venv\Scripts\python.exe -m sim_hedge.state.replay `
  protocols\execution\v1\examples\lifecycle.json `
  --output outputs\execution_replay.json
```

The recorded frames cover an older working proposal, initial submission-ready
state, working orders, a partial fill, confirmed cancellation with an uncovered
remainder, a replacement order, and final completion. Frames contain cumulative
authoritative snapshots rather than invented broker events. The replay checks
every expected status and performs zero broker operations.

`SIM_ACCESS_TOKEN` can replace username/password while it remains valid. An ETF
option account and its linked stock/cash settlement account form one logical
portfolio; the adapter also provides the read-only settlement-account lookup,
but combining both accounts belongs in the next increment. JSON output is for
inspection/replay only. Pricing and hedging will read the in-memory state.

## Run

```powershell
python -m venv --system-site-packages .venv
.\.venv\Scripts\python -m pip install --no-deps ..\ymm_live_data_sdk-0.8.7-py3-none-any.whl
.\.venv\Scripts\python -m pip install --no-deps ..\ymm_data_sdk-0.9.16-py3-none-any.whl
$env:LIVE_TOKEN = "your-live-token"
$env:DATA_TOKEN = "your-data-token"
.\.venv\Scripts\python -m sim_hedge 159915.XSHE --mode lan

# Record one safe, complete request and replay it without live data:
.\.venv\Scripts\python -m sim_hedge --record-pricing outputs\pricing-request.json
.\.venv\Scripts\python -m pricing_engine outputs\pricing-request.json

# Use the second maturity and three strike levels on each side of ATM:
.\.venv\Scripts\python -m sim_hedge 159915.XSHE --expiry 1 --strike-wings 3

# Contract-metadata diagnostic only; does not start Live SDK:
.\.venv\Scripts\python -m sim_hedge --check-options 159915.XSHE --mode lan

.\.venv\Scripts\python -m unittest discover -s tests -v
```

## Architecture Principle

```text
                 LIVE DATA PLATFORM
                        |
                        v
              +--------------------+
              | MarketDataAdapter  |
              +--------------------+
                        |
                        v
              +--------------------+
              | Normalized State   |
              +--------------------+
                 |          |
                 |          +--------------------------+
                 |                                     |
                 v                                     v
        +--------------------+                +--------------------+
        | Pricing Engine     |                | Dashboard State    |
        +--------------------+                | Publisher          |
                 |                            +--------------------+
                 |
                 v
        +--------------------+
        | Hedge Engine       |
        +--------------------+
                 |
                 v
            OrderState
                 |
                 v
        +--------------------+
        | Order Gateway      |
        | Adapter            |
        +--------------------+
                 |
                 v
              ORDER API
                 |
       ack / fill / cancel / reject
                 |
                 v
        +--------------------+
        | Execution State /  |
        | Position Reconcile |
        +--------------------+
                 |
                 +-----------> dashboard
                 |
                 +-----------> next engine state
```

## Files

```text
sim_hedge/
├── protocols/             Versioned external contracts
├── pricing_engine/        Independent reference pricing service
├── hedge_engine/          Pure hedge calculations
├── hedge_service/         Independent hedge strategy service
├── sim_hedge/
│   ├── __main__.py        Market-process composition root
│   ├── adapters/          YMM, simulator, and gRPC boundaries
│   ├── market/            Quotes, monitoring, chain, and universe
│   ├── pricing_client/    Pricing requests, worker, and remote CLI
│   ├── hedge_client/      Hedge requests and client-side CLIs
│   ├── execution/
│   │   ├── alpha/         Alpha lifecycle
│   │   ├── beta/          Beta lifecycle
│   │   └── submission.py  Shared submission validation
│   ├── state/             Portfolio, ledger, registry, and execution state
│   ├── domain/
│   │   ├── contract.py    Option contract metadata
│   │   ├── quote.py       Normalized live quotes
│   │   ├── portfolio.py   Normalized account and portfolio snapshots
│   │   └── pricing.py     Transport-neutral pricing results
│   ├── auto_trader.py     Trading composition root
│   └── ports.py           Application interfaces
├── tests/
│   ├── test_market_state.py
│   ├── test_market_monitor.py
│   ├── test_option_chain.py
│   ├── test_pricing_protocol_examples.py
│   ├── test_pricing_worker.py
│   ├── test_strategy_universe.py
│   ├── test_ymm_reference.py
│   └── test_ymm_live.py
├── .gitignore
├── pyproject.toml
└── README.md
```
