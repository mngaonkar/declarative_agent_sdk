#!/usr/bin/env bash
#
# Install the Discord agent as a supervised service.
#
#   Linux : systemd unit in /etc/systemd/system   (needs root)
#   macOS : LaunchAgent in ~/Library/LaunchAgents (no root), or a LaunchDaemon
#           in /Library/LaunchDaemons with --system (needs root)
#
# Re-running this upgrades the installed wheel in place and leaves agent.yaml,
# instructions.md, skills/, workspace/ and .env untouched.
#
#   ./packaging/deploy/install.sh --help

set -euo pipefail

SERVICE_NAME="declarative-agent-discord"
LABEL="com.declarative-agent.discord"
PYTHON_VERSION="3.14"          # pinned: several dependencies have no 3.15 wheels

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

info() { printf '==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

# --- options ---------------------------------------------------------------

PREFIX=""
WHEEL=""
AGENT_DIR="$REPO_ROOT/examples/discord_bot"
SERVICE_USER=""
SYSTEM_SCOPE=0
START=1
VERIFY=0

usage() {
  cat <<USAGE
Install the Discord agent as a service (systemd on Linux, launchd on macOS).

Options:
  --prefix DIR        install directory
                      (default: /opt/discord-agent on Linux,
                       \$HOME/.declarative-agent-discord on macOS)
  --wheel PATH        wheel to install (default: newest in dist/, else built)
  --agent-dir DIR     agent config to seed from (default: examples/discord_bot)
  --service-user USER account the service runs as
                      (Linux default: \$SUDO_USER, else the current user)
  --system            macOS: install a LaunchDaemon instead of a LaunchAgent,
                      so it starts at boot and survives logout (needs root)
  --no-start          install everything but do not start the service
  --verify            run 'discord --check' before starting; this logs in to
                      Discord for real to confirm the token and permissions
  -h, --help          this message
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --prefix)       PREFIX="${2:-}"; shift 2 ;;
    --wheel)        WHEEL="${2:-}"; shift 2 ;;
    --agent-dir)    AGENT_DIR="${2:-}"; shift 2 ;;
    --service-user) SERVICE_USER="${2:-}"; shift 2 ;;
    --system)       SYSTEM_SCOPE=1; shift ;;
    --no-start)     START=0; shift ;;
    --verify)       VERIFY=1; shift ;;
    -h|--help)      usage; exit 0 ;;
    *)              die "unknown option: $1 (see --help)" ;;
  esac
done

# --- platform --------------------------------------------------------------

OS="$(uname -s)"
case "$OS" in
  Linux)
    # systemctl can exist on a host that boots something else, so check for a
    # live systemd rather than just the binary.
    [ -d /run/systemd/system ] || die "this host is not running systemd; supervise the bot with whatever init it does use"
    command -v systemctl >/dev/null || die "systemctl not found"
    [ "$(id -u)" -eq 0 ] || die "writing to /etc/systemd/system needs root — re-run with sudo"
    [ "$SYSTEM_SCOPE" -eq 0 ] || warn "--system is the only mode on Linux; ignoring the flag"
    : "${PREFIX:=/opt/discord-agent}"
    # Under sudo, $USER is root; the service should run as the human instead.
    : "${SERVICE_USER:=${SUDO_USER:-root}}"
    [ "$SERVICE_USER" = "root" ] && warn "the service will run as root — pass --service-user to use an unprivileged account"
    id "$SERVICE_USER" >/dev/null 2>&1 || die "no such user: $SERVICE_USER"
    SERVICE_GROUP="$(id -gn "$SERVICE_USER")"
    ;;
  Darwin)
    if [ "$SYSTEM_SCOPE" -eq 1 ]; then
      [ "$(id -u)" -eq 0 ] || die "--system installs a LaunchDaemon in /Library/LaunchDaemons — re-run with sudo"
      : "${PREFIX:=/opt/declarative-agent-discord}"
      # A daemon runs as root unless told otherwise, and this agent executes
      # shell commands on request, so insist on a real account.
      : "${SERVICE_USER:=${SUDO_USER:-}}"
      [ -n "$SERVICE_USER" ] || die "could not determine the account to run as; pass --service-user"
    else
      # launchctl targets the domain of whoever runs it, so sudo here would
      # install into root's GUI domain, not the user's.
      [ "$(id -u)" -ne 0 ] || die "a LaunchAgent installs into your own launchd domain — re-run without sudo, or pass --system for a boot-time daemon"
      : "${PREFIX:=$HOME/.declarative-agent-discord}"
      : "${SERVICE_USER:=$(id -un)}"
    fi
    id "$SERVICE_USER" >/dev/null 2>&1 || die "no such user: $SERVICE_USER"
    SERVICE_GROUP="$(id -gn "$SERVICE_USER")"
    ;;
  *)
    die "unsupported platform: $OS (Linux and macOS only)"
    ;;
esac

info "platform : $OS"
info "prefix   : $PREFIX"
info "runs as  : $SERVICE_USER:$SERVICE_GROUP"

# --- uv --------------------------------------------------------------------

