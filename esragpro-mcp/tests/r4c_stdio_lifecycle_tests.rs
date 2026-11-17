//! P1-09 regression test: real MCP stdio JSON-RPC lifecycle.
//!
//! Verifies that the stdio transport cleanly handles the canonical lifecycle:
//!
//!   1. client → server: `initialize` (request, id=1)
//!   2. server → client: result (response, id=1)
//!   3. client → server: `notifications/initialized` (notification, no id)
//!   4. client → server: `tools/list` (request, id=2)
//!   5. server → client: result (response, id=2)
//!
//! Before the P1-09 fix, `tracing_subscriber::fmt()` wrote startup log lines
//! to stdout, corrupting the JSON-RPC stream and producing a parse error
//! when the client sent `notifications/initialized` immediately after
//! receiving the `initialize` response. The fix redirects tracing to stderr.
//!
//! This test spawns the real `esragpro-mcp` binary with stdio transport and
//! verifies that every line read from stdout is a valid JSON-RPC message —
//! no interleaved log output.

use std::io::{BufRead, BufReader, Write};
use std::process::{Command, Stdio};
use std::time::Duration;

/// Timeout for reading a single JSON-RPC line from the server.
const READ_TIMEOUT: Duration = Duration::from_secs(10);

/// Spawn the esragpro-mcp binary in stdio mode.
/// Returns (child, stdin, line_reader).
fn spawn_mcp_stdio() -> (std::process::Child, std::process::ChildStdin, LineReader) {
    let binary = env!("CARGO_BIN_EXE_esragpro-mcp");

    let mut child = Command::new(binary)
        .arg("stdio")
        .env("ESRAGPRO_BACKEND_URL", "http://127.0.0.1:1/api/v1")
        .env("RUST_LOG", "info")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        // stderr is intentionally inherited so tracing output is visible
        // in test output — but it must NOT appear on stdout.
        .spawn()
        .expect("failed to spawn esragpro-mcp binary");

    let stdin = child.stdin.take().expect("stdin pipe");
    let stdout = child.stdout.take().expect("stdout pipe");
    let reader = LineReader::new(stdout);

    (child, stdin, reader)
}

/// Read a single non-empty line from stdout.
/// Fails the test if the line is not valid JSON (i.e. it's a log line).
/// Uses a background reader thread that is spawned once per test (see
/// `LineReader`) to avoid lifetime issues with BufReader across threads.
fn read_json_line_from(rx: &std::sync::mpsc::Receiver<Result<String, String>>) -> String {
    match rx.recv_timeout(READ_TIMEOUT) {
        Ok(Ok(line)) => {
            // Critical assertion: every line on stdout MUST be valid JSON.
            // If this fails, the server is writing non-JSON-RPC output
            // (e.g. log lines) to stdout, which corrupts the stream.
            assert!(
                line.starts_with('{'),
                "stdout line is not a JSON object (log line on stdout?): {}",
                &line[..line.len().min(200)]
            );
            let parsed: Result<serde_json::Value, _> = serde_json::from_str(&line);
            assert!(
                parsed.is_ok(),
                "stdout line is not valid JSON-RPC (parse error): {}",
                &line[..line.len().min(200)]
            );
            line
        }
        Ok(Err(e)) => panic!("server closed stdout: {}", e),
        Err(_) => panic!(
            "timeout ({:?}) waiting for JSON-RPC response — server may be hung",
            READ_TIMEOUT
        ),
    }
}

/// Spawns a background reader thread that reads lines from a ChildStdout
/// and sends them over a channel. This avoids lifetime issues with moving
/// a BufReader into a thread.
struct LineReader {
    rx: std::sync::mpsc::Receiver<Result<String, String>>,
    _handle: std::thread::JoinHandle<()>,
}

impl LineReader {
    fn new(stdout: std::process::ChildStdout) -> Self {
        let (tx, rx) = std::sync::mpsc::channel();
        let handle = std::thread::spawn(move || {
            let mut reader = BufReader::new(stdout);
            let mut line = String::new();
            loop {
                line.clear();
                let n = match reader.read_line(&mut line) {
                    Ok(0) => {
                        tx.send(Err("EOF reached".to_string())).ok();
                        return;
                    }
                    Ok(n) => n,
                    Err(e) => {
                        tx.send(Err(format!("read_line error: {}", e))).ok();
                        return;
                    }
                };
                let _ = n;
                let trimmed = line.trim().to_string();
                if trimmed.is_empty() {
                    continue;
                }
                if tx.send(Ok(trimmed)).is_err() {
                    return;
                }
            }
        });
        Self {
            rx,
            _handle: handle,
        }
    }
}

