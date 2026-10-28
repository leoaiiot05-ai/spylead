# TODO (deferred)

Nothing here is in scope for v1. Do not build any of it without being asked. Append new deferred items under the matching heading, one line each, with the reason or the seam it would use.

## Data

- [ ] Full history for the ICE BofA spreads (CCC, high yield), which FRED now keeps for 3 years only. Needs another source or a CSV import. Seam: a new client in `sources.py`.
- [ ] ISM PMI and the Conference Board LEI, which have no free source. Needs a CSV import with release days.
- [ ] SPY total return (dividends reinvested) as an option. Today: raw close.
- [ ] Scheduled automatic fetches. Today: the [Fetch Data] button only.
- [ ] Export of series and results as CSV.
- [ ] Use revised (latest-vintage) values as an alternative to first releases.

## Analysis

- [ ] Projection past today: the composite extended by the shortest lead, as an implied SPYL1.
- [ ] Per-candidate F_L1 charts at the best A, and score-against-A curves.
- [ ] Automatic choice of the best subset of LEADL1 series for F.
- [ ] Weekly instead of monthly refits for the one-sided series, to react faster.
- [ ] Cache the one-sided anchors if compute time grows. Today a full compute fits within the budget.
- [ ] A significance test for leads (for example a block bootstrap), beyond the out-of-sample score.

## Interface

- [ ] Offline mode (service worker).
- [ ] Native iOS or Windows apps. The JSON API is the seam.
- [ ] A notification when a fetch finds new data.

## Operations

- [ ] Daily backup of `data/spylead.db`.
- [ ] Run as a Windows service instead of a logon task.
- [ ] Report every `config.toml` and `catalog.toml` error at once; startup stops at the first today. Seam: `config.check_table`.
- [ ] Show config and catalog errors under `pythonw.exe` (autostart): they are printed before logging starts, and pythonw has no console. Seam: `__main__.main`.
