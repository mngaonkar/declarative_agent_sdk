# Creating A2A agents as background services

`install.sh` creates a lean A2A agent and runs it as a supervised background
process, from command-line flags alone. It is built to be called by something
else — an orchestrating agent provisioning sub-agents — so it is
non-interactive, it never prompts, and with `--json` it prints one result
object on stdout.

It needs nothing but itself: the SDK is installed from GitHub, and `agent.yaml`,
`instructions.md` and `run_server.py` are generated from the flags. There is no
repository checkout involved, so the script can be downloaded on its own.

```bash
./install.sh --name researcher --provider openai --model gpt-4o-mini
./install.sh --name coder --provider vllm --model qwen2.5-coder:latest \
             --endpoint http://10.0.0.147:11434/v1 --json
./install.sh --list
./uninstall.sh --name coder --purge
```

Requires [uv](https://astral.sh/uv). That is not a preference: pip cannot
resolve this dependency set at all, exiting with `resolution-too-deep` after
several minutes.

## supervisord runs the agents

Every agent is a program under a single **supervisord**, which `install.sh`
installs into `<root>/supervisor/.venv` and starts on first use. This replaces
the per-agent systemd units and launchd jobs earlier versions created:

- **one mechanism on both platforms** — same config format, same control
  command, no `daemon-reload` on one OS and `bootout`/`bootstrap` on the other;
- **no root** — supervisord, its config and its socket are all owned by the
  invoking user. Creating a sub-agent on a box where you have no sudo works;
- **adding an agent is a file drop** — `conf.d/<name>.conf`, then
  `supervisorctl reread && update`, which leaves the other agents running;
- **an XML-RPC control plane** on the unix socket, if the orchestrator wants
  `getAllProcessInfo`/`startProcess` rather than shelling out;
- **log rotation**, which launchd does not do at all.

```text
<root>/supervisor/
  .venv/              supervisor itself
  supervisord.conf    generated once
  conf.d/<name>.conf  one per agent
  s.sock              unix socket, 0700
  supervisord.log
```

The socket is deliberately a unix socket. `inet_http_server` has no
authentication by default, and a remote process-control endpoint next to an
already network-exposed agent is a bad combination.

### Surviving a reboot

supervisord cannot start itself, so this is the one place a platform service is
still involved — created **once per machine**, not per agent:

```bash
./install.sh --enable-at-boot            # may be passed on its own
```

On Linux that is a systemd unit (system-wide as root, otherwise a `--user`
unit, which needs `loginctl enable-linger <user>` to start before login). On
macOS it is a LaunchAgent, or a LaunchDaemon when run as root. Without it,
agents survive crashes but not reboots.

## Many agents on one machine

Each agent is independent — its own directory, virtualenv, program and port:

| | |
|---|---|
| directory | `<root>/<name>`, where root is `/opt/declarative-agent` as root, `$XDG_DATA_HOME/declarative-agent` on Linux, `~/.declarative-agent` on macOS |
| program | `[program:<name>]` in `<root>/supervisor/conf.d/<name>.conf` |
| port | `--port`, otherwise the first free one from `--port-base` (default 8000) |
| address | binds `0.0.0.0` so other machines can reach it; publishes this host's primary address |

Port allocation skips ports recorded by other installed agents *and* ports
something is already listening on. Passing a `--port` another agent holds is
refused rather than silently accepted. Re-installing an existing agent keeps
its port, so clients holding its URL keep working.

Each agent records its own metadata in `<prefix>/.agent-meta`, which is what
`--list` reads and what the port allocator consults.

## Bind address versus published address

These are two different things, and conflating them is how an agent ends up
advertising a URL nothing can reach.

| Flag | Meaning | Default |
|---|---|---|
| `--host` | which interface the server listens on | `0.0.0.0`, every interface |
| `--advertise-host` | what goes in the agent card and in `url` | this host's primary address |

With the defaults, an agent on a LAN host publishes something like
`http://10.0.0.170:8000/` and is reachable from any machine on that network.
The address is found the way the SDK finds it — a UDP `connect()` reserves a
route without sending a packet, and `getsockname()` reports the local address
the kernel chose — falling back to the primary interface address and then the
hostname if there is no default route.

```bash
./install.sh --name api --advertise-host agents.internal.example   # behind a proxy
./install.sh --name api --advertise-host 203.0.113.9               # behind NAT
./install.sh --name api --host 127.0.0.1                           # local only
```

The generated `run_server.py` writes this URL into the agent card itself rather
than letting `AIAgentServer` fill it in, because the SDK's own logic re-probes
the default route at startup and so cannot account for NAT, Docker or a proxy.

## Calling it from another agent

```bash
result=$(./install.sh --name "$name" --provider vllm \
                      --endpoint "$endpoint" --model "$model" --json)
port=$(printf '%s' "$result" | jq -r .port)
card=$(printf '%s' "$result" | jq -r .agent_card)
```

Progress narration goes to **stderr**; stdout carries only the JSON. The object
holds `name`, `prefix`, `bind_host`, `advertise_host`, `port`, `url`,
`agent_card`, `framework`, `provider`, `model`, `service_kind` (`supervisor`),
`service_id` (the program name), `service_state`, `supervisor_conf`,
`supervisorctl`, `sdk_version`, `sdk_source`, `run_command`,
`uninstall_command`, `ready` and `problems`.

`url` and `agent_card` are what a client should dial. They are built from
`advertise_host`, never from `bind_host`, because the default bind of `0.0.0.0`
is not an address anything can connect to.

Exit status is the thing to branch on:

| Status | Meaning |
|---|---|
| 0 | agent created and started; `ready` is true |
| 3 | created but cannot start — missing credential or bad config. The JSON is still printed, with `ready: false` and `problems` listing why |
| 1 | usage error, port conflict, or the install itself failed |

A not-ready agent is registered with supervisord but not started, so it does
not burn its restart budget. Fix the cause and run the same command again.

## Credentials

Secrets are never flags. An API key in `argv` is visible to every user on the
box through `ps`, so instead:

- the provider's key is read from `install.sh`'s own environment
  (`OPENAI_API_KEY`, `GOOGLE_API_KEY`, `ANTHROPIC_API_KEY`), and written to
  `<prefix>/.env` with mode 0600;
- or `--env-file F` copies a prepared file into place;
- or you write `<prefix>/.env` yourself and re-run.

`provider: vllm` needs no key, which makes a local OpenAI-compatible server the
simplest thing to point sub-agents at.

Whether the credential is usable is checked by the SDK's own `preflight()`, so
what it demands follows the config's provider instead of a name hardcoded here.

## What gets generated

```text
<prefix>/
  agent.yaml        from the flags; kept on reinstall unless --force
  instructions.md   --instructions, --instructions-file, or a default
  run_server.py     rewritten every run — put changes in the two files above
  .env              0600
  .agent-meta       JSON, read by --list and the port allocator
  skills/           empty; drop SKILL.md directories in to add capabilities
  workspace/        where the agent's own file tools write
  logs/             stdout and stderr, rotated at 10MB x3
  .venv/            the SDK, installed from --source
```

`--source git` (default) builds from the repository at `--ref`, so a sub-agent
can track an unreleased fix. `--source release` installs the prebuilt wheel for
`--version`, which is faster and needs no build toolchain.

`run_server.py` is generated rather than shipped because `declarative-agent
a2a` is deliberately not wired up — `cli/main.py` returns 2 and says to use
`AIAgentServer` directly.

## Operating an agent

```bash
SCTL="<root>/supervisor/.venv/bin/supervisorctl -c <root>/supervisor/supervisord.conf"

$SCTL status                 # every agent on this machine
$SCTL tail -f <name>         # follow its log
$SCTL restart <name>         # after editing agent.yaml or instructions.md
$SCTL stop <name>            # leave it registered but down
$SCTL shutdown               # stop supervisord and every agent with it

curl -s http://<advertise_host>:<port>/.well-known/agent-card.json
```

The exact `supervisorctl` invocation is in the install's JSON output, so an
orchestrator does not have to reconstruct it.

## What recovers what

| Failure | Handled by | Visible as |
|---|---|---|
| Unhandled exception, OOM kill, wedged process | supervisord | restarted within a few seconds; `RUNNING` with a new pid |
| Missing credential, unloadable config | nothing, and it must not be retried forever | `startretries=3`, then the program goes `FATAL` and is left alone |
| Port already bound by something else | nothing | the server exits non-zero, supervisord retries three times into the same conflict, then `FATAL` |
| supervisord itself dying, or a reboot | the one `--enable-at-boot` entry | everything comes back together |

## Three gotchas

**An externally bound agent is unauthenticated.** The default `--host 0.0.0.0`
is what makes a sub-agent reachable from other machines, but the A2A endpoint
has no authentication and wide-open CORS, and the agent can run shell commands
through `exec_command`. Keep it on a trusted network, firewall it, or bind one
private interface with `--host 10.0.0.5`. The installer warns whenever it binds
every interface.

**supervisord gives `exec_command` *fewer* restrictions than systemd did.** The
old per-agent unit set `NoNewPrivileges=true` (no sudo, no setuid) and
`PrivateTmp=true` (a namespaced `/tmp`). supervisord has no equivalent, so
commands the agent runs inherit the normal environment of the user supervisord
runs as. `stopasgroup`/`killasgroup` are set, so a stop still takes the agent's
children with it. If that trade matters, run supervisord under a confined
account.

**The unix socket path is capped at about 104 bytes.** A deep agents root makes
supervisord fail at bind time with `AF_UNIX path too long`. The installer
checks this up front and says so rather than letting it fail obscurely.

## Removal

```bash
./uninstall.sh --name <name>            # stop the program, drop its config
./uninstall.sh --name <name> --purge    # delete the directory too
./uninstall.sh --list                   # same listing as install.sh --list
```

`--purge` deletes `.env` and `workspace/`, so it is guarded: it refuses `/`,
`$HOME`, the current directory, any path under three components deep, and
anything that does not look like an agent install. The delete runs through
python rather than `rm -rf "$PREFIX"` so a mistyped `--prefix` cannot expand
into something unintended.

Agents created **before** the switch to supervisord recorded `service_kind` as
`systemd`, `launchagent` or `launchdaemon` in their metadata. `uninstall.sh`
reads that and removes the old unit or plist as well, so a purge cannot leave
an orphaned registration pointing at a directory that no longer exists.
