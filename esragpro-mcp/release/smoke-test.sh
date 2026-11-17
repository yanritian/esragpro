#!/usr/bin/env bash
# Release Smoke Test (WP8)
#
# This is a non-deployment gate for the shipped MCP protocol surfaces. It
# builds the same HTTP + management profile that production exposes, runs the
# full Rust suite, and drives the built binary through bounded protocol tests.
#
# Authenticated Streamable HTTP initialize/list/call requires an operator
# supplied bearer token and JWKS endpoint. The gate always checks the real
# unauthenticated /mcp boundary, but fails closed when those credentials are
# unavailable instead of claiming authenticated coverage.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

canonicalize_path() {
    local path="$1"
    if command -v cygpath >/dev/null 2>&1; then
        path="$(cygpath -m -- "$path")"
    fi
    realpath -m -- "$path"
}

if ! SHARED_TARGET_DIR="$(canonicalize_path "$PROJECT_DIR/target")"; then
    echo "ERROR: unable to resolve shared Cargo target path" >&2
    exit 1
fi
TARGET_DIR_INPUT="${MCP_SMOKE_TARGET_DIR:-${CARGO_TARGET_DIR:-}}"
CLEANUP_TARGET_DIR=0

if [[ -z "$TARGET_DIR_INPUT" ]]; then
    TARGET_DIR="$(mktemp -d "${TMPDIR:-/tmp}/esragpro-mcp-release.XXXXXX")"
    CLEANUP_TARGET_DIR=1
else
    TARGET_DIR="$TARGET_DIR_INPUT"
fi

if ! TARGET_DIR="$(canonicalize_path "$TARGET_DIR")"; then
    echo "ERROR: unable to resolve isolated Cargo target: $TARGET_DIR_INPUT" >&2
    exit 1
fi

case "$TARGET_DIR" in
    "$SHARED_TARGET_DIR"|"$SHARED_TARGET_DIR"/*)
        echo "ERROR: release smoke validation requires an isolated target; shared target is forbidden: $TARGET_DIR" >&2
        exit 1
        ;;
esac

export CARGO_TARGET_DIR="$TARGET_DIR"

cleanup_target() {
    if [[ "$CLEANUP_TARGET_DIR" -eq 1 ]]; then
        rm -rf -- "$TARGET_DIR"
    fi
}
trap cleanup_target EXIT

EXE_SUFFIX=""
case "$(uname -s 2>/dev/null || printf '')" in
    MINGW*|MSYS*|CYGWIN*) EXE_SUFFIX=".exe" ;;
esac
BINARY="$TARGET_DIR/release/esragpro-mcp$EXE_SUFFIX"
MISSING_HTTP_PREREQUISITE=0

echo "=== MCP Release Protocol Smoke Test ==="
echo ""

echo "[1/6] Building release binary with HTTP + management profile..."
cargo build --release --locked --features http,management-profile
if [[ ! -f "$BINARY" ]]; then
    echo "ERROR: Release binary not found: $BINARY" >&2
    exit 1
fi
echo "      Isolated Cargo target: $TARGET_DIR"
echo "      Binary: $BINARY"

echo ""
echo "[2/6] Running full release test suite with HTTP + management profile..."
cargo test --release --locked --features http,management-profile
echo "      Full release test suite passed"

echo ""
echo "[3/6] Running real stdio lifecycle protocol test..."
cargo test --release --locked --features http,management-profile --test r4c_stdio_lifecycle_tests -- --nocapture
echo "      stdio initialize/notifications/list protocol passed"

echo ""
echo "[4/6] Running bounded stdio + Streamable HTTP protocol test..."
# This starts the built binary and exercises:
# - stdio: initialize, notifications/initialized, tools/list, tools/call
# - HTTP: /.well-known metadata and unauthenticated POST /mcp (401)
cargo test --release --locked --features http,management-profile --test release_protocol_tests -- --nocapture
echo "      protocol-level stdio and HTTP boundary checks passed"

echo ""
echo "[5/6] Checking authenticated HTTP prerequisites..."
if [[ -z "${MCP_SMOKE_HTTP_BEARER_TOKEN:-}" ]]; then
    echo "MISSING_PREREQUISITE: MCP_SMOKE_HTTP_BEARER_TOKEN is required for authenticated Streamable HTTP initialize/list/call"
    MISSING_HTTP_PREREQUISITE=1
fi
if [[ -z "${MCP_SMOKE_OAUTH_ISSUER:-}" ]]; then
    echo "MISSING_PREREQUISITE: MCP_SMOKE_OAUTH_ISSUER is required for authenticated Streamable HTTP initialize/list/call"
    MISSING_HTTP_PREREQUISITE=1
fi
if [[ -z "${MCP_SMOKE_OAUTH_AUDIENCE:-}" ]]; then
    echo "MISSING_PREREQUISITE: MCP_SMOKE_OAUTH_AUDIENCE is required for authenticated Streamable HTTP initialize/list/call"
    MISSING_HTTP_PREREQUISITE=1
fi
if [[ -z "${MCP_SMOKE_JWKS_URL:-}" ]]; then
    echo "MISSING_PREREQUISITE: MCP_SMOKE_JWKS_URL is required for authenticated Streamable HTTP initialize/list/call"
    MISSING_HTTP_PREREQUISITE=1
fi

if [[ "$MISSING_HTTP_PREREQUISITE" -ne 0 ]]; then
    echo "HTTP authenticated initialize/list/call and management-scope ACL checks are not claimed."
    echo "The real HTTP negative boundary was exercised; provide all four MCP_SMOKE_* values to enable authenticated coverage."
    exit 1
fi

echo "      Authenticated HTTP prerequisites present"
echo ""
echo "[6/6] Release protocol gate complete"
echo ""
echo "Validation summary:"
echo "  - HTTP + management-profile release build passed"
echo "  - Full release tests passed"
echo "  - Real stdio initialize/list/call protocol passed"
echo "  - Real HTTP metadata + unauthorized /mcp boundary passed"
echo "  - Authenticated HTTP prerequisites present (token/JWKS)"
echo ""
echo "=== MCP Release Protocol Smoke Test Complete ==="
