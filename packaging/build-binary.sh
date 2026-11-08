#!/usr/bin/env bash
#
# Build a self-contained `declarative-agent` executable with PyApp.
#
#   ./packaging/build-binary.sh                            # host target
#   ./packaging/build-binary.sh x86_64-unknown-linux-gnu   # explicit target
#
# Output: dist/declarative-agent-<version>-<target>/ and a matching .tar.gz
#
# Requires: cargo (Rust), uv, curl, tar, python3.
#
# NOTE: PyApp installs the project into a real on-disk site-packages at first
# run. That is deliberate and required here — ToolRegistry discovers built-in
# tools by globbing tools/builtin/*.py (see tools/tool_registry.py), which a
# frozen or zipimported bundle (PyInstaller, zipapp) would break silently,
# registering zero tools with no error. Do not "optimize" this into a freezer.

set -euo pipefail

PYAPP_VERSION="${PYAPP_VERSION:-0.31.0}"
PYTHON_VERSION="${PYTHON_VERSION:-3.14}"
BIN_NAME="declarative-agent"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

TARGET="${1:-$(rustc -vV | awk '/^host:/ {print $2}')}"
WORK="$REPO_ROOT/.pyapp-build"
OUT="$REPO_ROOT/dist"

echo "==> target : $TARGET"
echo "==> pyapp  : v$PYAPP_VERSION"
echo "==> python : $PYTHON_VERSION"

# Remove only generated trees, by explicit path. Done in python rather than
# `rm -rf` with a glob so a mistyped variable cannot expand into something
# unintended.
clean_tree() {
  python3 - "$@" <<'PY'
import pathlib, shutil, sys
for raw in sys.argv[1:]:
    p = pathlib.Path(raw)
    if p.is_dir():
        shutil.rmtree(p)
        print(f"    removed {p}")
PY
}

# --- 1. clean -------------------------------------------------------------
# A stale build/ or *.egg-info from the older flat layout can poison the
# setuptools build, so clear them before building the wheel.
echo "==> cleaning"
clean_tree "$WORK" "$REPO_ROOT/build" "$REPO_ROOT/declarative_agent_sdk.egg-info"
mkdir -p "$WORK" "$OUT"

# --- 2. wheel -------------------------------------------------------------
# --package is needed because the repo root is a uv workspace whose member
# examples/simple_agent is also buildable.
echo "==> building wheel"
uv build --package declarative-agent-sdk --wheel --out-dir "$OUT" >/dev/null
WHEEL_PATH="$(ls -t "$OUT"/*.whl | head -1)"
WHEEL_NAME="$(basename "$WHEEL_PATH")"
VERSION="$(printf '%s' "$WHEEL_NAME" | cut -d- -f2)"
echo "    $WHEEL_NAME (version $VERSION)"

# --- 3. pyapp source ------------------------------------------------------
# The `cargo install pyapp` route cannot cross compile and cannot take local
# embedded paths, so build from the pinned source archive instead.
echo "==> fetching pyapp source"

fetch_pyapp() {
  # --fail is essential: without it curl writes HTTP error bodies (e.g. a 504
  # HTML page) straight into the output file and still exits 0. The failure then
  # surfaces much later as a baffling "tar: unrecognized archive format".
  curl --fail --location --silent --show-error \
       --retry 5 --retry-all-errors --retry-delay 3 \
       "$1" -o "$WORK/pyapp.tar.gz"
}

# Release assets and codeload archives are served by different backends, so
# when one has an outage the other usually still works. Both unpack to a
# pyapp-* directory.
if ! fetch_pyapp "https://github.com/ofek/pyapp/releases/download/v${PYAPP_VERSION}/source.tar.gz"; then
  echo "    release asset unavailable; falling back to the tag archive"
  fetch_pyapp "https://github.com/ofek/pyapp/archive/refs/tags/v${PYAPP_VERSION}.tar.gz"
fi

if ! gzip -t "$WORK/pyapp.tar.gz" 2>/dev/null; then
  echo "downloaded pyapp archive is not valid gzip; first bytes:" >&2
  head -c 200 "$WORK/pyapp.tar.gz" >&2
  echo >&2
  exit 1
fi

