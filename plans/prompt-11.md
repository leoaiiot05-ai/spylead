# Prompt 11: Final audit

The whole report is in this file. No other file changed (CLOUD.md).

## Steps

1. Read SPEC.md in full, every module of `spylead/`, the static UI, `scripts/`, `run.bat`, and every test file.
2. Mapped each acceptance criterion of §16.2 to the tests that prove it (section 1).
3. Compared the code with the spec, section by section, and checked every decision recorded in `plans/prompt-01.md` to `plans/prompt-10.md` against the code (section 2).
4. Traced every input of the analysis back to the day it was released (section 3), and every use of the token, the session value and the two keys (section 4).
5. Scanned for dead code, unused dependencies and the rules of CLAUDE.md (section 5). The scans parsed the code, and their scripts stayed in the scratch directory.
6. Confirmed the two new findings of section 2.1 with scratch tests against the test fakes. They were not committed.
7. Ran the suite: `.venv/bin/python -m pytest -q` (Linux VM, Python 3.13, the locked package versions), 707 passed in about 41 s.

## 1. Acceptance criteria

Test files are named without `tests/`. "PC" marks what only the owner's PC can show (§16.3).

| AC | Proven by | Manual step, or gap |
|---|---|---|
| AC-1 | `test_smoothing.py`: `test_weighted_solve_matches_make_smoothing_spline` (3 seeds × 3 λ, uneven spacing, random weights; g, a 2,001-point grid and γ within 1e-6), `test_value_formula_is_continuous_at_the_knots`, `test_linear_extension_continues_the_end_values_with_the_end_slopes` | — |
| AC-2 | `test_smoothing.py`: `test_irls_never_increases_the_objective`, `test_a_spike_moves_the_robust_fit_less_than_the_l2_fit`, `test_inflection_count`, `test_search_returns_at_most_t_inflections_and_more_just_below`, `test_a_warm_start_gives_the_lambda_of_the_full_bracket`, and the two limit tests | — (the check at λ · 10^-0.005 carries the search's weights: `plans/prompt-02.md`) |
| AC-3 | `test_smoothing.py`: `test_no_value_depends_on_points_released_after_the_last_month_end_before_it`, `test_values_follow_the_anchor_line`, `test_no_value_before_the_first_anchor`, `test_each_anchor_is_the_end_value_and_end_slope_of_its_refit` | — |
| AC-4 | `test_pit.py`: the FRED and Tiingo parsing tests, the five `typical_lag` tests, `test_pre_archive_observations_get_estimated_release_days_and_the_flag`, the three known-value tests, `test_points_strictly_increase_and_hold_the_newest_observation_known`, `test_a_log_series_with_a_value_that_is_not_positive_has_invalid_values` | — |
| AC-5 | `test_analysis.py`: `test_an_indicator_equal_to_spy_90_days_later_leads_by_exactly_90_days` (LOG and LEVEL), `test_one_window_serves_every_shift`, `test_ties_go_to_the_smaller_absolute_value`, `test_step_4`, `test_a_3_year_series_has_insufficient_history`, `test_a_window_needs_10_years_of_days` | — |
| AC-6 | `test_analysis.py`: `test_step5_recovers_every_lead_exactly` (periods of 1,500 to 3,100 days), `test_two_runs_are_identical`, `test_every_candidate_lies_within_minus_730_and_0`; a = 0 by construction (SPY is never shifted) | — |
| AC-7 | `test_analysis.py`: `test_lad_matches_linprog_on_a_small_case`, `test_lad_reaches_the_linprog_optimum_given_enough_solves`, `test_score_is_the_mean_absolute_deviation_from_the_median`, `test_slope_is_in_percent_per_year`, `test_reported_f_and_composite_follow_the_formulas` | — (§7.1's stop rules miss the LAD optimum on many small cases: "For the owner" in `plans/prompt-04.md`) |
| AC-8 | `test_analysis.py`: `test_the_split_day_starts_the_last_30_percent`, `test_step3_out_of_sample_fits_on_the_first_70_percent_only`, `test_step5_out_of_sample_fits_on_the_first_70_percent_only`, `test_the_out_of_sample_check_changes_nothing_else` | — |
| AC-9 | `test_jobs.py`: `test_the_first_fetch_downloads_everything_from_fetch_start_and_computes`, `test_the_start_days_are_set_once`; `test_web.py`: the chart tests | **Partial gap**: the chart routes are tested on results built with `jobs.payload_of`, not on what a fetch saved; no test joins the two. PC: §16.3 step 2 |
| AC-10 | `test_jobs.py`: `test_a_fetch_with_everything_up_to_date_makes_metadata_requests_only_and_no_compute` | — |
| AC-11 | `test_jobs.py`: `test_an_incremental_fetch_inserts_only_new_days_and_computes`, `test_the_typical_lag_of_a_later_fetch_counts_the_stored_observations`; `test_store.py`: `test_inserting_an_existing_observation_day_leaves_the_stored_row_unchanged` | — |
| AC-12 | `test_jobs.py`: `test_a_failing_series_keeps_its_data_and_records_its_error_while_the_others_continue`, `test_spy_without_data_ends_no_spy_data_without_a_compute`, `test_an_empty_key_fails_every_series_of_its_source_with_missing_key`; `test_sources.py`: the retry, 429, 4xx, pacing and missing-key tests, with an injected sleep | — |
| AC-13 | `test_jobs.py`: `test_a_second_job_is_refused_while_one_runs`, `test_progress_runs_through_every_phase`, both shutdown tests, `test_refit_f_reruns_step_5_alone_from_the_stored_curves`; `test_web.py`: `test_a_job_request_while_a_job_runs_gets_409` | — |
| AC-14 | `test_jobs.py`: `test_every_status_arises_from_the_data_and_the_fetch` (all eight, with a stand-in for step 3), `test_f_has_no_members_when_nothing_is_kept_or_every_member_is_unchecked`, `test_the_payload_keeps_f_s_status_as_step_5_gives_it`; `test_analysis.py`: `test_status_is_the_first_that_applies`, `test_a_stale_series_is_discontinued_and_never_in_f`, `test_no_members_give_no_f`, `test_members_that_overlap_less_than_10_years_give_no_f` | — (INSUFFICIENT_OVERLAP cannot arise from real data, so its scenario is at the analysis level: `plans/prompt-07.md`) |
| AC-15 | `test_jobs.py`: `test_an_added_series_is_stored_fetched_and_computed_with_the_form_of_section_4_4`, `test_an_annual_or_unknown_series_is_refused`, `test_a_catalog_series_is_not_removed`, `test_a_series_that_left_the_catalog_leaves_the_next_compute`; `test_web.py`: the FRED ID and removal tests; `test_store.py`: `test_the_catalog_sync_deletes_rows_that_left_the_catalog_with_their_data`; `test_pit.py`: `test_a_user_series_is_level_for_percent_units_or_a_value_that_is_not_positive` | — |
| AC-16 | `test_jobs.py`: `test_an_unchecked_member_leaves_f_and_stays_out_after_a_restart`, `test_a_newly_kept_series_joins_f`; `test_web.py`: `test_unchecking_kept_series_refits_f`; `test_store.py`: `test_the_exclusions_survive_close_and_reopen` | — |
| AC-17 | `test_web.py`: the session, 415, INVALID_JSON, finite-number, header, cookie, constant-time and secret-sweep tests; `test_main.py`: `test_serve_listens_on_127_0_0_1_only_and_shuts_down_in_order`, `test_ctrl_c_stops_cleanly_and_a_failure_is_logged_redacted`; `test_sources.py`: `test_no_key_reaches_an_error_text_or_a_log_line`, `test_the_logged_fred_url_is_redacted`; `test_jobs.py`: `test_an_unexpected_error_ends_the_job_failed_with_a_redacted_message` | — |
| AC-18 | `test_web.py`: `test_the_spy_chart`, both series-chart tests, `test_the_f_chart`, the NO_DATA, NOT_FOUND and NO_F tests, `test_values_are_rounded_to_6_significant_digits_and_nan_becomes_null` | — |
| AC-19 | `test_static.py` (45 tests); `test_web.py`: `test_static_files_are_served_with_their_content_types` | PC: the UI checklist of `plans/prompt-09.md` (§16.3 step 4) |
| AC-20 | `test_config.py`: `test_errors_name_the_key_and_echo_no_value`, `test_token_of_16_characters_is_accepted`; `test_catalog.py`: `test_errors_name_the_entry_and_echo_no_value`, `test_committed_catalog_matches_the_spec_table`; `test_main.py`: the placeholder subprocess test and both "before logging" tests | — |
| AC-21 | `test_main.py`: `test_a_second_process_exits_before_opening_the_database_or_the_port`, `test_a_lock_left_by_a_killed_process_does_not_block_a_restart`, both shutdown-order tests, `test_pool_workers_leave_ctrl_c_to_the_main_process`; `test_store.py`: `test_the_wal_fallback_uses_the_delete_journal_with_a_warning` | PC: the `msvcrt.locking` branch runs only in the suite on Windows (§16.3 step 1); WAL on the real drive (§16.3 step 2) |
| AC-22 | `test_compute.py`: `test_two_computes_on_the_same_data_give_identical_results` (the real numerics at a reduced scale); `test_analysis.py`: `test_two_runs_are_identical`; `test_bench.py`: `test_a_small_bench_compute_times_every_phase`, `test_the_report_names_every_phase_and_the_budget` | PC: `python -m spylead.bench`, under 10 minutes (§16.3 step 3) |

Never run here, and checked only as text: `scripts/install-autostart.ps1` and `run.bat` (there is no PowerShell on the VM). Their first run is §16.3 step 5.

## 2. Where the code differs from SPEC.md

### 2.1 New findings

Neither contradicts a rule of SPEC.md: the code follows the spec's letter. Each leaves the results out of date, and the spec says nothing about the case. Both were confirmed by scratch tests against the test fakes.

1. **A compute that never finished is not redone** (§8.2 against §9). Ambiguity.
   - A fetch downloads new data, then a shutdown, or an error, ends its compute. The previous results stay (§9).
   - The next Fetch Data finds every series up to date and ends UP_TO_DATE, with no compute (§8.2). The downloaded data does not reach the results until new data arrives or N changes.
   - The scratch test interrupted the compute of an incremental fetch: the next fetch said "Every series is up to date", and the results were unchanged.
   - Proposal for §8.2: "When every series is up to date and the last compute has seen every stored observation: UP_TO_DATE. Otherwise, compute."
2. **A catalog edit leaves the stored results as they were** (§4.4, §10 against §7.4, §7.6). Ambiguity, with a visible inconsistency.
   - A series removed from `catalog.toml` leaves the database at the next start. F, though, keeps it as a member, with its b and β, until the next compute or refit. An up-to-date Fetch Data runs neither.
   - The scratch test removed a member and restarted: the next fetch said up to date, and F still listed the removed series, whose curve was gone.
   - A changed `fred_id` drops the old data (prompt 5). The state document then shows the old series' status and step-3 report for the new series, though no data is stored, where §7.6 gives PENDING. The next Fetch Data downloads the new series and computes, which ends it.
   - Today's remedy: uncheck and recheck any "in F" box (REFIT_F reads the current candidates).
   - Proposal: refit F at startup when the sync removed a member, or count a catalog change as "not up to date".

### 2.2 Readings already recorded

These decisions are in the plans, with their reasons. The code matches each, and none weakens a rule. They are spec ambiguities, not defects. The first four came with a suggested wording for SPEC.md.

- **§6.2, IRLS penalty** (`plans/prompt-02.md`): each solve uses 2λ, which makes IRLS converge to §6.1's minimizer; read literally, λ broke AC-2. Wording: "solve the weighted problem with these weights and penalty 2λ".
- **§7.6, PENDING** (`plans/prompt-04.md`): read literally, PENDING covers FETCH_FAILED too. Wording: "No data stored yet, and no failed fetch".
- **§12, the stale banner** (`plans/prompt-09.md`): with polls 30 s apart when idle, a literal 10 s age shows the banner most of the time. The UI judges the age when a poll fails. Wording: "a banner when a poll fails and the last successful poll is older than 10 s".
- **§5.2, late releases inside the archive** (`plans/prompt-03.md`): see section 3. Refinement in TODO.md.
- §5 (`plans/prompt-03.md`): the 24-month window's edges; `typical_lag` rounded up to a whole day; "exceeds" is strict; a null Tiingo close is missing; a release day before its observation day is accepted.
- §6 (`plans/prompt-02.md`):
  - IRLS keeps the better fit when a solve comes out worse;
  - a month without a new point runs no refit and keeps the previous anchor, as PROMPTS.md words it, where §6.4 speaks of a refit per month end;
  - the first refit month; T from whole days; the warm bracket clipped to [−8, 8]; a reference fit needs 8 points.
- §7 (`plans/prompt-04.md`):
  - "shorter than 10 years" means fewer than 3,652.5 days;
  - the split day is computed in integers;
  - a tie between A and −A goes to the positive A;
  - "clipped to [−730, 0]" is read as "restricted to";
  - "changes by less than 1e-8" is read as "falls by at most 1e-8";
  - F's median is exactly 1 for an odd count of days, and cosh(Δ/2), a hair above 1, for an even one;
  - DISCONTINUED's spacings count back from the latest release;
  - step 5's out-of-sample check is one descent from the full-window b;
  - §7.1's IRLS can stop short of the LAD optimum on small problems (accuracy, no rule changed).
- §8 and §9 (`plans/prompt-07.md`, `plans/prompt-06.md`):
  - every job that would compute needs SPY's data;
  - MISSING_KEY is ADD_SERIES's outcome;
  - "up to date" is literal: a changed `last_updated` with no new day still computes;
  - `typical_lag` runs over the stored and the new observations;
  - INSUFFICIENT_OVERLAP cannot arise from real data;
  - CONNECTION_FAILED; one retry budget per kind of failure; how Retry-After is read; any 2xx is a success; the pacing counts retries.
- §10 (`plans/prompt-05.md`): a changed `fred_id` forgets the old data; a removal also drops the name from the exclusions; curves are stored in y; SPY's row is USD and D.
- §11 (`plans/prompt-08.md`):
  - bodies hold exactly their fields; NaN and Infinity reach the field checks;
  - four more error codes (NOT_FOUND for unknown paths, METHOD_NOT_ALLOWED, PAYLOAD_TOO_LARGE, INTERNAL_ERROR);
  - ALREADY_EXISTS also by FRED ID; SPY's own chart route only;
  - the state document's `title_zh`, its point counts, `selected` null when not KEPT, and `f` null before a compute.
- §12 (`plans/prompt-09.md`):
  - the status bar shows the last job of any kind;
  - every series with a curve has a panel, the LEADL1 ones first;
  - English messages beside the localized codes;
  - Ctrl+wheel zooms on the PC, and a vertical swipe scrolls the page.
- §15 (`plans/prompt-10.md`): the launcher waits for `pythonw.exe`, and logs to `launch.log`; pool workers ignore Ctrl+C and are spawned on every system; the console handler is added only when there is a console.
- Additions with no rule against them (`plans/prompt-01.md`, `plans/prompt-05.md`): `web.port` refuses IB's API ports; `SPY` is a reserved catalog name; validation stops at the first error (TODO.md).

## 3. Look-ahead

### Paths found

1. **Step 3's positive shifts** (§7.2). For A > 0, the fit pairs SPY's day d with the indicator's one-sided value on day d + A, built from data released up to the month end before d + A: after d.
   - This is the intentional test of whether SPY leads, and the range is marked intentional.
   - Such a series is NOT_LEADING (A* ≥ 0) and never enters F. F's shifts are b ≤ 0 (AC-6).
   - Positive A also competes with negative A for A*, so data after d takes part in deciding whether a series is kept.
   - Proposal: one sentence in §6.4 or §7.2 saying that the no-look-ahead rule holds for every value at a shift A ≤ 0, and for F.
2. **§5.2's re-dating** (`plans/prompt-03.md`). A first release that came more than `typical_lag + 60` days late inside the archive gets `observation day + typical_lag`, a day before it was published, and the value counts from then.
   - The same happens when FRED adds an old observation today: it is placed back near its date.
   - The spec requires this. TODO.md holds the refinement: re-date only the observations released on the archive's first vintage day.
3. **Estimated release days** (§5.2, §17.3) use the lag of the latest 24 months. A value from years when the lag was longer then counts before its publication. Accepted limitation.
4. **Parameters**: A*, b, β and c are fitted over the whole window. These are in-sample parameters, not values for a day: the out-of-sample score is the guard (§7.5, §17.7).
5. **The reference fit** uses every point. It is the exception CLAUDE.md names: it feeds only charts (`web.chart`), never the analysis.

### Evidence for the rest

- **Points**: FRED's `realtime_start` with `output_type=4` is each value's first release (`pit.fred_observations`). A SPY close is released on its trading day. Stored rows never change (`store.insert_observations`, `ON CONFLICT DO NOTHING`; AC-11). The known-value rule keeps only the newest observation known on each release day (AC-4).
- **One-sided values**: refit k fits the points with release day ≤ m_k (`searchsorted(..., side="right")`). Day d takes the last m_k before it (`side="left"`), so a point released on m_k counts from m_k + 1. The AC-3 test changes and removes later points and checks every earlier value. `smoothing` is pure, with no clock.
- **`today`** is read once per job (`Jobs.today`) and passed down. The last refit is the last month end before it.
- **Step 3 at A ≤ 0**: r(d + A) depends on data released by the month end before d + A ≤ d. W3 keeps every d + A within the indicator's span, so `Daily.at` never wraps a negative index.
- **Step 5**: b ∈ [−730, 0] (`neighbours`, the random start's range, AC-6). W5 keeps d + b within every member's span.
- **Out-of-sample**: the first 70% is fitted alone, and the rest only scored (AC-8).
- **REFIT_F** reads the stored one-sided curves (`daily_of` uses `rt`, not `ref`).
- **`analysis_start`** enters no computation: the windows come from the curves' spans.

## 4. Secrets

### Paths found

None in normal operation. Notes:

1. **The login's Set-Cookie** carries the session value, by design (§11).
2. **The access log records request lines.** A URL that carried a secret would be logged. The UI never puts one in a URL, and since prompt 9 the login form posts, so even a native submit sends the token in a body.
3. **A static route's unexpected exception** is logged twice: redacted by `answer_errors`, then by aiohttp itself, unredacted. No static handler holds a secret, so nothing can leak; an API route answers 500 and is logged once, redacted.
4. **An unclosed aiohttp response** would make asyncio's exception handler log its repr, with the FRED URL and key. Every request uses `async with`, which closes the response, also when cancelled.
5. **`redact`** matches `api_key=`. A key in another spelling, such as percent-encoded, would pass, but no code writes one.
6. **Failed logins** wait 1 s each, with no limit across parallel requests (§11 as written). With a random token of at least 16 characters, inside the tailnet only, guessing stays out of reach.

### Evidence

- **Web token**: compared with `hmac.compare_digest`. "A login failed" is logged without it. Error texts are fixed. `Config`'s repr hides it. `test_no_secret_reaches_a_response_a_log_line_or_an_event` sweeps every route, error, access-log line and stored event for the token, the session value and both keys.
- **Session value**: an HMAC, sent only in Set-Cookie, and compared in constant time.
- **FRED key**:
  - it lives only in `Fred`'s query parameters;
  - the one log line with a URL is redacted;
  - `SourceError` texts hold no URL, and are raised `from None`;
  - tracebacks go through `redact` in `jobs.run`, `web.answer_errors` and `__main__.main`;
  - libraries log at INFO and above only.
- **Tiingo key**: in a header only. aiohttp logs no header, and errors are raised `from None`.
- **`last_fetch_error` and events** hold `SourceError` texts and redacted job messages. The state document gives the keys as booleans.
- **Git**: `.gitignore` covers `config.toml`, `data/` and `logs/`, and git tracks none of them. The only key-shaped strings are the tests' sentinels.

## 5. Dead code, dependencies, and CLAUDE.md

- **Dead code**: none.
  - Every top-level name and method in `spylead/` is referenced, in the syntax tree, not in comments; every function in `app.js` is called; every CSS custom property is used.
  - Six code tuples serve only the locale test, by design (AC-19): `analysis.STATUSES` and `F_STATUSES`, `jobs.KINDS` and `OUTCOMES`, `web.ERROR_CODES`, `sources.ERROR_CODES`.
- **Dependencies**: all used. numpy; scipy (`solveh_banded`; in tests, `make_smoothing_spline` and `linprog`); aiohttp; tzdata (zoneinfo on Windows); pytest. The vendored ECharts matches its hash. `pylock.toml` and `requirements.in` have not changed since prompt 1.
- **CLAUDE.md**, readings for the owner to confirm:
  1. **"Tests never open a network connection."** `test_web.py` uses aiohttp's TestServer and TestClient over loopback, and `test_main.py`'s serve tests bind 127.0.0.1 on port 0. §16.1 asks web tests to use aiohttp's test utilities, which open loopback sockets (`plans/prompt-08.md`). No test reaches another host, and `test_sources.py` forbids connecting. If loopback counts, the web tests can move to aiohttp's `make_mocked_request`.
  2. **"Never start the app."** `test_main.py` runs `python -m spylead` in a child process twice: with the placeholder config (exit 2) and with the lock held (exit 3). An audit hook kills the child on any socket event, and both end before the database and the port. AC-20 and AC-21 need such a second process.
  3. **"Worker-pool functions … take plain arrays."** `analysis.step3` and `step5` take `Daily` and `Member` dataclasses that hold arrays. They pickle under spawn, which `test_compute.py`'s spawned-pool test shows; only the letter of the rule differs.
- **CLAUDE.md, kept**: the pure modules import no I/O, clock, logging or aiohttp; public functions are typed; seeds are fixed; no secret, `config.toml`, `data/` or `logs/` in git; IB's ports are refused as `web.port` and never contacted.
- **Tests, robustness**:
  - `test_ctrl_c_cancels_serve_which_shuts_down_in_order` polls in 10 ms steps of real time (§16.1 asks for none);
  - `test_the_committed_icons_are_what_the_script_makes` compares pixels exactly. That needs `math.sin` to agree between Windows' and Linux's C libraries close to a rounding point, which is very unlikely to fail. A tolerance of one level per channel would remove the risk.
- **Transparency**: prompt 9 checked the UI in headless Chromium, with the Playwright preinstalled on this VM, from scratch files outside the repository. No dependency was added.

## 6. Proposed changes to TODO.md

Not applied: prompt 11 changes no other file.

- **To remove**: none. No deferred item has been built.
- **To add**:
  - Data: compute after a download that no compute has seen. Today an interrupted or failed compute is not redone, because the next Fetch Data is UP_TO_DATE (section 2.1, finding 1). Seam: the up-to-date test in `jobs.fetch_job`. Change §8.2 first.
  - Analysis: refit F, or mark the results stale, when the startup sync removes a member or changes a `fred_id` (section 2.1, finding 2). Seam: `__main__.serve` after `store.sync_catalog`.
  - Analysis: one sentence in §6.4 or §7.2 on positive shifts and the no-look-ahead rule (section 3, path 1). A spec change, no code.
  - Interface: say in the state document whether a candidate has a curve, instead of `app.js` inferring it from the status (`NO_CHART`). Seam: `web.series_state`.
  - Tests: one web test over a store filled by a real `Jobs` fetch with the fakes, so that the chart routes are shown to read what a fetch saves (AC-9 from end to end).
  - Tests: socket-free web tests (`make_mocked_request`), if the owner reads CLAUDE.md's "network connection" to include loopback (section 5).

## Extension points

None: the audit changes no code.

## Refactor seams

None beyond those of sections 2.1 and 6.

## Deferred items

Proposed in section 6, not appended to TODO.md (CLOUD.md: prompt 11 changes no other file).
