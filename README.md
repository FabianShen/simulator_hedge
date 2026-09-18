# sim_hedge

`sim_hedge` is for simulating trade, able to do actual trading.

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
pricing is allowed to run.
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
.\.venv\Scripts\python.exe -m sim_hedge.price_remote `
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
pricing is disabled when `--pricing-target` is omitted.

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

.\.venv\Scripts\python.exe -m sim_hedge.portfolio_remote --list-accounts
$env:SIM_ACCOUNT_ID = "your-etf-option-account-id"
.\.venv\Scripts\python.exe -m sim_hedge.portfolio_remote `
  --output outputs\portfolio_state.json
```

To verify WebSocket recovery without processing incremental events yet:

```powershell
$env:SIM_WS_URL = "ws://your-simulator:8001/ws/trading"
.\.venv\Scripts\python.exe -m sim_hedge.portfolio_remote `
  --output outputs\portfolio_state.json
```

This loads REST revision 1, requires the documented first WebSocket business
event to be `SNAPSHOT`, replaces the state as revision 2, and exits. Individual
incremental events are intentionally not applied until their real payloads have
been observed.

After snapshot bootstrap has been verified, keep the position stream running:

```powershell
.\.venv\Scripts\python.exe -m sim_hedge.portfolio_remote --watch `
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
initial cash as a premium-equivalent budget:

```text
one basket = sum(reference price * contract multiplier) for every selected leg
premium capacity = floor(30% * initial cash / one basket)
contracts per option = min(premium capacity, explicit test cap)
```

This is an offline sizing rule, not a short-option margin calculation, and it
does not submit orders or modify the actual `PortfolioState`.

Build an inspectable Alpha plan entirely from recorded inputs:

```powershell
.\.venv\Scripts\python.exe -m sim_hedge.alpha_plan `
  outputs\live-pricing-request.json `
  outputs\portfolio_state.json `
  --output outputs\alpha_plan.json
```

The output explicitly records `PREMIUM_EQUIVALENT_NOT_MARGIN` and
`orders_generated: false`; it is an offline target, not permission to trade.
The command defaults to `--max-contracts-per-option 1` and records both the
uncapped premium capacity and the applied cap.

Generate registered Alpha order intents from saved data without submitting:

```powershell
.\.venv\Scripts\python.exe -m sim_hedge.alpha_orders `
  outputs\live-pricing-request.json `
  outputs\alpha_plan.json `
  --exchange-id SZSE `
  --output outputs\alpha_order_dry_run.json `
  --registry-output outputs\order_registry.json
```

The saved mid is rounded to the nearest valid price tick for inspection only.
The output explicitly says `submission_allowed: false` and `orders_submitted: 0`;
execution must rebuild or validate prices against a fresh live market snapshot.
For this workflow, record a fresh snapshot with
`--record-pricing PATH --stop-after-recording`, then immediately rebuild the
Alpha plan and dry-run.

The following command performs real simulated-account submissions. Run it
yourself only after regenerating and reviewing the three input files. The value
of `--confirm-submit` must exactly match the account ID:

```powershell
.\.venv\Scripts\python.exe -m sim_hedge.alpha_submit `
  outputs\live-pricing-request.json `
  outputs\alpha_order_dry_run.json `
  outputs\order_registry.json `
  --confirm-submit ETO202609151523232103 `
  --max-total-contracts 4
```

Before the first POST it independently checks that the pricing snapshot is at
most 10 seconds old, the account and risk states are `NORMAL`, and the broker
has no positions or active orders. It submits sequentially, saves each returned
`order_id` before continuing, and stops on the first rejection. A timeout,
connection loss, `5xx`, invalid response, or missing `order_id` is recorded as
`SUBMISSION_UNKNOWN`; that client ID cannot be submitted again until the order
is recovered through a read-only query and bound to the registry.

After submission, recover order IDs and rebuild the Alpha ledger only from the
simulator's authoritative order, trade, and portfolio queries:

```powershell
.\.venv\Scripts\python.exe -m sim_hedge.alpha_reconcile `
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

Persist order ownership before any future submission:

```powershell
.\.venv\Scripts\python.exe -m sim_hedge.order_registry register `
  outputs\order-intent.json `
  --output outputs\order_registry.json
```

After the simulator accepts that intent, bind its returned `order_id` using a
small JSON object containing `client_order_id` and `order_id`:

```powershell
.\.venv\Scripts\python.exe -m sim_hedge.order_registry bind `
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
.\.venv\Scripts\python.exe -m sim_hedge.strategy_ledger `
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
.\.venv\Scripts\python.exe -m sim_hedge.hedge_plan `
  outputs\live-pricing-request.json `
  outputs\portfolio_state.json `
  outputs\strategy_ledger.json `
  --output outputs\hedge_plan.json
```

The pure `hedge_engine` aggregates contract-multiplied Alpha Greeks, selects the
nearest non-Alpha call/put pair, and solves for continuous incremental trades
that neutralize current Delta and Gamma. It then evaluates the four surrounding
integer combinations and keeps the one with the smallest normalized Delta/Gamma
residual. `outputs/strategy_ledger.json` keeps fill-confirmed Alpha and Beta
positions separate. Hedge planning accepts only a `CONFIRMED` ledger, requires
`broker positions == Alpha actual positions + Beta actual positions`, and
rejects active broker orders. Alpha instruments contribute risk but are removed
from the nearest-DTE Beta universe. The hedge-plan file creates no broker orders.

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
├── protocols/
│   └── pricing/v1/      Versioned external pricing contract
├── pricing_engine/         Independent reference-engine project
├── sim_hedge/
│   ├── __init__.py       Public package information
│   ├── __main__.py       Command-line entry point
│   ├── adapters/
│   │   ├── ymm_live.py        Live SDK connection and translation boundary
│   │   └── ymm_reference.py   Data SDK contract-metadata boundary
│   ├── domain.py         Domain values only
│   ├── market_monitor.py Diagnostic status and atomic JSON projection
│   ├── market_state.py   Latest quotes and market readiness
│   ├── option_chain.py   Option-chain inspection logic
│   ├── pricing_worker.py Non-blocking continuous pricing coordinator
│   ├── strategy_universe.py  Pricing-universe selection
│   └── ports.py          Interfaces required by the application
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
