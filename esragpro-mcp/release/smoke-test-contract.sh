#!/usr/bin/env bash
# Contract test for the non-deployment MCP release gate.
#
# This intentionally checks the release gate's behavior contract rather than
# running the gate itself. It is a red-first guard against quietly regressing
# to source grep and compile-only validation.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SMOKE_SCRIPT="$SCRIPT_DIR/smoke-test.sh"

if [[ ! -f "$SMOKE_SCRIPT" ]]; then
    echo "ERROR: release smoke script not found: $SMOKE_SCRIPT" >&2
    exit 1
fi

require_literal() {
    local literal="$1"
    if ! grep -Fq -- "$literal" "$SMOKE_SCRIPT"; then
        echo "MISSING_RELEASE_GATE_CONTRACT: $literal" >&2
        exit 1
    fi
}

# The shipped HTTP endpoint is feature-gated and management tests must use the
# same combined profile that a release binary is expected to expose.
require_literal 'cargo build --release --locked --features http,management-profile'
require_literal 'cargo test --release --locked --features http,management-profile'

# The gate must drive actual MCP messages through both transports.
require_literal 'initialize'
require_literal 'notifications/initialized'
require_literal 'tools/list'
require_literal 'tools/call'
require_literal '/mcp'

# Missing OAuth/backend prerequisites must be reported explicitly rather than
# being presented as authenticated protocol coverage.
require_literal 'MISSING_PREREQUISITE'

# Release validation must never overwrite the shared target that an active MCP
# host may be executing. An operator may provide an isolated override, while
# the default path is created fresh and cleaned up by smoke-test.sh.
require_literal 'MCP_SMOKE_TARGET_DIR'
require_literal 'CARGO_TARGET_DIR'
require_literal 'SHARED_TARGET_DIR'
require_literal 'shared target'
require_literal 'realpath -m'
require_literal 'CLEANUP_TARGET_DIR'
require_literal 'BINARY="$TARGET_DIR/release/esragpro-mcp'

if grep -Fq 'BINARY="target/release/' "$SMOKE_SCRIPT"; then
    echo "INVALID_RELEASE_GATE_CONTRACT: shared target/release binary path remains" >&2
    exit 1
fi

# Source grep is not protocol validation. Keep this contract broad enough to
# allow implementation details while rejecting the old source-count approach.
if grep -Fq 'grep -q "name = \\"$tool\\"' "$SMOKE_SCRIPT" || \
   grep -Fq 'grep -E' "$SMOKE_SCRIPT"; then
    echo "INVALID_RELEASE_GATE_CONTRACT: source grep remains in smoke gate" >&2
    exit 1
fi

echo "MCP release smoke contract passed"
