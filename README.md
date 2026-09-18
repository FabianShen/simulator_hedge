# sim_hedge

`sim_hedge` is for simulating trade, able to do actual trading.

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
│   ├── strategy_universe.py  Pricing-universe selection
│   └── ports.py          Interfaces required by the application
├── tests/
│   ├── test_market_state.py
│   ├── test_market_monitor.py
│   ├── test_option_chain.py
│   ├── test_pricing_protocol_examples.py
│   ├── test_strategy_universe.py
│   ├── test_ymm_reference.py
│   └── test_ymm_live.py
├── .gitignore
├── pyproject.toml
└── README.md
```
