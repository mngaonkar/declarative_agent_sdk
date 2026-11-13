#!/usr/bin/env bash
#
# Create a lean A2A agent as a background service. Non-interactive: every input
# is a flag, so an orchestrating agent can provision sub-agents unattended.
#
# Needs nothing but this one file. The SDK is installed from GitHub, and
# agent.yaml, instructions.md, run_server.py and the service unit are generated
# from the flags. No repository checkout is involved.
#
#   ./install.sh --name researcher --provider openai --model gpt-4o-mini
#   ./install.sh --name coder --provider vllm --endpoint http://10.0.0.147:11434/v1 \
#                --model qwen2.5-coder:latest --json
#   ./install.sh --list
#
# Several agents coexist on one machine: each gets its own directory, venv
# and port, and all of them run as programs under a single supervisord that
# this script installs and starts on first use. Omit --port and the next free
# one is allocated.
#
# supervisord replaces per-agent systemd units and launchd jobs: one mechanism
# on both platforms, no root needed, and adding an agent is a config drop plus
# `supervisorctl update` rather than daemon-reload or bootstrap/kickstart.
#
# Secrets are never passed on the command line, where `ps` would expose them.
# The provider's key is read from this script's own environment, or seeded from
# a file with --env-file.
#
# Requires: uv (https://astral.sh/uv).

set -euo pipefail

PYTHON_VERSION="3.14"          # pinned: several dependencies have no 3.15 wheels
SERVICE_PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

REPO_URL="https://github.com/mngaonkar/declarative_agent_sdk"
GIT_URL="git+${REPO_URL}.git"
DEFAULT_GIT_REF="master"
DEFAULT_RELEASE="0.9.2"
DEFAULT_PORT_BASE=8000
PORT_SCAN_LIMIT=200
META_FILE=".agent-meta"

# supervisord's own home, shared by every agent this script installs.
SUPERVISOR_DIRNAME="supervisor"
BOOT_LABEL="com.declarative-agent.supervisor"
BOOT_UNIT="declarative-agent-supervisor"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$SCRIPT_DIR/uninstall.sh" ]; then
  UNINSTALL_HINT="$SCRIPT_DIR/uninstall.sh"
else
  UNINSTALL_HINT="$REPO_URL/blob/master/packaging/deploy/uninstall.sh"
fi

# Progress goes to stderr so that --json owns stdout and a caller can parse it
# without filtering.
info() { printf '%s\n' "$*" >&2; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

# --- platform --------------------------------------------------------------

OS="$(uname -s)"
IS_ROOT=0
[ "$(id -u)" -eq 0 ] && IS_ROOT=1

case "$OS" in
  Linux)
    if [ "$IS_ROOT" -eq 1 ]; then
      AGENTS_ROOT="/opt/declarative-agent"
    else
      AGENTS_ROOT="${XDG_DATA_HOME:-$HOME/.local/share}/declarative-agent"
    fi
    ;;
  Darwin)
    if [ "$IS_ROOT" -eq 1 ]; then
      AGENTS_ROOT="/opt/declarative-agent"
    else
      # Not ~/Library/Application Support: a space in the path would need
      # escaping in the plist, the unit and every command that follows.
      AGENTS_ROOT="$HOME/.declarative-agent"
    fi
    ;;
  *)
    die "unsupported platform: $OS (Linux and macOS only)"
    ;;
esac

SUPERVISOR_HOME="$AGENTS_ROOT/$SUPERVISOR_DIRNAME"
SUPERVISOR_VENV="$SUPERVISOR_HOME/.venv"
SUPERVISOR_CONF="$SUPERVISOR_HOME/supervisord.conf"
SUPERVISOR_CONFD="$SUPERVISOR_HOME/conf.d"
SUPERVISOR_SOCK="$SUPERVISOR_HOME/s.sock"

# --- options ---------------------------------------------------------------

NAME=""
DESCRIPTION=""
FRAMEWORK="lean"
PROVIDER="openai"
MODEL=""
ENDPOINT_URL=""
ENDPOINT_SET=0
BIND_HOST="0.0.0.0"
ADVERTISE_HOST=""
PORT=""
PORT_BASE="$DEFAULT_PORT_BASE"
PREFIX=""
SOURCE_KIND="git"
GIT_REF="$DEFAULT_GIT_REF"
RELEASE_VERSION="$DEFAULT_RELEASE"
INSTRUCTIONS=""
INSTRUCTIONS_FILE=""
TOOLS="web_request,exec_command"
APPROVAL="true"
ENV_FILE=""
SERVICE=1
START=1
ENABLE_AT_BOOT=0
FORCE=0
JSON=0
LIST=0
SERVICE_USER=""

