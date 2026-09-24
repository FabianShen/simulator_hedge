# Offline execution replay v1

An execution replay is a deterministic test input. It contains one valid hedge
proposal and a sequence of independent, cumulative execution snapshots. Each
frame records all confirmed fills and currently working order remainders known
at that point; frames are not incremental broker events.

Every frame declares its expected status and, optionally, its expected
uncovered trades. Replay fails immediately when the execution model produces a
different result. This verifies partial fills, cancellations, replacements,
completion, and older-proposal blocking without market data or broker access.

The format intentionally contains no credentials, prices, REST endpoints, or
permission to trade. `broker_operations_performed` is always zero.

Run the included scenario:

```powershell
.\.venv\Scripts\python.exe -m sim_hedge.state.replay `
  protocols\execution\v1\examples\lifecycle.json `
  --output outputs\execution_replay.json
```