/// Write a JSON-RPC message followed by a newline to stdin.
fn send_json(stdin: &mut std::process::ChildStdin, msg: &serde_json::Value) {
    let payload = serde_json::to_string(msg).expect("serialize JSON-RPC message");
    writeln!(stdin, "{}", payload).expect("write to stdin failed");
    stdin.flush().expect("flush stdin failed");
}

#[test]
fn test_stdio_lifecycle_initialize_then_notifications_initialized() {
    let (mut child, mut stdin, reader) = spawn_mcp_stdio();

    // Step 1: send `initialize` request.
    let init_request = serde_json::json!({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {
                "name": "p1-09-lifecycle-test",
                "version": "0.1.0"
            }
        }
    });
    send_json(&mut stdin, &init_request);

    // Step 2: read the `initialize` response.
    let init_response_str = read_json_line_from(&reader.rx);
    let init_response: serde_json::Value =
        serde_json::from_str(&init_response_str).expect("parse initialize response");

    // Must be a successful response (has `result`, not `error`).
    assert!(
        init_response.get("result").is_some(),
        "initialize must succeed, got: {}",
        init_response
    );
    assert_eq!(init_response["id"], 1, "response id must match request id");

    // Step 3: send `notifications/initialized` notification (no id).
    // This is the critical step — before the P1-09 fix, this would trigger
    // a parse error because the server's log output interleaved with the
    // JSON-RPC stream.
    let initialized_notification = serde_json::json!({
        "jsonrpc": "2.0",
        "method": "notifications/initialized"
    });
    send_json(&mut stdin, &initialized_notification);

    // Step 4: send `tools/list` to verify the session is still alive.
    let tools_list_request = serde_json::json!({
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/list",
        "params": {}
    });
    send_json(&mut stdin, &tools_list_request);

    // Step 5: read the `tools/list` response.
    // The server may emit JSON-RPC notifications before the response
    // (e.g. `notifications/message`). Skip any non-response lines until
    // we get the response with id=2.
    let mut got_tools_list = false;
    for _ in 0..10 {
        let line_str = read_json_line_from(&reader.rx);
        let msg: serde_json::Value =
            serde_json::from_str(&line_str).expect("parse tools/list response");

        // If this is a response to our id=2 request, we're done.
        if msg.get("id").and_then(|v| v.as_i64()) == Some(2) {
            assert!(
                msg.get("result").is_some(),
                "tools/list must succeed, got: {}",
                msg
            );
            // Verify the server returned some tools (memory.query etc.).
            let tools = msg["result"]["tools"].as_array();
            assert!(
                tools.is_some() && !tools.unwrap().is_empty(),
                "tools/list must return at least one tool"
            );
            got_tools_list = true;
            break;
        }
        // Otherwise it's a notification or stray message — accept it as
        // valid JSON (the key invariant is that stdout is always valid JSON).
    }

    assert!(
        got_tools_list,
        "did not receive tools/list response (id=2) within 10 lines"
    );

    // Clean shutdown: drop stdin to signal EOF to the server.
    drop(stdin);

    // Wait for the child to exit (it should shut down gracefully).
    let status = child.wait().expect("wait for child failed");
    // The server may exit with 0 or a non-zero code depending on whether
    // the backend is reachable. We only care that it didn't crash with a
    // panic during the lifecycle.
    assert!(
        !status.code().map(|c| c == 101).unwrap_or(false),
        "server exited with panic code 101 — likely a JSON-RPC parse error"
    );
}

/// Verify that no log lines are written to stdout when the server starts.
/// This is the specific regression guard for the P1-09 fix.
#[test]
fn test_stdio_no_log_lines_on_stdout_at_startup() {
    let (mut child, mut stdin, reader) = spawn_mcp_stdio();

    // Give the server a moment to start up and emit any startup logs.
    std::thread::sleep(Duration::from_millis(500));

    // Send an initialize request. If startup logs were written to stdout,
    // the first line read would be a log line, not a JSON response.
    let init_request = serde_json::json!({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {
                "name": "p1-09-log-line-test",
                "version": "0.1.0"
            }
        }
    });
    send_json(&mut stdin, &init_request);

    // The first line from stdout MUST be the JSON-RPC response.
    let first_line = read_json_line_from(&reader.rx);
    let parsed: serde_json::Value =
        serde_json::from_str(&first_line).expect("first stdout line must be valid JSON");

    assert_eq!(
        parsed["id"], 1,
        "first stdout line must be the initialize response (id=1), got: {}",
        parsed
    );
    assert!(
        parsed.get("result").is_some(),
        "initialize response must have result, got: {}",
        parsed
    );

    // Clean up.
    drop(stdin);
    let _ = child.wait();
}
