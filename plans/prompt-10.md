# Prompt 10: Operations and README

## Steps

1. `scripts/install-autostart.ps1` (§15), for Windows PowerShell 5.1, ASCII only (5.1 reads a file without a byte-order mark in the ANSI code page):
   - it writes the launcher `%LOCALAPPDATA%\spylead\launch.ps1`, on the system drive, with the project folder (the script's parent folder) in it;
   - it registers a per-user logon task named `spylead` that runs the launcher through `powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File`;
   - if registering the task is refused, it puts a shortcut to the same command in the user's Startup folder instead, and says so; once the task registers, a shortcut left by an earlier fallback is removed, so spylead never starts twice;
   - `-Remove` deletes the task, the shortcut and the launcher folder, saying what it removed.
2. The launcher:
   - it waits up to 10 minutes, checking every 5 s, for the project's `.venv\Scripts\pythonw.exe`, which is there once the external drive is mounted;
   - then it starts `.venv\Scripts\pythonw.exe -m spylead --config config.toml` in the project folder, hidden;
   - it appends what it did to `%LOCALAPPDATA%\spylead\launch.log`: started, with the process id, or gave up after 10 minutes.
3. `run.bat`: from its own folder, `.venv\Scripts\python.exe -m spylead --config config.toml` in the console; on a non-zero exit it pauses, so that a config error or "another process holds the lock" stays readable.
4. §15 against `__main__.py`:
   - startup order, the lock, logging, and shutdown in §15's order hold (prompt 8's tests);
   - gap: under `pythonw.exe`, which the autostart uses, `sys.stderr` is None, so the console handler fails on every record and logging swallows the error each time. The console handler is now added only when there is a console; the log file is unchanged.
5. `README.md`, for the owner, from a clean PC to the first charts: what spylead does and does not do; prerequisites and the two keys (§19); install from `pylock.toml`; configure; run; the first Fetch Data, "already up to date" and compute times; reading the results; Edge as an app; iPhone and iPad through Tailscale Serve, and why never Funnel or a public port; autostart, the fixed drive letter, and backups; the bench and §16.3's checks; §17's limitations; troubleshooting.
6. Tests, `tests/test_operations.py`, without running PowerShell:
   - `install-autostart.ps1` exists, is ASCII, requires PowerShell 5.1, and uses no syntax PowerShell 7 added (`??`, `?.`, `&&`, `||`, a ternary, `-Parallel`, `Join-Path` with two child paths);
   - it names the task `spylead`, writes the launcher under `%LOCALAPPDATA%`, registers a logon task, has a `-Remove` path that unregisters it, and a Startup-folder fallback that says so;
   - the launcher it writes waits up to 10 minutes and starts `pythonw.exe -m spylead --config config.toml` in the project folder;
   - `run.bat` exists, is ASCII, runs from its own folder and starts the venv's `python.exe -m spylead --config config.toml`;
   - the README covers each topic the prompt lists, and every command it gives names files that exist;
   - `tests/test_main.py`: without a console, logging goes to the file only.
   - AC-21 still holds: prompt 8's tests in `tests/test_main.py` are unchanged and pass.
7. `.venv/Scripts/python -m pytest -q`.

### Decisions beyond the letter of SPEC.md

- **The launcher waits for `pythonw.exe`**, not just the folder: the folder being there means little until the venv in it is.
- **The launcher logs to `%LOCALAPPDATA%\spylead\launch.log`**: under autostart nothing is on screen, and this file says whether the launcher ran and when it gave up.
- **The task's command** is the same in the shortcut, so both paths behave alike. `-ExecutionPolicy Bypass` lets the launcher run under any execution policy; it applies to that one process only.
- **The task** starts on battery power too and does not stop when the power source changes: the defaults would skip a logon on a laptop running on battery.
- **Stopping an autostarted spylead**: it has no console, so Ctrl+C cannot reach it. The README says to end `pythonw.exe` in Task Manager while no job runs, or to start it with `run.bat` instead. Ending the process keeps the database whole (each save is one transaction) and frees the lock.
- **run.bat pauses on a non-zero exit** only, so a normal Ctrl+C closes the window.

## Extension points

- **A Windows service** (TODO.md) would replace the logon task; the launcher's wait and command stay the same.
- **More launcher conditions** (for example a network check) go in `launch.ps1`'s wait loop.

## Refactor seams

- **The launcher's text** is built in one function of `install-autostart.ps1`; the task and the shortcut both call `powershell.exe` with the same arguments.

## Deferred items (appended to TODO.md)

- A way to stop an autostarted spylead gracefully, with the shutdown of §15, instead of ending `pythonw.exe` (for example a stop button for the owner, or `run.bat` stopping the hidden process first).
