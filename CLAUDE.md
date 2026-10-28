# spylead

Personal research tool. One Python process on the owner's Windows 11 PC:

- it downloads SPY daily closes (Tiingo) and US leading indicators (FRED first releases);
- it smooths each series with a one-sided robust cubic smoothing spline;
- it measures how many days each indicator leads SPY, keeps the leading ones, and fits their combination F;
- it serves the charts as a PWA, used from the PC in Edge and from iPhone and iPad in Safari through Tailscale Serve.

Documents:

- `SPEC.md` is the source of truth for behaviour. Read the sections a task names before writing code.
- `PROMPTS.md` is the build sequence.
- `TODO.md` holds everything deferred.

## Rules

- Never weaken, skip or "simplify" a rule in SPEC.md without asking. Behaviour marked "intentional" is the owner's decision, even where it looks like a bug.
- Tests never open a network connection. Tiingo and FRED are reached only through `sources.py`; tests use fakes.
- Never start the app (`python -m spylead` or `python -m spylead.bench`), and never contact Tiingo or FRED, unless I ask. Prove behaviour with tests.
- No look-ahead. Every value the analysis uses for day d comes from data released by the last month end before d (SPEC.md §5, §6.4). The reference fit is the only exception, and it is display only.
- Never write the web token, the session value, the FRED key or the Tiingo key to a log, an event, an API response, a chart payload or an error message. FRED takes its key in the URL: never log a FRED URL unredacted.
- Never commit `config.toml`, `data/`, `logs/` or any key.
- This PC may also run IB Gateway for another project. Never connect to ports 4001, 4002, 7496 or 7497.

## Code style

- Small single-purpose functions. No dead code. No speculative abstraction (YAGNI).
- Follow existing patterns in this repo before inventing new ones.
- Comments explain why, never what.
- Type hints on public functions; dataclasses for data.
- `pit.py`, `smoothing.py` and `analysis.py` stay pure: no I/O, no clock reads, no logging, no aiohttp.
- Numerical code is vectorized with NumPy and deterministic. Fix every random seed.

## Dependencies

- **Python.** Approved: `numpy`, `scipy`, `aiohttp`, `tzdata` (runtime); `pytest` (dev). Use the standard library for everything else.
- **Frontend.** Apache ECharts 6.1.0, vendored at `spylead/static/vendor/echarts/`. Do not replace or upgrade it. No framework and no build step.
- **Lock.** Dependencies are locked with hashes in `pylock.toml` (SPEC.md §13). Never re-lock, upgrade, or install anything outside the lock unless I ask.
- Ask before adding any other dependency, including dev tools.

## Output discipline

- Show diffs or changed files only. Never re-print unchanged code.
- No explanations, apologies, or summaries unless I ask.

## Planning

Every plan lists, before any code is written:

1. Steps
2. Extension points
3. Refactor seams
4. Deferred items (append them to `TODO.md`)

Then wait for my approval.

## Quality bar

Ship "good enough": correct, tested, readable. A phase is done when its tests pass and the acceptance criteria it names in SPEC.md §16 hold.

## Environment

- **Runtime.** Native Windows 11, Python 3.12+. The project lives on `E:\00_CODING\spylead`, an external drive that may be exFAT.
- **Portability.** Code must still import and pass tests on any OS: cloud sessions run Linux. Guard Windows-only calls.
- **Install.**
  1. `python -m venv .venv`
  2. `.venv/Scripts/python -m pip install --upgrade pip`
  3. `.venv/Scripts/python -m pip install -r pylock.toml`
- The project is not installed as a package. Run the tests and the app from the repository root.
- **Test.** `.venv/Scripts/python -m pytest -q`
- **Run.** `.venv/Scripts/python -m spylead --config config.toml`
- Forward slashes work in both PowerShell and Git Bash.
- When you edit files, use your file-editing tools, not `git apply`, which the unattended runner does not allow.

## Numerics

- SciPy and NumPy are whatever `pylock.toml` pins. Check their APIs against the installed source, not memory.
- NumPy 2 batched `linalg.solve` needs an explicit trailing axis for stacked right-hand sides (SPEC.md §7.7).
- Worker-pool functions are top-level and take plain arrays: Windows spawns workers.