usage() {
  cat <<USAGE
Create a lean A2A agent as a background service.

  install.sh --name NAME [options]
  install.sh --list

Identity:
  --name NAME            required; [a-z0-9_-], used for the directory, the
                         service name and the metadata key
  --description TEXT     one line for the agent card
  --prefix DIR           install directory (default $AGENTS_ROOT/NAME)

Model:
  --framework NAME       lean | adk | deepagent              (default lean)
  --provider NAME        openai | google | anthropic | vllm  (default openai)
  --model NAME           default depends on the provider
  --endpoint URL         default depends on the provider

Serving:
  --host ADDR            bind address (default 0.0.0.0, every interface).
                         127.0.0.1 keeps the agent reachable only from this
                         machine.
  --advertise-host ADDR  address or hostname published in the agent card and
                         returned as "url". Defaults to this host's primary
                         address, which is what other machines should dial.
                         Set it explicitly behind NAT, Docker or a proxy.
  --port N               listen port; omit to allocate the next free one
  --port-base N          where allocation starts (default $DEFAULT_PORT_BASE)

Behaviour:
  --instructions TEXT    body of instructions.md
  --instructions-file F  copy F as instructions.md instead
  --tools LIST           comma-separated built-ins
                         (default $TOOLS)
  --no-approval          run tools without asking the client first

Install:
  --source KIND          git | release                       (default git)
  --ref REF              git ref when --source git           (default $DEFAULT_GIT_REF)
  --version V            version when --source release       (default $DEFAULT_RELEASE)
  --env-file F           seed .env from F (0600); otherwise the provider's key
                         is taken from this script's environment
  --service-user USER    account the service runs as (default \$SUDO_USER or you)
  --no-service           provision the directory but register nothing with
                         supervisord
  --no-start             register the program but leave it stopped
  --enable-at-boot       also make supervisord itself start at boot. This is
                         the one place a platform service is still used, and
                         it is created once, not per agent. May be passed on
                         its own, without --name.
  --force                overwrite an existing agent.yaml and instructions.md
  --json                 print one JSON result object on stdout
  -h, --help             this message

Exit status 0 when the agent is ready, 3 when it was installed but cannot
start (missing credentials, bad config), 1 for a usage or install failure.
With --json the object is printed in the ready and not-ready cases alike, with
"ready" and "problems" saying which.
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --name)              NAME="${2:-}"; shift 2 ;;
    --description)       DESCRIPTION="${2:-}"; shift 2 ;;
    --prefix)            PREFIX="${2:-}"; shift 2 ;;
    --framework)         FRAMEWORK="${2:-}"; shift 2 ;;
    --provider)          PROVIDER="${2:-}"; shift 2 ;;
    --model)             MODEL="${2:-}"; shift 2 ;;
    --endpoint)          ENDPOINT_URL="${2:-}"; ENDPOINT_SET=1; shift 2 ;;
    --host)              BIND_HOST="${2:-}"; shift 2 ;;
    --advertise-host)    ADVERTISE_HOST="${2:-}"; shift 2 ;;
    --port)              PORT="${2:-}"; shift 2 ;;
    --port-base)         PORT_BASE="${2:-}"; shift 2 ;;
    --instructions)      INSTRUCTIONS="${2:-}"; shift 2 ;;
    --instructions-file) INSTRUCTIONS_FILE="${2:-}"; shift 2 ;;
    --tools)             TOOLS="${2:-}"; shift 2 ;;
    --no-approval)       APPROVAL="false"; shift ;;
    --source)            SOURCE_KIND="${2:-}"; shift 2 ;;
    --ref)               GIT_REF="${2:-}"; shift 2 ;;
    --version)           RELEASE_VERSION="${2:-}"; shift 2 ;;
    --env-file)          ENV_FILE="${2:-}"; shift 2 ;;
    --service-user)      SERVICE_USER="${2:-}"; shift 2 ;;
    --no-service)        SERVICE=0; shift ;;
    --enable-at-boot)    ENABLE_AT_BOOT=1; shift ;;
    --no-start)          START=0; shift ;;
    --force)             FORCE=1; shift ;;
    --json)              JSON=1; shift ;;
    --list)              LIST=1; shift ;;
    -h|--help)           usage; exit 0 ;;
    *)                   die "unknown option: $1 (see --help)" ;;
  esac
done

# --- json helper -----------------------------------------------------------

json_escape() {
  local s="$1"
  s="${s//\\/\\\\}"
  s="${s//\"/\\\"}"
  s="${s//$'\n'/\\n}"
  s="${s//$'\t'/\\t}"
  printf '%s' "$s"
}

# --- installed agents ------------------------------------------------------

# Each agent records its own metadata, so this script can answer "what exists"
# and "which ports are taken" without a central registry to keep in sync.
list_agents_json() {
  local first=1 meta
  printf '['
  for meta in "$AGENTS_ROOT"/*/"$META_FILE"; do
    [ -f "$meta" ] || continue
    [ "$first" -eq 1 ] || printf ','
    first=0
    printf '%s' "$(cat "$meta")"
  done
  printf ']\n'
}

recorded_ports() {
  local meta
  for meta in "$AGENTS_ROOT"/*/"$META_FILE"; do
    [ -f "$meta" ] || continue
    sed -n 's/.*"port"[[:space:]]*:[[:space:]]*\([0-9]\{1,\}\).*/\1/p' "$meta"
  done
}

