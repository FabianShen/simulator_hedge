# sim_hedge

`sim_hedge` is for simulating trade, able to do actual trading.

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
│   ├── __main__.py       Executable example
│   ├── domain.py         Domain values only
│   └── market.py         Deterministic market behavior
├── tests/
│   └── test_market.py
├── .gitignore
├── pyproject.toml
└── README.md
```

## First commit

After reading and running the code:

```powershell
git status
git add .
git commit -m "build: start deterministic market simulator"
```

