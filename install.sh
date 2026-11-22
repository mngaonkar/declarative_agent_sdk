#!/usr/bin/env bash
# Install agent-board — the board that installs and runs SDK agents on your
# machines — with one command:
#
#   curl -fsSL https://raw.githubusercontent.com/mngaonkar/declarative_agent_sdk/master/install.sh | bash
#
# Options go after `bash -s --`:
#
#   curl -fsSL …/install.sh | bash -s -- --port 9000 --host 0.0.0.0
#
# What it does, as the current user (no sudo):
#   1. installs uv (https://astral.sh/uv) if it is missing;
#   2. installs the SDK, with the `declarative-agent` command, as a uv tool;
#   3. starts agent-board as a background service that comes back after a
#      restart (launchd on macOS, a systemd user unit on Linux);
#   4. waits for it to answer and prints the address.
#
# Run it again to upgrade. `--uninstall` removes the service and the tool, and
# leaves your data (~/.agent-board) alone.
#
# The whole script is one function called on the last line, so a download cut
# short cannot run half of it.

main() {
  set -euo pipefail

  local REPO_URL="https://github.com/mngaonkar/declarative_agent_sdk"
  local REF="master"
  local SOURCE=""            # a local path or URL to install from instead of git
  local HOST="127.0.0.1"
  local PORT="8900"
  local SERVICE=1
  local INSTALL_UV=1
  local UNINSTALL=0
  local DRY_RUN=0
  local PYTHON_VERSION="3.14"
  local LABEL="com.declarative-agent.board"
  local UNIT="agent-board"
  local DATA_DIR="$HOME/.agent-board"
  # Names from when agent-board was agents-control.
  local LEGACY_LABEL="com.declarative-agent.control"
  local LEGACY_UNIT="agents-control"
  # A board set up under the old name keeps using its data where it is.
  if [ ! -e "$DATA_DIR" ] && [ -f "$HOME/.agents-control/control.db" ]; then
    DATA_DIR="$HOME/.agents-control"
  fi

  info() { printf '%s\n' "$*" >&2; }
  warn() { printf 'warning: %s\n' "$*" >&2; }
  die()  { printf 'error: %s\n' "$*" >&2; exit 1; }
  run()  { if [ "$DRY_RUN" -eq 1 ]; then info "[dry-run] $*"; else "$@"; fi; }

  usage() {
    cat >&2 <<'USAGE'
Install agent-board.

  install.sh [--host ADDR] [--port N] [--ref REF] [--no-service] [--no-uv-install]
  install.sh --uninstall

  --host ADDR      address to listen on (default 127.0.0.1, this machine only).
                   Any other address needs a token; one is generated and kept
                   in ~/.agent-board/token.
  --port N         port (default 8900)
  --ref REF        git branch, tag or commit to install (default master)
  --source SPEC    install from SPEC instead of git (a local checkout or a
                   wheel), for testing the installer
  --no-service     install the command only; start it yourself with
                   `declarative-agent board`
  --no-uv-install  fail instead of installing uv when it is missing
  --uninstall      remove the service and the tool (keeps ~/.agent-board)
  --dry-run        print what would be done, change nothing
  -h, --help       this message
USAGE
  }

  while [ $# -gt 0 ]; do
    case "$1" in
      --host)          HOST="${2:-}"; shift 2 ;;
      --port)          PORT="${2:-}"; shift 2 ;;
      --ref)           REF="${2:-}"; shift 2 ;;
      --source)        SOURCE="${2:-}"; shift 2 ;;
      --no-service)    SERVICE=0; shift ;;
      --no-uv-install) INSTALL_UV=0; shift ;;
      --uninstall)     UNINSTALL=1; shift ;;
      --dry-run)       DRY_RUN=1; shift ;;
      -h|--help)       usage; exit 0 ;;
      *)               usage; die "unknown option: $1" ;;
    esac
  done

  case "$PORT" in ''|*[!0-9]*) die "--port must be a number: '$PORT'" ;; esac
  case "$HOST" in ''|*[!A-Za-z0-9.:_-]*) die "--host is not an address: '$HOST'" ;; esac
  [ "$(id -u)" -ne 0 ] || die "run this as your own user, not root: agent-board keeps
       its data and SSH keys in your home directory"

  local OS
  OS="$(uname -s)"
  case "$OS" in
    Darwin|Linux) ;;
    *) die "unsupported system '$OS' — agent-board runs on macOS and Linux" ;;
  esac

  # uv installs tools here unless told otherwise; the service needs the path.
  local BIN_DIR="${UV_TOOL_BIN_DIR:-${XDG_BIN_HOME:-$HOME/.local/bin}}"
  export PATH="$BIN_DIR:$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  local COMMAND="$BIN_DIR/declarative-agent"

  local PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
  local UNIT_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/$UNIT.service"

  # The service under its old name would hold the same port; remove it.
  remove_legacy_service() {
    local plist="$HOME/Library/LaunchAgents/$LEGACY_LABEL.plist"
    local unit_file="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/$LEGACY_UNIT.service"
    if [ "$OS" = "Darwin" ] && [ -f "$plist" ]; then
      run launchctl bootout "gui/$(id -u)/$LEGACY_LABEL" 2>/dev/null || true
      run rm -f "$plist"
      info "removed the old launchd service ($LEGACY_LABEL)"
    elif [ "$OS" = "Linux" ] && [ -f "$unit_file" ]; then
      run systemctl --user disable --now "$LEGACY_UNIT" 2>/dev/null || true
      run rm -f "$unit_file"
      run systemctl --user daemon-reload 2>/dev/null || true
      info "removed the old systemd user service ($LEGACY_UNIT)"
    fi
  }

  # ------------------------------------------------------------- uninstall

  if [ "$UNINSTALL" -eq 1 ]; then
    remove_legacy_service
    if [ "$OS" = "Darwin" ] && [ -f "$PLIST" ]; then
      run launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
      run rm -f "$PLIST"
      info "removed the launchd service"
    elif [ "$OS" = "Linux" ] && [ -f "$UNIT_FILE" ]; then
      run systemctl --user disable --now "$UNIT" 2>/dev/null || true
      run rm -f "$UNIT_FILE"
      run systemctl --user daemon-reload 2>/dev/null || true
      info "removed the systemd user service"
    fi
    if command -v uv >/dev/null 2>&1; then
      run uv tool uninstall declarative-agent-sdk >/dev/null 2>&1 || true
      info "removed the declarative-agent command"
    fi
    info "agent-board removed. Your data is still in $DATA_DIR — delete it"
    info "yourself if you no longer want the board's record of machines and agents."
    info "Agents already installed on machines keep running; remove them from the"
    info "board first, or with uninstall.sh on each machine."
    exit 0
  fi

  # ------------------------------------------------------------------ uv

  if ! command -v uv >/dev/null 2>&1; then
    [ "$INSTALL_UV" -eq 1 ] || die "uv is not installed — see https://docs.astral.sh/uv/"
    command -v curl >/dev/null 2>&1 || die "curl is needed to install uv"
    info "installing uv (https://astral.sh/uv)"
    if [ "$DRY_RUN" -eq 1 ]; then
      info "[dry-run] curl -LsSf https://astral.sh/uv/install.sh | sh"
    else
      curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh >/dev/null \
        || die "installing uv failed"
    fi
  fi
  [ "$DRY_RUN" -eq 1 ] || command -v uv >/dev/null 2>&1 \
    || die "uv was installed but is not on PATH; open a new terminal and run this again"

  # ------------------------------------------------------------- the tool

  local SPEC
  if [ -n "$SOURCE" ]; then
    SPEC="$SOURCE"
  else
    SPEC="declarative-agent-sdk @ git+${REPO_URL}.git@${REF}"
  fi
  info "installing the SDK from ${SOURCE:-$REPO_URL@$REF} (this can take a few minutes)"
  # --force: replace an earlier install, which is what an upgrade is.
  if [ "$DRY_RUN" -eq 1 ]; then
    info "[dry-run] uv tool install --force --python $PYTHON_VERSION '$SPEC'"
  else
    local OUT
    if ! OUT="$(uv tool install --force --python "$PYTHON_VERSION" "$SPEC" 2>&1)"; then
      die "installing the SDK failed:
