# Pricing protocol

`pricing.proto` is the source of truth for communication between the market
gateway and any pricing-engine implementation. 

The engine may run locally or
remotely and may be written in Python, C++, or another language supported by
Protocol Buffers.

## Boundary

The gateway is responsible for:

- Converting vendor data into normalized inputs;
- Selecting a deterministic option market price, such as bid/ask midpoint;
- Rejecting stale or unsafe market data ;

The pricing engine is responsible for:

- Validating numerical inputs;
- Converting saved market prices to Black-76 IV;
- Calibrating the SABR smile and reporting its fit error;
- Calculating theoretical values and unit Greeks from the fitted smile;
- Returning one status per option.

## Units and conventions

- All timestamps are UTC values.
- Rates and volatility are annualized decimals (`0.20` means 20%).
- `ACT/365 Fixed` means elapsed seconds divided by `365 * 86400`.
- Delta and gamma are per one underlying unit.
- Theta is per year.
- Vega and rho are for a `+1.0` absolute change. Divide by 100 for a one
  percentage-point change.
- Prices use the instrument's quoted currency and price unit.

## Compatibility rules

- Never renumber or reuse an existing field or enum value.
- Add backward-compatible fields with new numbers.
- Put breaking changes in a new package such as `sim_hedge.pricing.v2`.
- Preserve `request_id` when retrying a request.
- Do not include API tokens, account data, positions, or raw vendor payloads.

The JSON files under `examples/` use the standard protobuf JSON field names and
are intended for documentation, replay fixtures, and cross-language contract
tests.
