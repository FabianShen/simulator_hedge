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
