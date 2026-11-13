#!/usr/bin/env bash
#
# Remove an agent created by install.sh: stop its supervisord program, drop the
# program config, and optionally delete its directory. Non-interactive, like
# its counterpart, so an orchestrating agent can tear down sub-agents
# unattended.
#
#   ./uninstall.sh --name researcher             # stop and unregister
#   ./uninstall.sh --name researcher --purge     # also delete its directory
#   ./uninstall.sh --list
#
# Without --purge the install directory is left alone, because it holds the
# agent's .env and whatever it wrote to workspace/.
#
# Exit status 0 when the agent is gone, 1 on a usage error. Removing something
# that was never installed is not an error; --json says what was found.

set -euo pipefail

META_FILE=".agent-meta"

info() { printf '%s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

NAME=""
PREFIX=""
PURGE=0
JSON=0
LIST=0

usage() {
  cat <<USAGE
Remove an agent created by install.sh.

  uninstall.sh --name NAME [--purge] [--json]
  uninstall.sh --list

Options:
  --name NAME    agent to remove (required unless --list)
  --prefix DIR   install directory, if it is not the default for this NAME
  --purge        delete the install directory too, including .env and
                 workspace/. Guarded: refuses anything that does not look
                 like an agent install.
  --json         print one JSON result object on stdout
  --list         list installed agents as JSON and exit
  -h, --help     this message
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --name)   NAME="${2:-}"; shift 2 ;;
    --prefix) PREFIX="${2:-}"; shift 2 ;;
    --purge)  PURGE=1; shift ;;
    --json)   JSON=1; shift ;;
    --list)   LIST=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
done

# --- platform (same roots install.sh uses) ---------------------------------

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
      AGENTS_ROOT="$HOME/.declarative-agent"
    fi
    ;;
  *)
    die "unsupported platform: $OS (Linux and macOS only)"
    ;;
esac

SUPERVISOR_HOME="$AGENTS_ROOT/supervisor"
SUPERVISOR_VENV="$SUPERVISOR_HOME/.venv"
SUPERVISOR_CONF="$SUPERVISOR_HOME/supervisord.conf"
SUPERVISOR_CONFD="$SUPERVISOR_HOME/conf.d"

sctl() { "$SUPERVISOR_VENV/bin/supervisorctl" -c "$SUPERVISOR_CONF" "$@"; }

