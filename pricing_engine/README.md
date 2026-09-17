# Pricing Engine

This directory will contain the independent reference pricing service. It must
implement `protocols/pricing/../pricing.proto` and must **NOT** import a market-data
or broker SDK.

The first implementation will be Python and correctness-focused. A future C++
implementation will use the same protocol and the same contract test vectors,
so the market gateway will not need to change when the engine is replaced.

The reference implementation uses SABR to fit a volatility smile and Black-76
to convert volatility into option prices. It intentionally uses only Python's
standard library. Its deterministic coarse-to-fine calibration is educational,
not a claim of production calibration precision.

Run it entirely offline against the included request:

```powershell
.\.venv\Scripts\python.exe -m pricing_engine
```

Copy and edit `examples/sabr_request.json` to test different spot prices,
market option prices, rates, time to expiry, or fixed beta values. No market SDK,
token, network connection, system clock, or trading account is involved.

Run the focused verification:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_pricing_engine -v
```

The future gRPC service will translate protocol messages into these pure input
types. It will not contain the model mathematics itself.
