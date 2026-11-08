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
curl -sSL "https://github.com/ofek/pyapp/releases/download/v${PYAPP_VERSION}/source.tar.gz" \
  -o "$WORK/pyapp.tar.gz"
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
