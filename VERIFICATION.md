# Verification of the reliable paper demo

## Completed checks

- `python3 -m unittest discover -s tests -v`: **24 passed**.
- A separate two-process integration check started the actual dashboard server with a deterministic paper feed, bought in the first process, sent SIGTERM, restarted from the same data directory, sold in the second process, and verified two saved ledger entries and fee-inclusive accounting. Both processes exited cleanly.
- `render.production.yaml` passed the current Render JSON Schema validation.

The tests cover strategy helpers; a paper buy and protective sell; closed candles; rejection of stale prices; account/ledger retention on restart; fee-inclusive realized results; corrupt state; atomic replacement failure; refusal of a missing required persistent mount; market failure and recovery; dashboard responsiveness and pause during an in-flight market request; copy-safe snapshots; graceful stop; migration of the old fee basis; password protection of status/export; immediate pause/resume and retained pause; CSRF and origin rejection; engine-liveness health checks; and a visible warning about temporary storage.

The deterministic feed is a test fixture. Its trades are not inserted into the hosted account and are not presented as real market performance.

## Pending checks

The always-on paid plan and persistent disk are not activated. The post-activation hosted restart and unattended-operation checks are described in `OPERATIONS_BG.md`. Local restart tests do not prove that Render has a persistent disk attached.

No live financial order was used for these checks. No profitability claim or historical strategy-performance claim is made.
