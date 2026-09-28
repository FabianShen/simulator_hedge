# Hedge proposal contract v1

`hedging.proto` is the source of truth for the stateless gRPC boundary between
the market/risk application and a hedge-decision engine. The response also
implements the JSON proposal contract consumed by an order execution
application. It describes a decision; it never grants permission to trade and
contains no order type, limit price, retry, or cancellation policy.

## Service boundary

`HedgeService.Propose` is unary: one complete normalized snapshot produces one
proposal. The request contains priced unit Greeks, option metadata, confirmed
Alpha/Beta positions, the hedge universe, account identity, and ledger revision.
It contains no vendor payloads, credentials, broker orders, or pending fills.

The service is intentionally stateless. Retrying the same request does not
advance any internal portfolio. Only the caller's broker-confirmed ledger can
change the position base for a later request.

The reference server's capital, scenario shocks, and Delta/Gamma risk bands are
process configuration, not request fields. Defaults are capital 100,000,000;
Delta entry/target bands 30,000/10,000; and Gamma entry/target bands
10,000/10,000. Either entry-band breach routes the request to the cost-,
displayed-depth-, and margin-aware D/G MILP. If neither is breached, the
stateless minimum-sufficient-hedge (MSH) policy runs instead. Operators can
override these settings with `--capital`, `--delta-entry-risk-band`,
`--delta-target-risk-band`, `--gamma-entry-risk-band`, and
`--gamma-target-risk-band`.

New requests identify this automatic routing policy as
`HEDGE_MODEL_SCENARIO_ROUTED`. The former
`HEDGE_MODEL_SIMPLE_DELTA_GAMMA_PAIR` enum spelling remains a deprecated alias
for wire and JSON compatibility; it selects the same policy.

## Position equation

Every proposal is based on one immutable, broker-confirmed strategy ledger:

```text
target_beta_positions
    = confirmed_beta_positions + incremental_trades
```

All quantities are signed integer contracts. Positive means long/buy and
negative means short/sell. Zero positions are omitted.

Alpha and Beta are independent books and may contain the same instrument.
Alpha remains the frozen strategy baseline; any hedge on an Alpha instrument
is recorded in Beta, and the reference engine limits the absolute Beta quantity
on each such leg to 30% of that leg's Alpha quantity.

The executable signal is `incremental_trades`. `target_beta_positions` is a
derived audit value that proves which confirmed ledger the increment was based
on; an execution application must not treat it as a continuously changing
absolute target. Once accepted, each incremental proposal is an immutable
execution batch.

`base_strategy_ledger_revision` and `confirmed_beta_positions` must both match
the executor's current confirmed ledger. A mismatch makes the proposal stale,
even when its market data is otherwise fresh.

`source_market_as_of` is the timestamp of the exact market snapshot used by the
pricing request. Consumers must use it, rather than the later proposal creation
time, when enforcing execution freshness.

## Identity and replay

`proposal_id` is a deterministic hash of the protocol version, pricing request,
account, base ledger revision, decision-engine identity, confirmed Beta
positions, incremental trades, and target Beta positions. Identical decisions
have the same ID. Changing any of those inputs produces a different ID. The
engine name and version let the simple implementation be replaced later without
changing the position semantics of this contract.

An executor must track its working and filled quantities by proposal and client
order ID. Re-reading the same proposal must not create duplicate orders.
Proposal ownership must be persisted with each Beta intent before submission;
it must not be inferred later from an order ID or instrument.

## Ownership

- The hedge engine owns risk calculations and the incremental proposal.
- The strategy ledger owns broker-confirmed Alpha and Beta positions.
- The execution application owns working orders, partial fills, cancellation,
  replacement prices, and progress through one accepted increment.
- Only reconciled broker fills may change confirmed Beta.

The additional diagnostics emitted by the reference implementation are
informative extensions. `hedge_pair` is retained as a legacy field name and
contains every instrument with a non-zero incremental trade. `decision_policy`
identifies the selected path as `D_G_MILP` or `MSH`. `gamma_improvement` is the
reduction in absolute Gamma scenario risk (before minus after); a negative
value means Gamma risk increased. `execution_diagnostics` reports an estimated
transaction cost per proposal and per leg, the side-specific displayed size
and depth cap used for each leg, short-margin estimates before/after and the
configured limit, plus each traded instrument's estimated short margin per
contract. MSH caps orders to the configured fraction of displayed size (50% by
default); D/G uses displayed size directly. Cost is a model estimate
(half-spread plus configured fee), displayed size is not a fill guarantee, and
margin is estimated from current mid-premium and spot. These values are
diagnostics, not broker execution instructions.
Consumers must use the v1 identity and position fields above as the
execution-neutral contract.

## Recorded replay

Start the reference Python server and replay the example from another terminal:

```powershell
.\.venv\Scripts\python.exe -m hedge_service.grpc_server `
  --capital 100000000 `
  --delta-entry-risk-band 100 --delta-target-risk-band 10 `
  --gamma-entry-risk-band 5 --gamma-target-risk-band 2

.\.venv\Scripts\python.exe -m sim_hedge.hedge_client.remote `
  protocols\hedging\v1\examples\hedge_request.json
```

The server binds to `127.0.0.1:50052` by default. The request and response JSON
files under `examples/` use standard protobuf JSON names. Signed 64-bit contract
quantities are strings in protobuf JSON, while the Python client converts them
back to integers in the execution-neutral proposal dictionary.

## Generate Python bindings

```powershell
.\.venv\Scripts\python.exe -m grpc_tools.protoc `
  -I protocols `
  --python_out=. `
  --grpc_python_out=. `
  protocols\hedging\v1\hedging.proto
```
