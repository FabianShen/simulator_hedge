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

Options with a market price calibrate the smile. Options without one are
valuation-only and still receive model prices and Greeks. This lets the
gateway value confirmed holdings with no bid/ask without putting illiquid or
stale prices into the fit.

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

The gRPC service translates protocol messages into these pure input types. It
does not contain the model mathematics itself.

## Local gRPC service

Install the project and development compiler once:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

Start the pricing service:

```powershell
.\.venv\Scripts\python.exe -m pricing_engine.grpc_server
```

In another terminal, send any recorded protocol request:

```powershell
.\.venv\Scripts\python.exe -m sim_hedge.price_remote `
  protocols\pricing\v1\examples\price_request.json
```

The default service address is `127.0.0.1:50051`; it is local-only and uses an
unencrypted channel. Remote deployment will require TLS and authentication.