# uv is required, not merely preferred, and checking here keeps the failure
# instant. pip cannot install this package at all: the dependency set has ~30
# direct requirements with loose lower bounds, and pip 26's resolver gives up
# after about eight minutes of backtracking with
#   error: resolution-too-deep — the dependency graph is too complex
# (reproduced on Arch with Python 3.14.7 and pip 26.3). uv's resolver handles
# the same graph in seconds, which is also why CI and .python-version assume it.
command -v uv >/dev/null || die "uv is required but not on PATH.
      Arch:   sudo pacman -S uv
      Debian: sudo apt install uv        (or the installer below)
      Any:    curl -LsSf https://astral.sh/uv/install.sh | sh
      Under sudo, check that uv is on root's PATH too — 'sudo env \"PATH=\$PATH\" $0 ...' works if it is not."

# --- wheel -----------------------------------------------------------------

if [ -z "$WHEEL" ]; then
  WHEEL="$(ls -t "$REPO_ROOT"/dist/*.whl 2>/dev/null | head -1 || true)"
fi
if [ -z "$WHEEL" ]; then
  info "no wheel in dist/ — building one"
  # --package: the repo root is a uv workspace whose member examples/simple_agent
  # is also buildable, so an unqualified build is ambiguous.
  (cd "$REPO_ROOT" && uv build --package declarative-agent-sdk --wheel --out-dir dist >/dev/null)
  WHEEL="$(ls -t "$REPO_ROOT"/dist/*.whl | head -1)"
fi
[ -f "$WHEEL" ] || die "wheel not found: $WHEEL"
WHEEL="$(cd "$(dirname "$WHEEL")" && pwd)/$(basename "$WHEEL")"
info "wheel    : $(basename "$WHEEL")"

[ -f "$AGENT_DIR/agent.yaml" ] || die "no agent.yaml in $AGENT_DIR"

# --- directories and config ------------------------------------------------

mkdir -p "$PREFIX" "$PREFIX/logs" "$PREFIX/workspace"

# Copy only what is missing: an upgrade must not discard edits to agent.yaml,
# tuned instructions, or hand-written skills.
for item in agent.yaml instructions.md skills; do
  if [ -e "$PREFIX/$item" ]; then
    info "keeping existing $item"
  elif [ -e "$AGENT_DIR/$item" ]; then
    cp -R "$AGENT_DIR/$item" "$PREFIX/$item"
    info "copied $item"
  fi
done

ENV_SEEDED=0
if [ ! -f "$PREFIX/.env" ]; then
  if [ -f "$AGENT_DIR/.env.example" ]; then
    cp "$AGENT_DIR/.env.example" "$PREFIX/.env"
  else
    : > "$PREFIX/.env"
  fi
  chmod 600 "$PREFIX/.env"
  ENV_SEEDED=1
  info "seeded .env from .env.example (0600) — fill in your keys"
else
  chmod 600 "$PREFIX/.env"
  info "keeping existing .env"
fi

# --- virtualenv ------------------------------------------------------------

VENV="$PREFIX/.venv"
if [ ! -x "$VENV/bin/python" ]; then
  info "creating venv (python $PYTHON_VERSION)"
  # uv fetches the interpreter if the host has no 3.14, so this works on a box
  # whose system python is older.
  uv venv --python "$PYTHON_VERSION" "$VENV" >/dev/null
fi

info "installing $(basename "$WHEEL") with the discord extra"
# The extra is what pulls in discord.py; without it the transport cannot import.
uv pip install --python "$VENV/bin/python" --upgrade "${WHEEL}[discord]" >/dev/null

BIN="$VENV/bin/declarative-agent"
[ -x "$BIN" ] || die "the wheel did not install a declarative-agent entry point"
info "installed : $("$BIN" version)"

# The service user must own what it will read, write and execute.
if [ "$(id -u)" -eq 0 ]; then
  chown -R "$SERVICE_USER:$SERVICE_GROUP" "$PREFIX"
fi

# --- can it actually start? ------------------------------------------------

# Decided before the service is registered, because launchd's RunAtLoad starts
# a job the moment it is bootstrapped — there is no "install but stay down"
# unless we skip the bootstrap itself. A placeholder .env cannot work, and
# starting anyway just burns the restart budget against Discord's login endpoint.
TOKEN_SET=0
grep -Eq '^[[:space:]]*DISCORD_BOT_TOKEN=.+' "$PREFIX/.env" 2>/dev/null && TOKEN_SET=1
grep -Eq '^[[:space:]]*DISCORD_BOT_TOKEN=(your|xxx|\.\.\.|changeme|<)' "$PREFIX/.env" 2>/dev/null && TOKEN_SET=0

if [ "$TOKEN_SET" -eq 0 ]; then
  START=0
  warn "DISCORD_BOT_TOKEN is not set in $PREFIX/.env — registering the service but leaving it stopped"
fi

if [ "$VERIFY" -eq 1 ] && [ "$TOKEN_SET" -eq 1 ]; then
  info "verifying the Discord side (this logs in for real)"
  (cd "$PREFIX" && "$BIN" discord --config "$PREFIX/agent.yaml" --check) \
    || die "--check failed; nothing has been registered yet"