$(printf '%s\n' "$OUT" | tail -n 20 | sed 's/^/    /')"
    fi
    [ -x "$COMMAND" ] || COMMAND="$(command -v declarative-agent || true)"
    [ -n "$COMMAND" ] && [ -x "$COMMAND" ] || die "the declarative-agent command was not found after install"
    info "  installed $("$COMMAND" version 2>/dev/null || echo declarative-agent)"
  fi

  # ---------------------------------------------------------------- token

  run mkdir -p "$DATA_DIR/logs"
  local TOKEN=""
  case "$HOST" in
    127.0.0.1|localhost|::1) ;;
    *)
      # agent-board refuses any other address without a token: it can run
      # commands on every machine you register.
      if [ -s "$DATA_DIR/token" ]; then
        TOKEN="$(cat "$DATA_DIR/token")"
      elif [ "$DRY_RUN" -eq 0 ]; then
        TOKEN="$(od -An -N24 -tx1 /dev/urandom | tr -d ' \n')"
        ( umask 077; printf '%s\n' "$TOKEN" > "$DATA_DIR/token" )
        info "  generated a token in $DATA_DIR/token"
      fi
      ;;
  esac

  if [ "$SERVICE" -eq 0 ]; then
    info ""
    info "Installed. Start it with:"
    info "  ${TOKEN:+AGENT_BOARD_TOKEN=\$(cat $DATA_DIR/token) }declarative-agent board --host $HOST --port $PORT"
    exit 0
  fi

  # -------------------------------------------------------------- service

  # The service does not get a login shell's PATH; agent-board needs ssh
  # (remote machines), uv (local wheels) and git, so name where they live.
  local SERVICE_PATH="$BIN_DIR:$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

  remove_legacy_service

  if [ "$OS" = "Darwin" ]; then
    local token_xml=""
    [ -n "$TOKEN" ] && token_xml="        <key>AGENT_BOARD_TOKEN</key><string>$TOKEN</string>"
    if [ "$DRY_RUN" -eq 1 ]; then
      info "[dry-run] write $PLIST and load it with launchctl"
    else
      mkdir -p "$(dirname "$PLIST")"
      ( umask 077; cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$COMMAND</string>
        <string>board</string>
        <string>--host</string><string>$HOST</string>
        <string>--port</string><string>$PORT</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key><string>$SERVICE_PATH</string>
$token_xml
    </dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>$DATA_DIR/logs/service.out.log</string>
    <key>StandardErrorPath</key><string>$DATA_DIR/logs/service.err.log</string>
</dict>
</plist>
PLIST
      )
      plutil -lint "$PLIST" >/dev/null || die "generated an invalid plist: $PLIST"
      launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
      launchctl bootstrap "gui/$(id -u)" "$PLIST" || die "launchctl could not load $PLIST"
      info "  started the launchd service ($LABEL)"
    fi
  else
    command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1 \
      || die "no systemd user session here. Re-run with --no-service and start
       'declarative-agent board' yourself (for example under tmux)"
    local token_line=""
    [ -n "$TOKEN" ] && token_line="Environment=AGENT_BOARD_TOKEN=$TOKEN"
    if [ "$DRY_RUN" -eq 1 ]; then
      info "[dry-run] write $UNIT_FILE and enable it with systemctl --user"
    else
      mkdir -p "$(dirname "$UNIT_FILE")"
      ( umask 077; cat > "$UNIT_FILE" <<UNITFILE
[Unit]
Description=agent-board (declarative-agent-sdk)
After=network-online.target

[Service]
Type=simple
ExecStart=$COMMAND board --host $HOST --port $PORT
Environment=PATH=$SERVICE_PATH
$token_line
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=default.target
UNITFILE
      )
      systemctl --user daemon-reload
      systemctl --user enable "$UNIT" >/dev/null 2>&1 || true
      systemctl --user restart "$UNIT" || die "could not start $UNIT; see: journalctl --user -u $UNIT"
      info "  started the systemd user service ($UNIT)"
      if [ "$(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null)" != "yes" ]; then
        warn "it will stop when you log out and start again at your next login. To keep
         it running and start it at boot:  sudo loginctl enable-linger $(id -un)"
      fi
    fi
  fi

  # ---------------------------------------------------------------- check

  local DIAL="$HOST"
  case "$HOST" in 0.0.0.0|::) DIAL="127.0.0.1" ;; esac
  local URL="http://$DIAL:$PORT"
  if [ "$DRY_RUN" -eq 0 ]; then
    local up=0
    for _ in $(seq 1 30); do
      if curl -fsS -m 2 "$URL/healthz" >/dev/null 2>&1; then up=1; break; fi
      sleep 1
    done
    if [ "$up" -eq 0 ]; then
      warn "agent-board did not answer on $URL within 30 s."
      if [ "$OS" = "Darwin" ]; then
        warn "see $DATA_DIR/logs/service.err.log"
      else
        warn "see: journalctl --user -u $UNIT"
      fi
      exit 1
    fi
  fi

  info ""
  if [ "$DRY_RUN" -eq 1 ]; then
    info "(dry run: nothing was changed) agent-board would be at $URL"
  else
    info "agent-board is running: $URL"
  fi
  [ -n "$TOKEN" ] && info "token: $DATA_DIR/token (the page asks for it once)"
  case "$HOST" in
    127.0.0.1|localhost|::1)
      info "Only this machine can reach it. From another computer, tunnel to it:"
      info "  ssh -L $PORT:127.0.0.1:$PORT $(id -un)@<this-machine>   then open http://127.0.0.1:$PORT"
      ;;
  esac
  info ""
  info "Next: open it, register a machine (use host 'localhost' for this one), then create an agent."
  info "Upgrade: run this installer again.  Remove: … | bash -s -- --uninstall"
}

main "$@"
