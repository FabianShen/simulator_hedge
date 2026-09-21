# Hedge proposal contract v1

This JSON contract is the boundary between a hedge decision engine and an order
execution application. It describes a decision; it never grants permission to
trade and contains no order type, limit price, retry, or cancellation policy.

## Position equation

Every proposal is based on one immutable, broker-confirmed strategy ledger:

```text
target_beta_positions
    = confirmed_beta_positions + incremental_trades
```

All quantities are signed integer contracts. Positive means long/buy and
negative means short/sell. Zero positions are omitted.

`base_strategy_ledger_revision` and `confirmed_beta_positions` must both match
the executor's current confirmed ledger. A mismatch makes the proposal stale,
even when its market data is otherwise fresh.

## Identity and replay

`proposal_id` is a deterministic hash of the protocol version, pricing request,
account, base ledger revision, decision-engine identity, confirmed Beta
positions, incremental trades, and target Beta positions. Identical decisions
have the same ID. Changing any of those inputs produces a different ID. The
engine name and version let the simple implementation be replaced later without
changing the position semantics of this contract.

An executor must track its working and filled quantities by proposal and client
order ID. Re-reading the same proposal must not create duplicate orders.

## Ownership

- The hedge engine owns risk calculations and the incremental proposal.
- The strategy ledger owns broker-confirmed Alpha and Beta positions.
- The execution application owns working orders, partial fills, cancellation,
  replacement prices, and progress toward the target.
- Only reconciled broker fills may change confirmed Beta.

The additional risk diagnostics emitted by the reference implementation are
informative extensions. Consumers must use the v1 identity and position fields
above as the execution-neutral contract.
