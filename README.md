# sim_hedge

`sim_hedge` is for simulating trade, able to do actual trading.

The market-data boundary subscribes to `ymm_live_data_sdk` tick channels and
converts vendor dictionaries into internal `MarketQuote` values. The SDK
callback only places batches into a bounded queue; normalization and application
work happen on a separate consumer thread. A thread-safe `MarketState` keeps one
latest quote per instrument and reports whether all required quotes are fresh.

## Run

```powershell
python -m venv --system-site-packages .venv
.\.venv\Scripts\python -m pip install --no-deps ..\ymm_live_data_sdk-0.8.7-py3-none-any.whl
$env:LIVE_TOKEN = "your-live-token"
.\.venv\Scripts\python -m sim_hedge 159915.XSHE --mode lan

.\.venv\Scripts\python -m pip install --no-deps ..\ymm_data_sdk-0.9.16-py3-none-any.whl
$env:DATA_TOKEN = "your-data-token"
.\.venv\Scripts\python -m sim_hedge --option-chain 159915.XSHE --mode lan

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
├── sim_hedge/
│   ├── __init__.py       Public package information
│   ├── __main__.py       Command-line entry point
│   ├── adapters/
│   │   ├── ymm_live.py        Live SDK connection and translation boundary
│   │   └── ymm_reference.py   Data SDK contract-metadata boundary
│   ├── domain.py         Domain values only
│   ├── market_state.py   Latest quotes and market readiness
│   ├── option_chain.py   Option-chain inspection logic
│   └── ports.py          Interfaces required by the application
├── tests/
│   ├── test_market_state.py
│   ├── test_option_chain.py
│   ├── test_ymm_reference.py
│   └── test_ymm_live.py
├── .gitignore
├── pyproject.toml
└── README.md
```