if [ "$LIST" -eq 1 ]; then
  first=1
  printf '['
  for meta in "$AGENTS_ROOT"/*/"$META_FILE"; do
    [ -f "$meta" ] || continue
    [ "$first" -eq 1 ] || printf ','
    first=0
    printf '%s' "$(cat "$meta")"
  done
  printf ']\n'
  exit 0
fi

[ -n "$NAME" ] || die "--name is required (see --help)"
case "$NAME" in
  *[!a-z0-9_-]*) die "--name may only contain a-z, 0-9, '_' and '-': '$NAME'" ;;
esac

: "${PREFIX:=$AGENTS_ROOT/$NAME}"

# --- stop and unregister ---------------------------------------------------

REMOVED=0
PURGED=0
ACTIONS=""

note() { ACTIONS="$ACTIONS${ACTIONS:+,}\"$1\""; info "$2"; }

PROGRAM_CONF="$SUPERVISOR_CONFD/$NAME.conf"

if [ -x "$SUPERVISOR_VENV/bin/supervisorctl" ] && [ -f "$SUPERVISOR_CONF" ]; then
  if sctl pid >/dev/null 2>&1; then
    # stop before removing the config: `update` on a deleted config stops it
    # too, but doing it explicitly means the JSON can report what happened.
    if sctl stop "$NAME" >/dev/null 2>&1; then
      REMOVED=1
      note "stopped" "stopped program $NAME"
    fi
  else
    info "supervisord is not running; removing the program config only"
  fi
fi

if [ -f "$PROGRAM_CONF" ]; then
  rm -f "$PROGRAM_CONF"
  REMOVED=1
  note "config-removed" "removed $PROGRAM_CONF"
  # reread+update makes supervisord forget the program entirely, so a later
  # reinstall of the same name is an add rather than a confusing no-op.
  if sctl pid >/dev/null 2>&1; then
    sctl reread >/dev/null 2>&1 || true
    sctl update >/dev/null 2>&1 || true
  fi
else
  info "no supervisord program config at $PROGRAM_CONF"
fi

# Agents created before the switch to supervisord registered a launchd job or
# a systemd unit of their own. Their metadata still says so, and without this
# a --purge would delete the directory and leave that registration behind,
# pointing at paths that no longer exist.
LEGACY_KIND=""
if [ -f "$PREFIX/$META_FILE" ]; then
  LEGACY_KIND="$(sed -n 's/.*"service_kind"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$PREFIX/$META_FILE" | head -1)"
fi

case "$LEGACY_KIND" in
  systemd)
    LEGACY_UNIT="/etc/systemd/system/declarative-agent-$NAME.service"
    if [ -f "$LEGACY_UNIT" ]; then
      [ "$IS_ROOT" -eq 1 ] || die "this agent predates supervisord and has a systemd unit at
    $LEGACY_UNIT
  Removing it needs root — re-run with sudo."
      systemctl disable --now "declarative-agent-$NAME" >/dev/null 2>&1 || true
      rm -f "$LEGACY_UNIT"
      systemctl daemon-reload
      systemctl reset-failed "declarative-agent-$NAME" 2>/dev/null || true
      REMOVED=1
      note "legacy-unit-removed" "removed pre-supervisord unit $LEGACY_UNIT"
    fi
    ;;
  launchagent|launchdaemon)
    LEGACY_LABEL="com.declarative-agent.$NAME"
    if [ "$LEGACY_KIND" = "launchdaemon" ]; then
      LEGACY_PLIST="/Library/LaunchDaemons/$LEGACY_LABEL.plist"
      LEGACY_DOMAIN="system"
    else
      LEGACY_PLIST="$HOME/Library/LaunchAgents/$LEGACY_LABEL.plist"
      LEGACY_DOMAIN="gui/$(id -u)"
    fi
    if launchctl bootout "$LEGACY_DOMAIN/$LEGACY_LABEL" 2>/dev/null; then
      REMOVED=1
      note "legacy-booted-out" "booted out pre-supervisord job $LEGACY_DOMAIN/$LEGACY_LABEL"
    fi
    if [ -f "$LEGACY_PLIST" ]; then
      if [ "$LEGACY_KIND" = "launchdaemon" ] && [ "$IS_ROOT" -ne 1 ]; then
        die "removing $LEGACY_PLIST needs root — re-run with sudo"
      fi
      rm -f "$LEGACY_PLIST"
      REMOVED=1
      note "legacy-plist-removed" "removed pre-supervisord plist $LEGACY_PLIST"
    fi
    ;;
esac

# --- optionally delete the install directory -------------------------------

purge_prefix() {
  # Done in python rather than `rm -rf "$PREFIX"` so a mistyped or surprising
  # --prefix cannot expand into something unintended. The guards are the point.
  python3 - "$1" <<'PY'
import os, pathlib, shutil, sys

target = pathlib.Path(sys.argv[1]).expanduser()
resolved = target.resolve()

if not resolved.is_dir():
    sys.exit(f"not a directory: {resolved}")
for guard in (pathlib.Path("/"), pathlib.Path.home(), pathlib.Path(os.getcwd())):
    if resolved == guard.resolve():
        sys.exit(f"refusing to delete {resolved}")
if len(resolved.parts) < 3:
    sys.exit(f"refusing to delete a top-level path: {resolved}")
# An agent directory always has at least one of these; a wrong --prefix usually
# has none, and that is the case worth catching before the delete.
markers = (".agent-meta", "agent.yaml", ".venv", ".env", "workspace")
if not any((resolved / name).exists() for name in markers):
    sys.exit(f"{resolved} does not look like an agent install "
             f"(no {', '.join(markers)}) — delete it yourself if you meant it")

shutil.rmtree(resolved)
PY
}

if [ "$PURGE" -eq 1 ]; then
  if [ ! -d "$PREFIX" ]; then
    info "nothing to purge at $PREFIX"
  else
    command -v python3 >/dev/null \
      || die "--purge needs python3 for the guarded delete; remove $PREFIX by hand"
    purge_prefix "$PREFIX"
    PURGED=1
    note "purged" "deleted $PREFIX"
  fi
elif [ -d "$PREFIX" ]; then
  info "left $PREFIX in place (.env, workspace/). Use --purge to delete it."
fi

# --- result ----------------------------------------------------------------

if [ "$JSON" -eq 1 ]; then
  cat <<JSON
{"name":"$NAME",
 "prefix":"$PREFIX",
 "service_removed":$([ "$REMOVED" -eq 1 ] && echo true || echo false),
 "purged":$([ "$PURGED" -eq 1 ] && echo true || echo false),
 "actions":[$ACTIONS]}
JSON
else
  if [ "$REMOVED" -eq 1 ]; then
    info "agent '$NAME' removed"
  else
    info "nothing was registered for '$NAME' — if it is still running, it was started by hand"
  fi
  if [ -d "$SUPERVISOR_CONFD" ] && [ -z "$(ls -A "$SUPERVISOR_CONFD" 2>/dev/null)" ]; then
    info "no agents left; stop supervisord with: $SUPERVISOR_VENV/bin/supervisorctl -c $SUPERVISOR_CONF shutdown"
  fi
fi
