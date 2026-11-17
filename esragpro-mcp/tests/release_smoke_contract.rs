use std::fs;

const REQUIRED_LITERALS: &[&str] = &[
    "cargo build --release --locked --features http,management-profile",
    "cargo test --release --locked --features http,management-profile",
    "initialize",
    "notifications/initialized",
    "tools/list",
    "tools/call",
    "/mcp",
    "MISSING_PREREQUISITE",
];

const ISOLATION_LITERALS: &[&str] = &[
    "MCP_SMOKE_TARGET_DIR",
    "CARGO_TARGET_DIR",
    "isolated",
    "shared target",
];

#[test]
fn release_smoke_gate_exercises_protocol_surfaces() {
    let smoke_path = format!("{}/release/smoke-test.sh", env!("CARGO_MANIFEST_DIR"));
    let smoke = fs::read_to_string(&smoke_path)
        .unwrap_or_else(|error| panic!("read {smoke_path}: {error}"));

    for literal in REQUIRED_LITERALS {
        assert!(
            smoke.contains(literal),
            "release smoke gate is missing required contract marker: {literal}"
        );
    }

    assert!(
        !smoke.contains("grep -q \\\"name = \\\\\\\"$tool\\\\\\\""),
        "release smoke gate must not count tools by grepping source"
    );
    assert!(
        !smoke.contains("grep -E"),
        "release smoke gate must not rely on source grep validation"
    );
}

#[test]
fn release_smoke_gate_rejects_shared_target_and_propagates_isolated_binary() {
    let smoke_path = format!("{}/release/smoke-test.sh", env!("CARGO_MANIFEST_DIR"));
    let smoke = fs::read_to_string(&smoke_path)
        .unwrap_or_else(|error| panic!("read {smoke_path}: {error}"));

    for literal in ISOLATION_LITERALS {
        assert!(
            smoke.contains(literal),
            "release smoke gate is missing isolated-target contract marker: {literal}"
        );
    }

    assert!(
        smoke.contains("BINARY=\"$TARGET_DIR/release/"),
        "release smoke gate must derive the checked binary from its isolated target"
    );
    assert!(
        !smoke.contains("BINARY=\"target/release/"),
        "release smoke gate must not use the shared target/release path"
    );
}

#[test]
fn release_protocol_test_contract_bounds_owned_child_lifecycle() {
    let protocol_path = format!(
        "{}/tests/release_protocol_tests.rs",
        env!("CARGO_MANIFEST_DIR")
    );
    let protocol = fs::read_to_string(&protocol_path)
        .unwrap_or_else(|error| panic!("read {protocol_path}: {error}"));

    for literal in [
        "CHILD_CLEANUP_TIMEOUT",
        "wait_for_exit",
        "terminate",
        "try_wait",
    ] {
        assert!(
            protocol.contains(literal),
            "release protocol tests are missing bounded child lifecycle marker: {literal}"
        );
    }
}
