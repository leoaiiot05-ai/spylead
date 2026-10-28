# Cloud sessions

Read this when a Claude Code cloud session is asked to continue the spylead build.

- On the owner's Windows PC, `run_prompts.py` runs the same build, and its sessions ignore this file.
- The PC and the cloud share state only through git: each finished prompt is a commit, and its plan is in `plans/`.

## Before you start

- The owner puts the PC's runner on hold (`build.cmd --hold`) before starting a cloud session, so only one side works at a time.
- If a push is ever rejected because `master` has moved, the PC is working too: stop and report it.

## Where the build stands

- A prompt is done when `git log --format=%s` shows a commit whose subject starts with `Prompt N:`. A subject that starts with `WIP prompt N:` marks unfinished work.
- Work through PROMPTS.md in order, starting at the first prompt that is not done. Stop when every prompt is done, or at the prompt the owner named.
- If `plans/prompt-NN.md` already exists, an earlier session, on the PC or in the cloud, started that prompt. Continue from its plan and the current files instead of starting over.

## Rules for each prompt

Nobody is watching this session. These rules replace "Plan first ... wait for my approval" in PROMPTS.md and the approval step of the Planning section in CLAUDE.md.

1. If the plan file does not exist, write the plan, in the format CLAUDE.md requires, to `plans/prompt-NN.md` before changing any code, and append its deferred items to TODO.md.
2. Implement it without waiting and without asking questions. Anything the prompt asks you to give the owner, such as a checklist or a report, also goes into the plan file.
3. Never start the app (`python -m spylead`, `python -m spylead.bench`), never contact Tiingo or FRED, and never connect to IB. Prove behaviour with tests.
4. Finish with the full test suite passing.
5. Commit everything with the subject `Prompt N: <title as written in PROMPTS.md>`, then push it (see Git).
6. If something blocks you, such as a contradiction in SPEC.md:
   - describe it under a `## Blocked` heading in the plan file;
   - commit with the subject `WIP prompt N: blocked`, push, and stop;
   - never weaken a rule to get past it.

   Remove a `## Blocked` section left by an earlier session only once the blocker no longer holds.

Prompt 11 is the audit: write the whole report to `plans/prompt-11.md` and change no other file.

## This VM is not the owner's PC

- The VM runs Linux and the owner runs Windows 11. Where CLAUDE.md says `.venv/Scripts/python`, use `.venv/bin/python`. Keep Windows-only code guarded, as CLAUDE.md requires.
- `pylock.toml` is generated on the PC and is valid only there. Never install from it, regenerate it or edit it here.
  - For tests, create `.venv` with Python 3.12 or later and install `requirements.in` into it.
  - This exception to "install only from the lock" applies to this VM only.
- Prompt 1 generates the lock, so it runs on the PC. If prompt 1 is not done, stop and say so.
- The manual checks after prompts 9 and 10 happen later on the PC. Do the code and the tests only.

## Git

- Push every commit as soon as you make it: `git push origin HEAD:master`. The session can stop at any moment, for example at a usage limit or when an idle VM is reclaimed, and only pushed work survives.
- During a long prompt, also commit and push a checkpoint after each major step, with the subject `WIP prompt N: <step>`.
- If a push is rejected because master has moved, stop and report it. Never force-push or rewrite history.
- Never commit `.venv/`, `config.toml`, keys, `data/` or `logs/`.
