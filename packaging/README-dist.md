# declarative-agent

A self-contained agent runner. No Python installation required.

## Contents

| File | Purpose |
|---|---|
| `declarative-agent` | The executable |
| `agent.yaml` | Agent definition — model, provider, tools, skills |
| `instructions.md` | System instructions for the agent |
| `skills/` | Progressive skills (`skills/<name>/SKILL.md`) |
| `.env.example` | Template for credentials |

## Setup

```bash
cp .env.example .env     # then fill in your keys
./declarative-agent discord --check    # verify the Discord side
./declarative-agent discord            # serve the agent
```

`--check` logs in, reports which channels the bot can post in, and exits — run it
first. The bot answers @mentions, DMs, and the `!ask ` prefix. Ctrl-C stops it;
it also shuts down cleanly on `SIGTERM`.

The bot shows as online in Discord only while the process is running. For an
always-on bot use a supervisor (`systemd`, or a container with
`restart: unless-stopped`).

## First run

The first invocation unpacks an embedded Python and installs dependencies into a
managed directory — expect roughly 20-45 seconds and a few hundred MB. Later
runs start immediately.

`./declarative-agent self --help` exposes the management commands:
`self remove` deletes the installation, `self restore` reinstalls it. If you
replace the binary with a rebuild of the *same version*, the cached install is
reused — run `self remove` first or the old code keeps running.

## Credentials

Read from the environment, or from a `.env` beside `agent.yaml`, or `./.env`.
Use `--env-file` to point somewhere else.

| Variable | Needed for |
|---|---|
| `DISCORD_BOT_TOKEN` | Connecting to Discord |
| `OPENAI_API_KEY` | The default `provider: openai` in `agent.yaml` |
| `ANTHROPIC_API_KEY` / `GOOGLE_API_KEY` | If you repoint the provider |
| `TAVILY_API_KEY` | The `tavily_search` tool |

Missing credentials are reported at startup, naming exactly what to set, rather
than failing on the first model call.

## Editing the agent

`agent.yaml`, `instructions.md` and `skills/` are plain files — edit and restart,
no rebuild. Paths inside `agent.yaml` resolve relative to the file itself, so the
directory can live anywhere.

## macOS

Downloaded binaries are quarantined by Gatekeeper. This build is unsigned, so:

```bash
xattr -d com.apple.quarantine ./declarative-agent
```

or right-click → Open once.
