# Contributing

Thanks for helping. Bug reports and small, focused changes are the easiest to take.

## Set up

With [mise](https://mise.jdx.dev), which pins python, ruff, node and uv in `mise.toml`:

```bash
mise install          # the pinned tools
mise run setup        # .venv with the package and its dev tools, as uv.lock pins them
```

`mise tasks` lists the rest. Without mise: `uv sync --locked`.

## Checks

Every change passes all three before it is ready:

```bash
mise run test         # pytest
mise run lint         # ruff
mise run typecheck    # pyright
```

- Tests mirror the package: the tests for `src/departureboard/<folder>/<module>.py` are in `tests/<folder>/test_<module>.py`. `tests/test_deploy.py` keeps the deploy files and the docs that describe them in step, so change them together with the code.
- Fixing a bug starts with a test that fails because of it. Then the fix.
- When a test fails, fix the code, not the test. Change a test only when the intended behaviour has changed, and say so.
- The JS behaviour tests run when `node` is on PATH.
- Don't run a formatter over the codebase. ruff checks; it doesn't reformat.

## Commits

Conventional commits: `<type>(<scope>): <subject>`, in the imperative, like `fix(timetable): keep journeys that start at your stop`. The body says why. Keep each commit small enough to revert on its own.

## How the code is written

- **Security first.** Treat every input as hostile: from the network, the settings forms, files and other services. Check it where it comes in. Units and containers get the least privilege that works.
- **Accessible pages.** WCAG 2.2 AA is the floor for every page: it works from the keyboard, uses semantic markup, has enough contrast and a visible focus.
- **Plain British English** in code, UI text and docs. Short sentences, active voice, no emoji.
- **Simple code.** Match the style around it.

The [README](README.md#layout) shows where things are.