port_is_listening() {
  # A bash builtin probe: no nc, no lsof, identical on both platforms.
  if (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; then
    exec 3<&- 3>&- 2>/dev/null || true
    return 0
  fi
  return 1
}

if [ "$LIST" -eq 1 ]; then
  list_agents_json
  exit 0
fi

# --- who runs it -----------------------------------------------------------

SERVICE_KIND="none"
[ "$SERVICE" -eq 1 ] && SERVICE_KIND="supervisor"

if [ -z "$SERVICE_USER" ]; then
  SERVICE_USER="$(id -un)"
  if [ "$IS_ROOT" -eq 1 ]; then
    # Under sudo the human is SUDO_USER; running an agent that executes shell
    # commands as root is a poor default.
    SERVICE_USER="${SUDO_USER:-root}"
    [ "$SERVICE_USER" = "root" ] && warn "no SUDO_USER in the environment — the service will run as root"
  fi
fi
id "$SERVICE_USER" >/dev/null 2>&1 || die "no such user: $SERVICE_USER"
SERVICE_GROUP="$(id -gn "$SERVICE_USER")"

# --- supervisord -----------------------------------------------------------

sctl() { "$SUPERVISOR_VENV/bin/supervisorctl" -c "$SUPERVISOR_CONF" "$@"; }

write_supervisor_conf() {
  mkdir -p "$SUPERVISOR_HOME" "$SUPERVISOR_CONFD"
  cat > "$SUPERVISOR_CONF" <<SUPEOF
; Generated by install.sh. Per-agent programs live in conf.d/ and are added
; with \`supervisorctl reread && supervisorctl update\`.

[unix_http_server]
file=$SUPERVISOR_SOCK
chmod=0700

; No [inet_http_server]: it has no authentication by default, which would be a
; remote process-control endpoint sitting next to an already exposed agent.

[supervisord]
logfile=$SUPERVISOR_HOME/supervisord.log
logfile_maxbytes=10MB
logfile_backups=3
pidfile=$SUPERVISOR_HOME/supervisord.pid
childlogdir=$SUPERVISOR_HOME
nodaemon=false

[rpcinterface:supervisor]
supervisor.rpcinterface_factory = supervisor.rpcinterface:make_main_rpcinterface

[supervisorctl]
serverurl=unix://$SUPERVISOR_SOCK

[include]
files = $SUPERVISOR_CONFD/*.conf
SUPEOF
}

supervisor_running() { sctl pid >/dev/null 2>&1; }

# Everything supervisord needs on disk, without starting it. The boot path
# installs the files but must leave starting to systemd or launchd.
ensure_supervisor_installed() {
  # A unix socket path is capped around 104 bytes by the kernel; a deep
  # --prefix would otherwise fail at bind time with a confusing error.
  if [ "${#SUPERVISOR_SOCK}" -gt 100 ]; then
    die "the supervisord socket path is too long for AF_UNIX (${#SUPERVISOR_SOCK} chars):
    $SUPERVISOR_SOCK
  Use a shorter agents root, e.g. --prefix under /opt."
  fi

  if [ ! -x "$SUPERVISOR_VENV/bin/supervisord" ]; then
    info "  installing supervisord"
    uv venv --python "$PYTHON_VERSION" "$SUPERVISOR_VENV" >/dev/null 2>&1 \
      || die "could not create the supervisord venv at $SUPERVISOR_VENV"
    uv pip install --python "$SUPERVISOR_VENV/bin/python" supervisor >/dev/null 2>&1 \
      || die "could not install supervisor into $SUPERVISOR_VENV"
  fi

  [ -f "$SUPERVISOR_CONF" ] || { write_supervisor_conf; info "  wrote $SUPERVISOR_CONF"; }
}

wait_for_supervisor() {
  # It daemonises before the socket is ready, so wait rather than racing the
  # first supervisorctl call.
  for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
    supervisor_running && return 0
    sleep 0.5
  done
  return 1
}

ensure_supervisor() {
  ensure_supervisor_installed

  if ! supervisor_running; then
    "$SUPERVISOR_VENV/bin/supervisord" -c "$SUPERVISOR_CONF" \
      || die "supervisord failed to start; see $SUPERVISOR_HOME/supervisord.log"
    wait_for_supervisor || die "supervisord started but its socket never appeared"
    info "  started supervisord"
  fi
}

# The single place a platform service is still involved: keeping supervisord
# itself alive across reboots. One entry for the whole machine.
enable_at_boot() {
  ensure_supervisor_installed
  local cmd="$SUPERVISOR_VENV/bin/supervisord -c $SUPERVISOR_CONF -n"

  # Only one supervisord can hold the socket. If this script already started
  # one, shut it down first and let the boot service own it, or the service
  # crash-loops on "address already in use" behind KeepAlive/Restart=always.
  # Agents stop with it and come back when the service starts them.
  if supervisor_running; then
    info "handing the running supervisord over to the boot service"
    sctl shutdown >/dev/null 2>&1 || true
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      supervisor_running || break
      sleep 0.5
    done
  fi
  if [ "$OS" = "Linux" ]; then
    if [ "$IS_ROOT" -eq 1 ]; then
      local unit="/etc/systemd/system/$BOOT_UNIT.service"
      cat > "$unit" <<BOOTEOF
[Unit]
Description=supervisord for declarative-agent sub-agents
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
ExecStart=$cmd
Restart=always
RestartSec=5s

[Install]
WantedBy=multi-user.target
BOOTEOF
      systemctl daemon-reload
      systemctl enable "$BOOT_UNIT" >/dev/null
      info "enabled $unit (system-wide)"
    else
      # A --user unit needs no root; lingering is what makes it start at boot
      # rather than at login.
      local dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
      mkdir -p "$dir"
      cat > "$dir/$BOOT_UNIT.service" <<BOOTEOF
[Unit]
Description=supervisord for declarative-agent sub-agents

[Service]
Type=simple
ExecStart=$cmd
Restart=always
RestartSec=5s

[Install]
WantedBy=default.target
BOOTEOF
      systemctl --user daemon-reload 2>/dev/null || true
      systemctl --user enable "$BOOT_UNIT" >/dev/null 2>&1 || true
      info "enabled $dir/$BOOT_UNIT.service (user scope)"
      if command -v loginctl >/dev/null && [ "$(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null)" != "yes" ]; then
        warn "user services only start at login until lingering is on:
         sudo loginctl enable-linger $(id -un)"
      fi
    fi
  else
    local plist
    if [ "$IS_ROOT" -eq 1 ]; then
      plist="/Library/LaunchDaemons/$BOOT_LABEL.plist"
    else
      plist="$HOME/Library/LaunchAgents/$BOOT_LABEL.plist"
    fi
    mkdir -p "$(dirname "$plist")"
    cat > "$plist" <<BOOTEOF
<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0">
<dict>
    <key>Label</key><string>$BOOT_LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$SUPERVISOR_VENV/bin/supervisord</string>
        <string>-c</string>
        <string>$SUPERVISOR_CONF</string>
        <string>-n</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>EnvironmentVariables</key>
    <dict><key>PATH</key><string>$SERVICE_PATH</string></dict>
</dict>
</plist>
BOOTEOF
    chmod 644 "$plist"
    plutil -lint "$plist" >/dev/null || die "generated plist is not valid: $plist"
    if [ "$IS_ROOT" -eq 1 ]; then
      chown root:wheel "$plist"
      launchctl bootout "system/$BOOT_LABEL" 2>/dev/null || true
      launchctl bootstrap system "$plist" 2>/dev/null || true
    else
      launchctl bootout "gui/$(id -u)/$BOOT_LABEL" 2>/dev/null || true
      launchctl bootstrap "gui/$(id -u)" "$plist" 2>/dev/null || true
      warn "a LaunchAgent stops at logout; run this as root for a LaunchDaemon that survives it"
    fi
    info "enabled $plist"
  fi

  if wait_for_supervisor; then
    info "supervisord is running under the boot service"
  else
    warn "the boot service did not bring supervisord up; check $SUPERVISOR_HOME/supervisord.log"
  fi
}

if [ "$ENABLE_AT_BOOT" -eq 1 ] && [ -z "$NAME" ]; then
  enable_at_boot
  exit 0
fi

# --- validate --------------------------------------------------------------

[ -n "$NAME" ] || die "--name is required (see --help)"
case "$NAME" in
  [a-z0-9]*) ;;
  *) die "--name must start with a lowercase letter or digit: '$NAME'" ;;
esac
case "$NAME" in
  *[!a-z0-9_-]*) die "--name may only contain a-z, 0-9, '_' and '-': '$NAME'" ;;
esac
[ "${#NAME}" -le 40 ] || die "--name is limited to 40 characters"

case "$FRAMEWORK" in
  lean|adk|deepagent) ;;
  *) die "--framework must be lean, adk or deepagent: '$FRAMEWORK'" ;;
esac

case "$SOURCE_KIND" in
  git|release) ;;
  *) die "--source must be git or release: '$SOURCE_KIND'" ;;
esac

case "$PROVIDER" in
  openai)
    : "${MODEL:=gpt-4o-mini}"
    DEF_URL="https://api.openai.com/v1"; KEY_NAME="OPENAI_API_KEY" ;;
  google)
    : "${MODEL:=gemini-2.0-flash}"
    DEF_URL=""; KEY_NAME="GOOGLE_API_KEY" ;;
  anthropic)
    : "${MODEL:=claude-sonnet-4-5}"
    DEF_URL="https://api.anthropic.com/v1"; KEY_NAME="ANTHROPIC_API_KEY" ;;
  vllm)
    : "${MODEL:=Qwen/Qwen3-4B}"
    DEF_URL="http://localhost:8000/v1"; KEY_NAME="" ;;
  *)
    die "--provider must be openai, google, anthropic or vllm: '$PROVIDER'" ;;
esac
[ "$ENDPOINT_SET" -eq 1 ] || ENDPOINT_URL="$DEF_URL"

: "${DESCRIPTION:=Lean A2A agent $NAME}"
: "${PREFIX:=$AGENTS_ROOT/$NAME}"
case "$PREFIX" in
  /*) ;;
  *) PREFIX="$PWD/$PREFIX" ;;
esac

if [ -n "$INSTRUCTIONS_FILE" ]; then
  [ -f "$INSTRUCTIONS_FILE" ] || die "--instructions-file not found: $INSTRUCTIONS_FILE"
fi
if [ -n "$ENV_FILE" ]; then
  [ -f "$ENV_FILE" ] || die "--env-file not found: $ENV_FILE"
fi

command -v uv >/dev/null || die "uv is required but not on PATH.
  Arch: sudo pacman -S uv | Debian: sudo apt install uv | macOS: brew install uv
  Any:  curl -LsSf https://astral.sh/uv/install.sh | sh
  pip is not an alternative: it cannot resolve this dependency set and exits
  with 'resolution-too-deep' after several minutes."

PROGRAM="$NAME"

# --- port ------------------------------------------------------------------

mkdir -p "$AGENTS_ROOT"

EXISTING_PORT=""
if [ -f "$PREFIX/$META_FILE" ]; then
  EXISTING_PORT="$(sed -n 's/.*"port"[[:space:]]*:[[:space:]]*\([0-9]\{1,\}\).*/\1/p' "$PREFIX/$META_FILE" | head -1)"
fi

if [ -n "$PORT" ]; then
  case "$PORT" in
    ''|*[!0-9]*) die "--port must be a number: '$PORT'" ;;
  esac
  # A port held by a *different* agent is a configuration error worth refusing:
  # both services would crash-loop fighting over the socket.
  while IFS= read -r taken; do
    [ -n "$taken" ] || continue
    [ "$taken" = "$PORT" ] || continue
    [ "$taken" = "$EXISTING_PORT" ] && continue
    die "port $PORT is already recorded by another installed agent (see --list)"
  done <<EOF
$(recorded_ports)
EOF
elif [ -n "$EXISTING_PORT" ]; then
  # Re-installing the same agent keeps its port so clients keep working.
  PORT="$EXISTING_PORT"
  info "reusing port $PORT from the existing install"
else
  case "$PORT_BASE" in
    ''|*[!0-9]*) die "--port-base must be a number: '$PORT_BASE'" ;;
  esac
  TAKEN=" $(recorded_ports | tr '\n' ' ') "
  candidate="$PORT_BASE"
  limit=$((PORT_BASE + PORT_SCAN_LIMIT))
  while [ "$candidate" -lt "$limit" ]; do
    if [ "${TAKEN#* $candidate }" != "$TAKEN" ]; then
      candidate=$((candidate + 1)); continue
    fi
    if port_is_listening "$candidate"; then
      candidate=$((candidate + 1)); continue
    fi
    PORT="$candidate"
    break
  done
  [ -n "$PORT" ] || die "no free port between $PORT_BASE and $limit"
  info "allocated port $PORT"
fi

# --- existing install ------------------------------------------------------

REGENERATE=1
if [ -f "$PREFIX/agent.yaml" ] && [ "$FORCE" -eq 0 ]; then
  REGENERATE=0
  info "keeping existing agent.yaml and instructions.md (--force to replace)"
fi

# --- source ----------------------------------------------------------------

if [ "$SOURCE_KIND" = "git" ]; then
  WHEEL_SPEC="declarative-agent-sdk @ ${GIT_URL}@${GIT_REF}"
  SOURCE_LABEL="git:$GIT_REF"
else
  RELEASE_VERSION="${RELEASE_VERSION#v}"
  WHEEL_SPEC="declarative-agent-sdk @ $REPO_URL/releases/download/v${RELEASE_VERSION}/declarative_agent_sdk-${RELEASE_VERSION}-py3-none-any.whl"
  SOURCE_LABEL="release:$RELEASE_VERSION"
fi

info "creating agent '$NAME' at $PREFIX (port $PORT, $SOURCE_LABEL, service $SERVICE_KIND)"

# --- write the agent -------------------------------------------------------

mkdir -p "$PREFIX" "$PREFIX/logs" "$PREFIX/skills" "$PREFIX/workspace"

if [ "$REGENERATE" -eq 1 ]; then
  {
    echo "# Generated by install.sh for agent '$NAME' on $(date -u '+%Y-%m-%d %H:%M:%SZ')."
    echo "# Re-running without --force keeps this file."
    echo
    echo "name: $NAME"
    echo "description: $DESCRIPTION"
    echo
    echo "# One end-to-end loop, no mixing: lean | adk | deepagent."
    echo "agent_framework: $FRAMEWORK"
    echo
    echo "instruction_file: instructions.md"
    echo "skills_directory: skills"
    echo "workspace_directory: workspace"
    echo
    echo "provider: $PROVIDER"
    echo "model: $MODEL"
    if [ -n "$ENDPOINT_URL" ]; then
      echo "endpoint:"
      echo "  url: $ENDPOINT_URL"
    fi
    echo
    echo "tools:"
    printf '%s\n' "$TOOLS" | tr ',' '\n' | while IFS= read -r tool; do
      tool="$(printf '%s' "$tool" | tr -d '[:space:]')"
      [ -n "$tool" ] && echo "  - $tool"
    done
    echo
    echo "tools_approval_required: $APPROVAL"
  } > "$PREFIX/agent.yaml"
  info "  wrote agent.yaml"

  if [ -n "$INSTRUCTIONS_FILE" ]; then
    cp "$INSTRUCTIONS_FILE" "$PREFIX/instructions.md"
    info "  wrote instructions.md from $INSTRUCTIONS_FILE"
  elif [ -n "$INSTRUCTIONS" ]; then
    printf '%s\n' "$INSTRUCTIONS" > "$PREFIX/instructions.md"
    info "  wrote instructions.md"
  else
    {
      echo "# $NAME"
      echo
      echo "$DESCRIPTION"
      echo
      echo "## How to answer"
      echo
      echo "- Be direct. Lead with the answer, then the reasoning that matters."
      echo "- Say when you are unsure, and say what would settle it."
      echo "- Prefer checking something over guessing: you have tools, use them."
      echo
      echo "## Files"
      echo
      echo "Write anything you produce into \`workspace/\`. Paths outside it are"
      echo "not yours to modify."
    } > "$PREFIX/instructions.md"
    info "  wrote instructions.md (default)"
  fi
fi

# run_server.py is generated, not copied: there is no checkout to copy from,
# and `declarative-agent a2a` is deliberately not wired up (cli/main.py returns
# 2 and says to use AIAgentServer directly).
cat > "$PREFIX/run_server.py" <<'PYEOF'
#!/usr/bin/env python3
"""Serve this agent over A2A (JSON-RPC + agent card).

Generated by install.sh and rewritten on every run, so put local changes in
agent.yaml or instructions.md instead.

    .venv/bin/python run_server.py        # HOST and PORT override the defaults
"""

from __future__ import annotations

import os
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Credentials live in .env beside this file; the SDK's loader reads it, so they
# never appear in the unit, the plist or the process arguments.
env_file = HERE / ".env"
if env_file.is_file():
    try:
        from dotenv import load_dotenv

        load_dotenv(env_file)
    except ImportError:  # python-dotenv is a core dependency, so this is odd
        print(f"note: {env_file} not loaded - python-dotenv is missing")

from declarative_agent_sdk import AgentFactory, AgentRegistry, AIAgentServer
from declarative_agent_sdk.core.agent_logging import get_logger

logger = get_logger(__name__)


def main() -> None:
    # Relative paths in agent.yaml resolve against the YAML's own directory, so
    # this works from any working directory.
    os.chdir(HERE)

    host = os.environ.get("HOST", "__BIND_HOST__")
    port = int(os.environ.get("PORT", "__BIND_PORT__"))

    agent = AgentFactory.from_yaml_file(str(HERE / "agent.yaml"))

    # Publish the URL the installer worked out. AIAgentServer would otherwise
    # fill this in by probing the default route, which cannot know about NAT,
    # Docker or a reverse proxy - and for a 0.0.0.0 bind there is no single
    # right answer to guess.
    card = getattr(agent, "agent_card", None)
    interfaces = getattr(card, "supported_interfaces", None) if card else None
    if interfaces is not None:
        del interfaces[:]
        from a2a.types import AgentInterface
        from a2a.utils.constants import (
            PROTOCOL_VERSION_CURRENT,
            TransportProtocol,
        )

        interfaces.append(
            AgentInterface(
                url="__ADVERTISE_URL__",
                protocol_binding=TransportProtocol.JSONRPC.value,
                protocol_version=PROTOCOL_VERSION_CURRENT,
            )
        )

    AgentRegistry.register(agent, category="a2a")
    logger.info("serving %s on %s:%s", agent.name, host, port)
    print(f"A2A agent '{agent.name}' on http://{host}:{port}/")
    AIAgentServer(agent, host=host, port=port).run()


if __name__ == "__main__":
    main()
PYEOF
# Substituted rather than interpolated so the heredoc stays quoted and nothing
# in the Python source expands.
sed -i.bak -e "s|__BIND_HOST__|$BIND_HOST|" -e "s|__BIND_PORT__|$PORT|" "$PREFIX/run_server.py"
rm -f "$PREFIX/run_server.py.bak"
chmod +x "$PREFIX/run_server.py"
info "  wrote run_server.py"

# --- credentials -----------------------------------------------------------

if [ -n "$ENV_FILE" ]; then
  cp "$ENV_FILE" "$PREFIX/.env"
  chmod 600 "$PREFIX/.env"
  info "  seeded .env from $ENV_FILE (0600)"
elif [ ! -f "$PREFIX/.env" ]; then
  {
    echo "# Credentials for '$NAME', loaded by run_server.py at startup. 0600."
    if [ -n "$KEY_NAME" ]; then
      # Inherited from this script's environment rather than a flag: an API key
      # in argv is visible to every user on the box through `ps`.
      if [ -n "${!KEY_NAME:-}" ]; then
        echo "$KEY_NAME=${!KEY_NAME}"
      else
        echo "# $KEY_NAME="
      fi
    fi
    echo "# TAVILY_API_KEY=   # only if tavily_search is in the tools list"
  } > "$PREFIX/.env"
  chmod 600 "$PREFIX/.env"
  if [ -n "$KEY_NAME" ] && [ -n "${!KEY_NAME:-}" ]; then
    info "  wrote .env with $KEY_NAME from the environment (0600)"
  else
    info "  wrote .env (0600)"
  fi
else
  chmod 600 "$PREFIX/.env"
  info "  kept existing .env"
fi

# --- install the SDK -------------------------------------------------------

VENV="$PREFIX/.venv"
if [ ! -x "$VENV/bin/python" ]; then
  # uv downloads the interpreter if the host has no 3.14.
  uv venv --python "$PYTHON_VERSION" "$VENV" >/dev/null 2>&1 \
    || die "could not create a python $PYTHON_VERSION venv at $VENV"
  info "  created venv on python $PYTHON_VERSION"
fi

uv pip install --python "$VENV/bin/python" --upgrade "$WHEEL_SPEC" >/dev/null 2>&1 \
  || die "installing '$WHEEL_SPEC' failed"
SDK_VERSION="$("$VENV/bin/declarative-agent" version 2>/dev/null | awk '{print $2}')"
info "  installed SDK ${SDK_VERSION:-unknown}"

if [ "$IS_ROOT" -eq 1 ]; then
  chown -R "$SERVICE_USER:$SERVICE_GROUP" "$PREFIX"
fi

# --- advertised address ----------------------------------------------------
#
# The bind address and the published address are different things: 0.0.0.0
# means "listen on every interface", but it is not an address a client can
# dial, so it must never appear in the agent card or in "url".

if [ -z "$ADVERTISE_HOST" ]; then
  case "$BIND_HOST" in
    0.0.0.0|::|"")
      # A UDP connect() reserves a route without sending a packet, and
      # getsockname then reports which local address the kernel picked for it.
      # Same technique the SDK uses for its own card, so the two agree.
      ADVERTISE_HOST="$("$VENV/bin/python" -c '
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.connect(("8.8.8.8", 80))
    print(s.getsockname()[0])
except OSError:
    pass
finally:
    s.close()
' 2>/dev/null || true)"
      if [ -z "$ADVERTISE_HOST" ]; then
        # No default route. Fall back to the primary interface address, then to
        # the hostname, and say so: either can be wrong on a multi-homed box.
        if [ "$OS" = "Linux" ]; then
          ADVERTISE_HOST="$(hostname -I 2>/dev/null | awk '{print $1}')"
        else
          ADVERTISE_HOST="$(ipconfig getifaddr en0 2>/dev/null || true)"
        fi
        [ -n "$ADVERTISE_HOST" ] || ADVERTISE_HOST="$(hostname 2>/dev/null || true)"
        [ -n "$ADVERTISE_HOST" ] \
          || die "could not determine this host's address; pass --advertise-host"
        warn "no default route; advertising '$ADVERTISE_HOST' - pass --advertise-host if that is not reachable"
      fi
      ;;
    *)
      # An explicit bind address is also the right thing to publish.
      ADVERTISE_HOST="$BIND_HOST"
      ;;
  esac
fi

ADVERTISE_URL="http://$ADVERTISE_HOST:$PORT/"
info "  binding $BIND_HOST:$PORT, advertising $ADVERTISE_URL"

if [ "$BIND_HOST" = "0.0.0.0" ]; then
  warn "bound to every interface: the A2A endpoint has no authentication and
         wide-open CORS, and this agent can run shell commands. Restrict it
         with a firewall, or pass --host <private-addr> to bind one interface."
fi

# run_server.py publishes exactly this URL, so the card does not depend on the
# SDK re-running the route probe at boot (which would also ignore NAT).
sed -i.bak -e "s|__ADVERTISE_URL__|$ADVERTISE_URL|" "$PREFIX/run_server.py"
rm -f "$PREFIX/run_server.py.bak"

# --- can it start? ---------------------------------------------------------

# The SDK's own preflight, so the credential demanded follows agent.yaml's
# provider instead of a name hardcoded here. First line only: the full text is
# multi-line advice meant for a human.
PROBLEMS="$(
  "$VENV/bin/python" - "$PREFIX/agent.yaml" <<'PY' 2>&1 || true
import contextlib, io, sys
from pathlib import Path

from declarative_agent_sdk.cli.main import load_dotenv_if_present, preflight
from declarative_agent_sdk.core.agent_factory import AgentFactory

config_path = Path(sys.argv[1])
with contextlib.redirect_stdout(io.StringIO()):
    load_dotenv_if_present(None, config_path)
    config = AgentFactory.load_config(str(config_path))

for problem in preflight(config, require_discord_token=False):
    print(problem.splitlines()[0])
PY
)"

BUILD_ERR=""
if ! BUILD_OUT="$(cd "$PREFIX" && "$VENV/bin/python" -c "
from declarative_agent_sdk import AgentFactory
print(AgentFactory.from_yaml_file('agent.yaml').name)
" 2>&1)"; then
  BUILD_ERR="$(printf '%s' "$BUILD_OUT" | tail -1)"
  warn "the agent could not be built from $PREFIX/agent.yaml: $BUILD_ERR"
fi

READY=true
if [ -n "$PROBLEMS" ] || [ -n "$BUILD_ERR" ]; then
  READY=false
  START=0
fi

# --- metadata --------------------------------------------------------------
#
# Written before the service so --list sees the agent even when registration
# fails, and so installing a sibling agent later avoids this port.

SERVICE_ID=""
[ "$SERVICE_KIND" = "supervisor" ] && SERVICE_ID="$PROGRAM"

write_meta() {
  cat > "$PREFIX/$META_FILE" <<META
{"name":"$(json_escape "$NAME")",
 "prefix":"$(json_escape "$PREFIX")",
 "bind_host":"$(json_escape "$BIND_HOST")",
 "advertise_host":"$(json_escape "$ADVERTISE_HOST")",
 "port":$PORT,
 "url":"$(json_escape "$ADVERTISE_URL")",
 "agent_card":"$(json_escape "${ADVERTISE_URL}.well-known/agent-card.json")",
 "framework":"$(json_escape "$FRAMEWORK")",
 "provider":"$(json_escape "$PROVIDER")",
 "model":"$(json_escape "$MODEL")",
 "service_kind":"$(json_escape "$SERVICE_KIND")",
 "service_id":"$(json_escape "$SERVICE_ID")",
 "sdk_version":"$(json_escape "${SDK_VERSION:-unknown}")",
 "sdk_source":"$(json_escape "$SOURCE_LABEL")",
 "ready":$READY,
 "updated":"$(date -u '+%Y-%m-%dT%H:%M:%SZ')"}
META
  [ "$IS_ROOT" -eq 1 ] && chown "$SERVICE_USER:$SERVICE_GROUP" "$PREFIX/$META_FILE"
  return 0
}
write_meta

# --- register with supervisord ---------------------------------------------

SERVICE_STATE="not-registered"
PROGRAM_CONF="$SUPERVISOR_CONFD/$NAME.conf"

if [ "$SERVICE_KIND" = "supervisor" ]; then
  ensure_supervisor

  # `user=` only means anything when supervisord itself runs as root; setting
  # it otherwise makes supervisor refuse to start the program.
  USER_LINE=""
  [ "$IS_ROOT" -eq 1 ] && USER_LINE="user=$SERVICE_USER"

  # % is the interpolation character in supervisor configs, so any literal one
  # in a path or value has to be doubled.
  esc() { printf '%s' "$1" | sed 's/%/%%/g'; }

  cat > "$PROGRAM_CONF" <<PROGEOF
; Generated by install.sh for agent '$NAME'. Overwritten on reinstall.
[program:$NAME]
command=$(esc "$VENV/bin/python") $(esc "$PREFIX/run_server.py")
directory=$(esc "$PREFIX")
$USER_LINE

autostart=true
autorestart=true
; Must stay up this long before supervisor calls the start a success. The SDK
; imports and registers its tools first, which takes a few seconds.
startsecs=8
; After this many failed starts the program goes FATAL and is left alone,
; which is what stops a missing credential becoming an endless restart loop.
startretries=3
exitcodes=0

stopsignal=TERM
; serve() waits up to 10s for a graceful shutdown; give it room.
stopwaitsecs=20
; The agent's own tools spawn children (exec_command, run_script); without
; these, a stop leaves them orphaned and holding the port.
stopasgroup=true
killasgroup=true

stdout_logfile=$(esc "$PREFIX/logs/$NAME.log")
stderr_logfile=$(esc "$PREFIX/logs/$NAME.err.log")
stdout_logfile_maxbytes=10MB
stdout_logfile_backups=3
stderr_logfile_maxbytes=10MB
stderr_logfile_backups=3

environment=PYTHONUNBUFFERED="1",HOST="$(esc "$BIND_HOST")",PORT="$PORT",PATH="$(esc "$SERVICE_PATH")"
PROGEOF
  info "  wrote $PROGRAM_CONF"

  sctl reread >/dev/null 2>&1 || true
  if [ "$START" -eq 1 ]; then
    # `update` adds new programs and restarts changed ones; an unchanged
    # program needs an explicit restart to pick up a rebuilt venv.
    sctl update >/dev/null 2>&1 || true
    sctl restart "$NAME" >/dev/null 2>&1 || sctl start "$NAME" >/dev/null 2>&1 || true
    SERVICE_STATE="$(sctl status "$NAME" 2>/dev/null | awk '{print tolower($2)}')"
    [ -n "$SERVICE_STATE" ] || SERVICE_STATE="unknown"
    if [ "$SERVICE_STATE" = "running" ]; then
      info "  program running"
    else
      warn "program state is $SERVICE_STATE — $SUPERVISOR_VENV/bin/supervisorctl -c $SUPERVISOR_CONF tail -100 $NAME stderr"
    fi
  else
    # Registered but deliberately not started: `update` would start it, so
    # only make supervisor aware of the config.
    SERVICE_STATE="registered"
    info "  registered but not started"
  fi
fi

if [ "$ENABLE_AT_BOOT" -eq 1 ]; then
  enable_at_boot
fi

# --- result ----------------------------------------------------------------

SCTL="$SUPERVISOR_VENV/bin/supervisorctl -c $SUPERVISOR_CONF"

if [ "$JSON" -eq 1 ]; then
  problems_json="["
  if [ -n "$PROBLEMS" ]; then
    first=1
    while IFS= read -r line; do
      [ -n "$line" ] || continue
      [ "$first" -eq 1 ] || problems_json="$problems_json,"
      first=0
      problems_json="$problems_json\"$(json_escape "$line")\""
    done <<EOF
$PROBLEMS
EOF
  fi
  if [ -n "$BUILD_ERR" ]; then
    [ "$problems_json" = "[" ] || problems_json="$problems_json,"
    problems_json="$problems_json\"$(json_escape "$BUILD_ERR")\""
  fi
  problems_json="$problems_json]"

  cat <<JSON
{"name":"$(json_escape "$NAME")",
 "prefix":"$(json_escape "$PREFIX")",
 "bind_host":"$(json_escape "$BIND_HOST")",
 "advertise_host":"$(json_escape "$ADVERTISE_HOST")",
 "port":$PORT,
 "url":"$(json_escape "$ADVERTISE_URL")",
 "agent_card":"$(json_escape "${ADVERTISE_URL}.well-known/agent-card.json")",
 "framework":"$(json_escape "$FRAMEWORK")",
 "provider":"$(json_escape "$PROVIDER")",
 "model":"$(json_escape "$MODEL")",
 "service_kind":"$(json_escape "$SERVICE_KIND")",
 "service_id":"$(json_escape "$SERVICE_ID")",
 "service_state":"$(json_escape "$SERVICE_STATE")",
 "supervisor_conf":"$(json_escape "$SUPERVISOR_CONF")",
 "supervisorctl":"$(json_escape "$SCTL")",
 "sdk_version":"$(json_escape "${SDK_VERSION:-unknown}")",
 "sdk_source":"$(json_escape "$SOURCE_LABEL")",
 "run_command":"$(json_escape "$VENV/bin/python $PREFIX/run_server.py")",
 "uninstall_command":"$(json_escape "$UNINSTALL_HINT --name $NAME")",
 "ready":$READY,
 "problems":$problems_json}
JSON
else
  if [ "$READY" = "false" ]; then
    info ""
    info "agent '$NAME' installed but not started:"
    [ -n "$PROBLEMS" ] && printf '%s\n' "$PROBLEMS" | sed 's/^/  - /' >&2
    [ -n "$BUILD_ERR" ] && info "  - $BUILD_ERR"
    info ""
    info "  add the missing credentials to $PREFIX/.env and re-run this command"
  fi
  info ""
  info "agent      : $NAME"
  info "bound      : $BIND_HOST:$PORT"
  info "url        : $ADVERTISE_URL"
  info "agent card : ${ADVERTISE_URL}.well-known/agent-card.json"
  info "program    : ${SERVICE_ID:-none} ($SERVICE_STATE)"
  info "status     : $SCTL status"
  info "logs       : $SCTL tail -f $NAME"
  info "restart    : $SCTL restart $NAME"
  info "remove     : $UNINSTALL_HINT --name $NAME"
fi

[ "$READY" = "true" ] || exit 3
