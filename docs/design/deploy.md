# Installing agents on a machine — design spec

How an agent becomes a long-running service on a macOS or Linux machine. The
board runs these scripts over SSH ([agents-control/machines.md](agents-control/machines.md));
people can run them by hand too. User guide: [../deployment.md](../deployment.md).

Code: `packaging/deploy/`, `Dockerfile`, `build-docker.sh`,
`run_agents_control.sh`, `run_ui.sh`.

## `packaging/deploy/install.sh`

**Does:** creates or updates one agent: its folder, Python environment,
config and a supervisord program, then starts it and reports the result.

**Rules — layout:**

- DEPLOY-INST-1: Agents live under `AGENTS_ROOT`: `~/.declarative-agent` for a
  user, `/opt/declarative-agent` for root. One folder per agent
  (`--prefix` overrides) holding `agent.yaml`, `instructions.md`,
  `run_server.py`, `.env` (mode 0600), `.venv/`, `skills/`, `workspace/`,
  `logs/` and `.agent-meta` (JSON about the install).
- DEPLOY-INST-2: One supervisord per machine, in `AGENTS_ROOT/supervisor`,
  runs every agent. Each agent is one program file in `conf.d/`, with
  `autostart` and `autorestart` on.
- DEPLOY-INST-3: A reinstall keeps `agent.yaml`, `instructions.md`,
  `workspace/` and the port unless `--force`. `.env` is replaced only when
  `--env-file` is given.

**Rules — what gets installed:**

- DEPLOY-INST-4: `--source git` (default, branch `master`), `release`, or
  `wheel` (a file on this machine). A wheel is always force-reinstalled,
  because its version does not change while code is being worked on.
- DEPLOY-INST-5: When a `uv` step fails, the error includes the last 20 lines
  of what it printed.

**Rules — address and port:**

- DEPLOY-INST-6: `--port` or the next free port from 8000.
- DEPLOY-INST-7: `--host` (default `0.0.0.0`) is tried with a real bind before
  anything is configured. An address this machine does not have stops the
  install with the machine's real addresses listed.
- DEPLOY-INST-8: The published address is `--advertise-host`, else the bind
  address when it is a single interface, else the address of the default
  route. Never `0.0.0.0`.

**Rules — starting and reporting:**

- DEPLOY-INST-9: Before starting, the agent is built once from `agent.yaml`; a
  config that cannot be built, or a missing credential, makes the install
  "not ready" and the program is left stopped.
- DEPLOY-INST-10: After starting, it waits (at most 15 s) for supervisord to
  leave STARTING. Not RUNNING means not ready, with the last error line from
  the agent's stderr log as the reason.
- DEPLOY-INST-11: Exit status 0 = ready, 3 = installed but not ready, 1 =
  usage or install failure. With `--json` the result object is printed in the
  ready and not-ready cases alike (`ready`, `problems`, `url`, `port`,
  `prefix`, `service_state`, …).
- DEPLOY-INST-12: `--list` prints every installed agent's `.agent-meta` as a
  JSON array.

**Rules — supervisord and boot:**

- DEPLOY-INST-13: Where logind kills a user's processes at logout and
  lingering is on, supervisord runs as a systemd user unit so it outlives the
  SSH session.
- DEPLOY-INST-14: `--enable-at-boot` makes supervisord start at boot (a
  systemd unit on Linux, a launchd job on macOS). It is safe to repeat: an
  unchanged unit that is already running is left alone; otherwise the running
  supervisord is handed over to the boot service. If the boot service does
  not bring supervisord up, it is started directly, so agents are never left
  down. Without lingering on Linux, boot start is skipped with a warning.

## `packaging/deploy/uninstall.sh`

**Does:** removes an agent's supervisord program, and with `--purge` its
folder too.

**Rules:**

- DEPLOY-UNINST-1: `--purge` refuses any folder that does not look like an
  agent install.
- DEPLOY-UNINST-2: `--json` prints one result object; `--list` lists installed
  agents.

## `Dockerfile`

**Does:** a two-stage `python:3.14-slim` image that runs an agent
(`python agent.py`, port 10004). See [../docker.md](../docker.md).

## `build-docker.sh`

**Does:** builds the image `declarative-agent:<tag>` with `docker buildx`.

## `run_agents_control.sh`

**Does:** starts agents-control on port 8900 (`declarative-agent control`).

## `run_ui.sh`

**Does:** runs the A2UI dev server in a container on port 5173
([a2ui.md](a2ui.md)).
