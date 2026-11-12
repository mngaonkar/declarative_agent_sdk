# Running the Discord agent as a service

The bot is a long-lived foreground process that exits non-zero on any failure,
so supervision is the OS's job: **systemd** on Linux, **launchd** on macOS.

```bash
# Linux — systemd unit in /etc/systemd/system
sudo ./packaging/deploy/install.sh --service-user youruser

# macOS — LaunchAgent in ~/Library/LaunchAgents (no sudo)
./packaging/deploy/install.sh

# macOS — LaunchDaemon, starts at boot and survives logout
sudo ./packaging/deploy/install.sh --system --service-user youruser
```

The first run seeds `.env` from `.env.example` and stops there, because a bot
with no token cannot start. Fill in the keys and run the same command again:
re-running is an upgrade, and it keeps `agent.yaml`, `instructions.md`,
`skills/`, `workspace/` and `.env` exactly as they are.

Remove it with `uninstall.sh` (add `--system` on macOS if you installed a
daemon). That stops and unregisters the service but leaves the install
directory, since it holds your token and whatever the agent wrote; `--purge`
deletes it too, after asking.

Nothing in the SDK restarts itself, and that is deliberate — a process that
re-execs itself cannot recover from the failures that matter (a wedged event
loop, OOM, a bad deploy), and it hides the crash from whatever is watching.

## What recovers what

Three layers, and it helps to know which one handles a given failure:

| Failure | Handled by | Visible as |
|---|---|---|
| Gateway blip, Discord 5xx, resumed session | `discord.py` internally (`client.start(..., reconnect=True)`) | a log line; the process never exits |
| Reconnect exhausted, unhandled exception, OOM kill | the supervisor | restart after `RestartSec` / `ThrottleInterval` |
| Bad token, MESSAGE CONTENT intent off, missing API key | nothing — and it must not be retried forever | repeated restarts, then systemd's start limit latches |

That third row is the one to get right. `cli/main.py` returns 1 for both "the
network dropped" and "your token is wrong", so a naive `Restart=always` with no
start limit will sit in a login-failure loop against Discord's API. The systemd
unit caps this at five failures per five minutes; launchd has no equivalent, so
the plist leans on `ThrottleInterval` instead.

## What install.sh does

1. Picks the install directory — `/opt/discord-agent` on Linux,
   `$HOME/.declarative-agent-discord` on macOS, or `--prefix`.
2. Finds a wheel in `dist/`, or builds one with `uv` if there is none.
3. Copies `agent.yaml`, `instructions.md` and `skills/` from
   `examples/discord_bot` (or `--agent-dir`) — **only if absent**, so an
   upgrade never discards local edits.
4. Creates `.venv` on Python 3.14 and installs the wheel with the `discord`
   extra, which is what pulls in `discord.py`.
5. Renders the matching template from this directory and installs it.
6. Starts the service, unless the token is missing or `--no-start` was passed.

Useful flags: `--wheel PATH` to install a specific build, `--verify` to run
`discord --check` first (a real login, so it confirms the token, the privileged
intent and per-channel permissions before the service goes up), `--no-start` to
install without registering.

## Operating it

```bash
# Linux
journalctl -u declarative-agent-discord -f
systemctl restart declarative-agent-discord
systemctl reset-failed declarative-agent-discord   # after the start limit latches

# macOS
tail -f <prefix>/logs/discord-agent.log
launchctl kickstart -k gui/$(id -u)/com.declarative-agent.discord
launchctl print gui/$(id -u)/com.declarative-agent.discord   # last exit status
```

launchd does no log rotation. If the logs grow, add a `newsyslog.d` entry or
point `StandardOutPath` somewhere you already rotate.

## Two gotchas

**macOS LaunchAgents die at logout.** A job in `~/Library/LaunchAgents` runs
only while that user has a session, so a bot installed that way disappears when
you log out and does not come back at boot. For anything unattended, use
`--system`, which installs the same job as a LaunchDaemon with a `UserName` so
it is not running as root.

**Don't over-harden the systemd unit.** The agent's own tools write files and
shell out (`exec_command`, `write_file`, `run_script`), so `ProtectSystem=strict`,
`ReadOnlyPaths=` or `ProtectHome=` produce a bot that starts cleanly and then
fails the first time a user asks it to do something. The shipped unit stops at
`NoNewPrivileges` and `PrivateTmp`.

## Installing by hand

The two `.template` files are the single source of truth; `install.sh` only
substitutes paths into them. To do it yourself, replace the `@PREFIX@`,
`@USER@`, `@GROUP@` and `@PATH@` tokens, and for the plist either delete the
`@USER_KEYS@` line (LaunchAgent) or swap it for `UserName`/`GroupName` keys
(LaunchDaemon).

## Why not Docker

It is the natural answer — `--restart unless-stopped` is exactly this — but the
image does not currently work for Discord: `Dockerfile:25` installs the SDK
without extras, so `discord.py` is absent, and `build-docker.sh` builds
multi-platform without `--load` or `--push`, so no local `declarative-agent:latest`
is produced. Fix those two and a container with a restart policy is equivalent
to the units here.
