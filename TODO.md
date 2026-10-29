# TODO (deferred)

Nothing here is in scope for v1. Do not build any of it without being asked. Append new deferred items under the matching heading, one line each, with the reason or the seam it would use.

## Data

- [ ] Full history for the ICE BofA spreads (CCC, high yield), which FRED now keeps for 3 years only. Needs another source or a CSV import. Seam: a new client in `sources.py`.
- [ ] ISM PMI and the Conference Board LEI, which have no free source. Needs a CSV import with release days.
- [ ] SPY total return (dividends reinvested) as an option. Today: raw close.
- [ ] Scheduled automatic fetches. Today: the [Fetch Data] button only.
- [ ] Export of series and results as CSV.
- [ ] Use revised (latest-vintage) values as an alternative to first releases.
- [ ] Re-date only the observations released on the archive's first vintage day, the series' earliest release day. §5.2 also re-dates a first release that came more than typical_lag + 60 days late inside the archive, to a day before it was published. Seam: `pit.estimate_release_days`.

## Analysis

- [ ] Projection past today: the composite extended by the shortest lead, as an implied SPYL1.
- [ ] Per-candidate F_L1 charts at the best A, and score-against-A curves.
- [ ] Automatic choice of the best subset of LEADL1 series for F.
- [ ] Weekly instead of monthly refits for the one-sided series, to react faster.
- [ ] Cache the one-sided anchors if compute time grows. Today a full compute fits within the budget.
- [ ] Carry IRLS weights across refits (the previous refit's weights, 1 for new points) if the bench shows the one-sided refits dominate the compute. Today each refit's search starts from unit weights. Seam: `smoothing.refit_anchors`.
- [ ] A significance test for leads (for example a block bootstrap), beyond the out-of-sample score.
- [ ] Guard steps 3 and 5 against singular normal equations: an indicator that is constant over its window, or two members that are affine copies of each other. Today `numpy.linalg.solve` raises LinAlgError, or the fit is meaningless. Seam: `analysis.lad`.

## Interface

- [ ] Offline mode (service worker).
- [ ] Native iOS or Windows apps. The JSON API is the seam.
- [ ] A notification when a fetch finds new data.
- [ ] Browser tests of `app.js` (for example Playwright), which need a dev dependency. Today pytest checks the static files, and the UI checklist the behaviour.

## Operations

- [ ] Daily backup of `data/spylead.db`.
- [ ] Run as a Windows service instead of a logon task.
- [ ] Report every `config.toml` and `catalog.toml` error at once; startup stops at the first today. Seam: `config.check_table`.
- [ ] Show config and catalog errors under `pythonw.exe` (autostart): they are printed before logging starts, and pythonw has no console. Seam: `__main__.main`.
- [ ] Stop an autostarted spylead gracefully, with the shutdown of §15, instead of ending `pythonw.exe` in Task Manager: it has no console for Ctrl+C. Seam: `__main__.serve`'s `until`.