fi

# --- unit file -------------------------------------------------------------

render() {
  # sed with | as the delimiter: paths contain / but never |.
  sed -e "s|@PREFIX@|$PREFIX|g" \
      -e "s|@USER@|$SERVICE_USER|g" \
      -e "s|@GROUP@|$SERVICE_GROUP|g" \
      -e "s|@PATH@|/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin|g" \
      "$1"
}

if [ "$OS" = "Linux" ]; then
  UNIT="/etc/systemd/system/$SERVICE_NAME.service"
  render "$SCRIPT_DIR/$SERVICE_NAME.service.template" > "$UNIT"
  chmod 644 "$UNIT"
  info "wrote $UNIT"
  systemctl daemon-reload
  # Clear any start-limit latch left by a previous broken install.
  systemctl reset-failed "$SERVICE_NAME" 2>/dev/null || true
  systemctl enable "$SERVICE_NAME" >/dev/null
else
  if [ "$SYSTEM_SCOPE" -eq 1 ]; then
    PLIST="/Library/LaunchDaemons/$LABEL.plist"
    DOMAIN="system"
    USER_KEYS=$(printf '    <key>UserName</key>\n    <string>%s</string>\n    <key>GroupName</key>\n    <string>%s</string>' "$SERVICE_USER" "$SERVICE_GROUP")
  else
    PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
    DOMAIN="gui/$(id -u)"
    # UserName in a LaunchAgent is meaningless: the job already runs as the
    # owner of the domain it is bootstrapped into.
    USER_KEYS=""
  fi
  mkdir -p "$(dirname "$PLIST")"
  # Splice the optional keys in by splitting the template at the marker line.
  # The obvious `awk -v keys="$USER_KEYS"` does not work: BSD awk rejects a -v
  # value containing newlines ("awk: newline in string"), so the whole --system
  # path would die here on macOS.
  TEMPLATE="$SCRIPT_DIR/$LABEL.plist.template"
  {
    render "$TEMPLATE" | awk '/^@USER_KEYS@$/ { exit } { print }'
    [ -n "$USER_KEYS" ] && printf '%s\n' "$USER_KEYS"
    render "$TEMPLATE" | awk 'past { print } /^@USER_KEYS@$/ { past = 1 }'
  } > "$PLIST"
  chmod 644 "$PLIST"
  [ "$SYSTEM_SCOPE" -eq 1 ] && chown root:wheel "$PLIST"
  plutil -lint "$PLIST" >/dev/null || die "generated plist is not valid: $PLIST"
  info "wrote $PLIST"
  if [ "$START" -eq 1 ]; then
    # bootout first so a re-run replaces the loaded job instead of erroring
    # with "service already bootstrapped".
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    launchctl bootstrap "$DOMAIN" "$PLIST"
  else
    info "not registering with launchd — RunAtLoad would start it immediately"
  fi
fi

# --- start -----------------------------------------------------------------

if [ "$START" -eq 1 ]; then
  if [ "$OS" = "Linux" ]; then
    systemctl start "$SERVICE_NAME"
    sleep 2
    systemctl is-active --quiet "$SERVICE_NAME" \
      && info "started" \
      || warn "service is not active — journalctl -u $SERVICE_NAME -n 50"
  else
    # bootstrap already started it via RunAtLoad; kickstart -k makes the restart
    # explicit so an upgrade picks up the new venv.
    launchctl kickstart -k "$DOMAIN/$LABEL"
    info "started"
  fi
fi

# --- what next -------------------------------------------------------------

echo
if [ "$ENV_SEEDED" -eq 1 ] || [ "$TOKEN_SET" -eq 0 ]; then
  cat <<NEXT
Finish the setup:
  1. edit $PREFIX/.env   (DISCORD_BOT_TOKEN, OPENAI_API_KEY, TAVILY_API_KEY)
  2. re-run this script — it keeps everything above and just starts the service
NEXT
  echo
fi

if [ "$OS" = "Linux" ]; then
  cat <<OPS
Service: $SERVICE_NAME
  start   : systemctl start $SERVICE_NAME
  status  : systemctl status $SERVICE_NAME
  logs    : journalctl -u $SERVICE_NAME -f
  unlatch : systemctl reset-failed $SERVICE_NAME
  remove  : $SCRIPT_DIR/uninstall.sh
OPS
else
  # Unregistered jobs need bootstrap first; a loaded one only needs kickstart.
  if [ "$START" -eq 1 ]; then
    START_CMD="launchctl kickstart -k $DOMAIN/$LABEL"
  else
    START_CMD="launchctl bootstrap $DOMAIN $PLIST"
  fi
  cat <<OPS
Service: $LABEL  (domain $DOMAIN)
  start   : $START_CMD
  status  : launchctl print $DOMAIN/$LABEL
  logs    : tail -f $PREFIX/logs/discord-agent.log
  remove  : $SCRIPT_DIR/uninstall.sh$([ "$SYSTEM_SCOPE" -eq 1 ] && echo " --system")
OPS
fi
