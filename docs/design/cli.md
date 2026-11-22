# Command line — design spec

The `declarative-agent` command (set in `pyproject.toml` as
`declarative_agent_sdk.cli.main:main`).

Code: `cli/`.

## `cli/__init__.py`

**Does:** marks `cli` as a package. It deliberately does not re-export `main`,
which would hide the `cli.main` submodule.

## `cli/__main__.py`

**Does:** lets `python -m declarative_agent_sdk.cli` run the CLI.

## `cli/main.py`

**Does:** the subcommands and the checks that run before an agent starts.

**Rules — commands:**

- CLI-1: `discord --config agent.yaml` serves an agent on Discord. Options:
  `--check` (log in, report what the bot can see, log out), `--env-file`,
  `--prefix` (default `!ask `), `--activity`, `--embed`, `--log-level`.
- CLI-2: `control` runs agents-control. Options: `--host` (default
  `127.0.0.1`), `--port` (default 8900), `--db` (default
  `~/.agents-control/control.db`), `--token`, `--public-url`, `--log-level`.
- CLI-3: `version` prints the SDK version (CORE-VER-1).
- CLI-4: `a2a` is reserved and exits with status 2, pointing to
  `AIAgentServer`.

**Rules — before starting:**

- CLI-5: The config is `--config`, else `./agent.yaml`.
- CLI-6: The first `.env` found is loaded: `--env-file`, then the config's
  folder, then the current folder.
- CLI-7: Preflight lists every problem that would break the first model call
  — missing API key for the provider, unknown provider, missing
  `DISCORD_BOT_TOKEN` for `discord` — and stops before starting, saying how to
  fix each. Non-fatal problems are warnings.
- CLI-8: Discord login failures (bad token, privileged intents not enabled)
  are explained in words, not as a traceback.

**Tests:** `tests/test_cli.py`.
