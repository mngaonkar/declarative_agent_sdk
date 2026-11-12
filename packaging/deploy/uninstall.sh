#!/usr/bin/env bash
#
# Remove the Discord agent service installed by install.sh.
#
# By default this stops and unregisters the service but leaves the install
# directory alone, because it holds your .env and everything the agent wrote to
# workspace/. Pass --purge to delete it too.
#
#   ./packaging/deploy/uninstall.sh --help

set -euo pipefail

SERVICE_NAME="declarative-agent-discord"
LABEL="com.declarative-agent.discord"

info() { printf '==> %s\n' "$*"; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

PREFIX=""
SYSTEM_SCOPE=0
PURGE=0
ASSUME_YES=0

usage() {
  cat <<USAGE
Remove the Discord agent service.

Options:
  --prefix DIR   install directory (default: the same as install.sh)
  --system       macOS: remove the LaunchDaemon rather than the LaunchAgent
  --purge        also delete the install directory, including .env and
                 workspace/ — asks first unless --yes is given
  --yes          do not prompt for the --purge confirmation
  -h, --help     this message
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --prefix) PREFIX="${2:-}"; shift 2 ;;
    --system) SYSTEM_SCOPE=1; shift ;;
    --purge)  PURGE=1; shift ;;
    --yes)    ASSUME_YES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
done

OS="$(uname -s)"
case "$OS" in
  Linux)
    command -v systemctl >/dev/null || die "systemctl not found"
    [ "$(id -u)" -eq 0 ] || die "removing /etc/systemd/system/$SERVICE_NAME.service needs root — re-run with sudo"
    : "${PREFIX:=/opt/discord-agent}"
    ;;
  Darwin)
    if [ "$SYSTEM_SCOPE" -eq 1 ]; then
      [ "$(id -u)" -eq 0 ] || die "removing a LaunchDaemon needs root — re-run with sudo"
      : "${PREFIX:=/opt/declarative-agent-discord}"
    else
      # Same reasoning as install.sh: launchctl acts on the caller's own domain.
      [ "$(id -u)" -ne 0 ] || die "a LaunchAgent lives in your own launchd domain — re-run without sudo, or pass --system"
      : "${PREFIX:=$HOME/.declarative-agent-discord}"
    fi
    ;;
  *)
    die "unsupported platform: $OS (Linux and macOS only)"
    ;;
esac

# --- stop and unregister ---------------------------------------------------

REMOVED=0

if [ "$OS" = "Linux" ]; then
  UNIT="/etc/systemd/system/$SERVICE_NAME.service"
  if [ -f "$UNIT" ]; then
    # disable --now stops it and drops the multi-user.target symlink in one go.
    systemctl disable --now "$SERVICE_NAME" >/dev/null 2>&1 || true
    rm -f "$UNIT"
    systemctl daemon-reload
    # Without this a latched start limit survives and confuses the next install.
    systemctl reset-failed "$SERVICE_NAME" 2>/dev/null || true
    info "removed $UNIT"
    REMOVED=1
  else
    info "no systemd unit installed at $UNIT"
  fi
else
  if [ "$SYSTEM_SCOPE" -eq 1 ]; then
    PLIST="/Library/LaunchDaemons/$LABEL.plist"
    DOMAIN="system"
  else
    PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
    DOMAIN="gui/$(id -u)"
  fi
  # Boot out whether or not the plist is still on disk: the job can be loaded
  # from an earlier install whose file was already deleted by hand.
  if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    info "booted out $DOMAIN/$LABEL"
    REMOVED=1
  fi
  if [ -f "$PLIST" ]; then
    rm -f "$PLIST"
    info "removed $PLIST"
    REMOVED=1
  fi
  [ "$REMOVED" -eq 1 ] || info "no launchd job installed for $LABEL"
fi

# --- optionally delete the install directory -------------------------------

purge_prefix() {
  # Done in python rather than `rm -rf "$PREFIX"` so a mistyped or surprising
  # --prefix cannot expand into something unintended. The guards below are the
  # point of the exercise: refuse anything that is not plausibly an install dir.
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
# An install dir always has at least one of these; a wrong --prefix usually has
# none, and that is the case worth catching before the delete.
if not any((resolved / name).exists() for name in ("agent.yaml", ".venv", ".env", "workspace")):
    sys.exit(f"{resolved} does not look like an agent install (no agent.yaml, "
             f".venv, .env or workspace/) — delete it yourself if you meant it")

shutil.rmtree(resolved)
print(f"    deleted {resolved}")
PY
}

if [ "$PURGE" -eq 1 ]; then
  if [ ! -d "$PREFIX" ]; then
    info "nothing to purge at $PREFIX"
  else
    # Checked before the prompt, so a wrong --prefix is refused outright rather
    # than after asking the user to confirm deleting something unrecognisable.
    LOOKS_RIGHT=0
    for marker in agent.yaml .venv .env workspace; do
      [ -e "$PREFIX/$marker" ] && LOOKS_RIGHT=1
    done
    [ "$LOOKS_RIGHT" -eq 1 ] || die "$PREFIX does not look like an agent install (no agent.yaml, .venv, .env or workspace/) — delete it yourself if you meant it"
    command -v python3 >/dev/null \
      || die "--purge needs python3 for the guarded delete; remove $PREFIX by hand instead"
    echo
    echo "About to delete $PREFIX, which contains:"
    [ -f "$PREFIX/.env" ]       && echo "  .env        your Discord token and API keys"
    [ -d "$PREFIX/workspace" ]  && echo "  workspace/  $(find "$PREFIX/workspace" -type f 2>/dev/null | wc -l | tr -d ' ') files the agent produced"
    [ -d "$PREFIX/skills" ]     && echo "  skills/     agent skills"
    [ -f "$PREFIX/agent.yaml" ] && echo "  agent.yaml  agent configuration"
    echo
    if [ "$ASSUME_YES" -eq 0 ]; then
      printf 'Delete it? [y/N] '
      read -r reply
      case "$reply" in
        y|Y|yes|YES) ;;
        *) info "left $PREFIX in place"; exit 0 ;;
      esac
    fi
    purge_prefix "$PREFIX"
    info "purged $PREFIX"
  fi
elif [ -d "$PREFIX" ]; then
  echo
  info "left $PREFIX in place (.env, workspace/, agent.yaml). Delete it with --purge."
fi

echo
if [ "$REMOVED" -eq 1 ]; then
  info "service removed"
else
  info "nothing was registered — if the bot is still running, it was started by hand"
fi
