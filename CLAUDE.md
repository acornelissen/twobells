# Project Instructions for AI Agents

This file provides instructions and context for AI coding agents working on this project.

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:1105d646 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/core-concepts/sync-concepts.md for details and anti-patterns.

## Agent Context Profiles

The managed Beads block is task-tracking guidance, not permission to override repository, user, or orchestrator instructions.

- **Conservative (default)**: Use `bd` for task tracking. Do not run git commits, git pushes, or Dolt remote sync unless explicitly asked. At handoff, report changed files, validation, and suggested next commands.
- **Minimal**: Keep tool instruction files as pointers to `bd prime`; use the same conservative git policy unless active instructions say otherwise.
- **Team-maintainer**: Only when the repository explicitly opts in, agents may close beads, run quality gates, commit, and push as part of session close. A current "do not commit" or "do not push" instruction still wins.

## Session Completion

This protocol applies when ending a Beads implementation workflow. It is subordinate to explicit user, repository, and orchestrator instructions.

1. **File issues for remaining work** - Create beads for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **Handle git/sync by active profile**:
   ```bash
   # Conservative/minimal/default: report status and proposed commands; wait for approval.
   git status

   # Team-maintainer opt-in only, unless current instructions forbid it:
   git pull --rebase
   git push
   git status
   ```
5. **Hand off** - Summarize changes, validation, issue status, and any blocked sync/commit/push step

**Critical rules:**
- Explicit user or orchestrator instructions override this Beads block.
- Do not commit or push without clear authority from the active profile or the current user request.
- If a required sync or push is blocked, stop and report the exact command and error.
<!-- END BEADS INTEGRATION -->


## Build & Test

Python 3.12 package, tools pinned in `mise.toml` (python, ruff, node).

```bash
mise install          # pinned tools
mise run setup        # .venv with the package and dev extras (pip install -e ".[dev]")
mise run test         # pytest -q
mise run lint         # ruff check src tests tools (config in pyproject.toml)
mise run demo         # departureboard demo --png preview.png, no keys needed
mise run emulate      # browser panel emulator on sample data
mise run dashboard    # dashboard with ./config.toml and ./secrets.env
```

- Tests are pytest in `tests/`; `pythonpath = ["src"]`, so they run against this checkout's `src`.
- `test_deploy.py` keeps the Dockerfile, compose file, systemd units and the docs that describe them in step;
  `test_cli.py` covers the command line. Change the docs and deploy files together with the code.
- The dashboard's and emulator's JS behaviour tests run through `node` when it is on PATH and skip otherwise.
- Don't run a formatter over the codebase; ruff is for checks only.

## Architecture Overview

One package, `src/departureboard`, with one CLI, `departureboard` (`cli.py`):

- `run`: the board renderer for the Pi's e-paper or HDMI screen (systemd timer, or `--loop` for a screen).
- `web`: the settings page on the home network. Its lookups go through `setup serve`
  (`departureboard-setup.service`), which alone has outbound network access.
- `dashboard`: the web dashboard for a Mac or NAS (`dashboard_static/`, `Dockerfile`, `docker-compose.yml`).
- `timetable`: builds the BODS bus timetable for the configured stops.
- `demo`, `emulate`, `probe`, `route`, `setup discover`: development and setup helpers.
- `deploy/`: systemd units and install/update scripts for the Pi. `docs/`: user guide and API notes.

A refactor is under way to merge the board and the dashboard into one app (bead epic `inky-departures-vrj`),
so these entry points will change. Check the epic before adding to either.

## Conventions & Patterns

- Security first: treat every input as hostile, validate at boundaries, least privilege in units and containers.
- WCAG 2.2 AA is the floor for every page: keyboard use, semantic markup, contrast, visible focus.
- Plain British English in code, UI text and docs. No emoji.
- Conventional commits, `<type>(<scope>): <subject>`, imperative; the body says why.
- Track work in beads (`bd`), not TODO files.