tar -xzf "$WORK/pyapp.tar.gz" -C "$WORK"
PYAPP_DIR="$(find "$WORK" -maxdepth 1 -type d -name 'pyapp-*' | head -1)"
[ -n "$PYAPP_DIR" ] || { echo "could not find unpacked pyapp dir" >&2; exit 1; }

# --- 4. embed the wheel ---------------------------------------------------
# PyApp requires embedded local paths to be relative AND inside its own tree.
cp "$WHEEL_PATH" "$PYAPP_DIR/$WHEEL_NAME"

# --- 5. build -------------------------------------------------------------
echo "==> cargo build (several minutes on a cold cache)"
cd "$PYAPP_DIR"

export PYAPP_PROJECT_PATH="./$WHEEL_NAME"   # embed the wheel, relative path
export PYAPP_PROJECT_FEATURES="discord"     # the discord extra, not [all]
export PYAPP_PYTHON_VERSION="$PYTHON_VERSION"
export PYAPP_DISTRIBUTION_EMBED="1"         # no Python needed on the host
export PYAPP_FULL_ISOLATION="1"             # own distribution copy per install
export PYAPP_UV_ENABLED="1"                 # ~40s first-run install, vs minutes
# :run not :main — PyApp discards the callable's return value, so the
# entry point must raise SystemExit for exit codes to survive.
export PYAPP_EXEC_SPEC="declarative_agent_sdk.cli.main:run"

rustup target add "$TARGET" >/dev/null 2>&1 || true
cargo build --release --target "$TARGET"

# --- 6. collect -----------------------------------------------------------
BUILT="$PYAPP_DIR/target/$TARGET/release/pyapp"
[ -f "$BUILT" ] || { echo "expected binary not found: $BUILT" >&2; exit 1; }

# PyApp's build.rs downloads the CPython distribution to embed it, from the same
# GitHub release-asset endpoint that occasionally 504s. On failure it can embed
# the HTML error page instead and still report a successful build, yielding a
# ~3MB binary that dies at runtime with "failed to iterate over archive".
# An embedded distribution is tens of MB, so a small binary means no distribution.
MIN_EMBED_BYTES=15000000
SIZE=$(wc -c < "$BUILT" | tr -d ' ')
if [ "${PYAPP_DISTRIBUTION_EMBED:-0}" = "1" ] && [ "$SIZE" -lt "$MIN_EMBED_BYTES" ]; then
  echo >&2
  echo "ERROR: binary is only $SIZE bytes — the embedded Python distribution is missing." >&2
  echo "       PYAPP_DISTRIBUTION_EMBED=1 was set, so this build is broken." >&2
  echo "       Usually a transient GitHub release-asset outage; retry shortly." >&2
  exit 1
fi

# Only meaningful when building for the host; a cross-built binary cannot run here.
if [ "$TARGET" = "$(rustc -vV | awk '/^host:/ {print $2}')" ]; then
  echo "==> verifying the binary runs"
  if ! "$BUILT" version >/dev/null 2>&1; then
    echo "ERROR: the built binary failed to run '$BUILT version'" >&2
    "$BUILT" version >&2 || true
    exit 1
  fi
fi

STAGE="$OUT/${BIN_NAME}-${VERSION}-${TARGET}"
clean_tree "$STAGE"
mkdir -p "$STAGE"
cp "$BUILT" "$STAGE/$BIN_NAME"
chmod +x "$STAGE/$BIN_NAME"

# Config ships alongside the binary: paths inside agent.yaml resolve relative
# to the YAML itself (AgentFactory.load_config), so unpack-and-run works from
# any directory.
EX="$REPO_ROOT/examples/discord_bot"
cp "$EX/agent.yaml" "$EX/instructions.md" "$EX/.env.example" "$STAGE/"
cp -R "$EX/skills" "$STAGE/skills"
cp "$REPO_ROOT/packaging/README-dist.md" "$STAGE/README.md"
# Belt and braces: never ship a real .env, it holds a live bot token.
rm -f "$STAGE/.env"

cd "$OUT"
tar -czf "${BIN_NAME}-${VERSION}-${TARGET}.tar.gz" "$(basename "$STAGE")"

echo
echo "==> done"
echo "    binary  : $STAGE/$BIN_NAME"
echo "    tarball : $OUT/${BIN_NAME}-${VERSION}-${TARGET}.tar.gz"
du -h "$STAGE/$BIN_NAME" | awk '{print "    size    : "$1}'
