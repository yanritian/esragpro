//! Release-gate protocol checks against the built MCP binary.

use std::io::{self, BufRead, BufReader, Write};
use std::process::{Child, ChildStdin, Command, ExitStatus, Stdio};
use std::sync::mpsc::{self, Receiver};
use std::thread;
use std::time::{Duration, Instant};

const READ_TIMEOUT: Duration = Duration::from_secs(10);
const CHILD_CLEANUP_TIMEOUT: Duration = Duration::from_secs(3);
const HTTP_READINESS_TIMEOUT: Duration = Duration::from_secs(10);

struct LineReader {
    rx: Receiver<Result<String, String>>,
    _handle: thread::JoinHandle<()>,
}

impl LineReader {
    fn new(stdout: std::process::ChildStdout) -> Self {
        let (tx, rx) = mpsc::channel();
        let handle = thread::spawn(move || {
            let mut reader = BufReader::new(stdout);
            let mut line = String::new();
            loop {
                line.clear();
                match reader.read_line(&mut line) {
                    Ok(0) => {
                        let _ = tx.send(Err("stdout closed".to_string()));
                        return;
                    }
                    Ok(_) => {}
                    Err(error) => {
                        let _ = tx.send(Err(format!("stdout read failed: {error}")));
                        return;
                    }
                }

                let line = line.trim().to_string();
                if line.is_empty() {
                    continue;
                }
                if tx.send(Ok(line)).is_err() {
                    return;
                }
            }
        });
        Self {
            rx,
            _handle: handle,
        }
    }

    fn next_json(&self) -> serde_json::Value {
        let line = self
            .rx
            .recv_timeout(READ_TIMEOUT)
            .expect("timed out waiting for MCP stdout")
            .expect("MCP closed stdout unexpectedly");
        assert!(
            line.starts_with('{'),
            "MCP stdout contained a non-JSON line: {line}"
        );
        serde_json::from_str(&line).expect("MCP stdout contained invalid JSON")
    }
}

struct OwnedChild {
    child: Child,
}

impl OwnedChild {
    fn new(child: Child) -> Self {
        Self { child }
    }

    fn try_wait(&mut self) -> io::Result<Option<ExitStatus>> {
        self.child.try_wait()
    }

    fn wait_for_exit(&mut self, timeout: Duration) -> io::Result<ExitStatus> {
        let deadline = Instant::now() + timeout;
        loop {
            match self.child.try_wait()? {
                Some(status) => return Ok(status),
                None if Instant::now() >= deadline => {
                    return Err(io::Error::new(
                        io::ErrorKind::TimedOut,
                        "timed out waiting for child exit",
                    ));
                }
                None => thread::sleep(Duration::from_millis(25)),
            }
        }
    }

    fn terminate(&mut self) -> io::Result<()> {
        if self.child.try_wait()?.is_none() {
            if let Err(error) = self.child.kill() {
                if self.child.try_wait()?.is_none() {
                    return Err(error);
                }
            }
        }
        self.wait_for_exit(CHILD_CLEANUP_TIMEOUT).map(|_| ())
    }
}

impl Drop for OwnedChild {
    fn drop(&mut self) {
        let _ = self.terminate();
    }
}

fn send_json(stdin: &mut ChildStdin, message: serde_json::Value) {
    writeln!(
        stdin,
        "{}",
        serde_json::to_string(&message).expect("serialize JSON-RPC message")
    )
    .expect("write JSON-RPC request");
    stdin.flush().expect("flush JSON-RPC request");
}

fn next_response(reader: &LineReader, id: i64) -> serde_json::Value {
    for _ in 0..16 {
        let message = reader.next_json();
        if message.get("id").and_then(serde_json::Value::as_i64) == Some(id) {
            return message;
        }
    }
    panic!("did not receive JSON-RPC response with id {id}");
}

fn spawn_stdio() -> (OwnedChild, ChildStdin, LineReader) {
    let mut child = Command::new(env!("CARGO_BIN_EXE_esragpro-mcp"))
        .arg("stdio")
        .env("ESRAGPRO_BACKEND_URL", "http://127.0.0.1:1/api/v1")
        .env("RUST_LOG", "info")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .expect("spawn release MCP stdio binary");

    let stdin = child.stdin.take().expect("stdio stdin");
    let stdout = child.stdout.take().expect("stdio stdout");
    (OwnedChild::new(child), stdin, LineReader::new(stdout))
}

#[test]
fn release_stdio_initialize_list_and_call() {
    let (mut child, mut stdin, reader) = spawn_stdio();

    send_json(
        &mut stdin,
        serde_json::json!({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {
                    "name": "release-smoke",
                    "version": env!("CARGO_PKG_VERSION")
                }
            }
        }),
    );
    let initialize = next_response(&reader, 1);
    assert!(
        initialize.get("result").is_some(),
        "initialize failed: {initialize}"
    );

    send_json(
        &mut stdin,
        serde_json::json!({
            "jsonrpc": "2.0",
            "method": "notifications/initialized"
        }),
    );
    send_json(
        &mut stdin,
        serde_json::json!({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {}
        }),
    );
    let list = next_response(&reader, 2);
    let tools = list["result"]["tools"]
        .as_array()
        .expect("tools/list result must include tools");
    assert!(!tools.is_empty(), "tools/list returned no tools");

    // Use an unknown tool so the call exercises MCP dispatch without relying
    // on a live backend or mutating external state. A JSON-RPC error is the
    // expected, valid response for this bounded negative call.
    send_json(
        &mut stdin,
        serde_json::json!({
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "__release_smoke_unknown_tool__",
                "arguments": {}
            }
        }),
    );
    let call = next_response(&reader, 3);
    assert!(
        call.get("result").is_some() || call.get("error").is_some(),
        "tools/call did not return a JSON-RPC result or error: {call}"
    );

    drop(stdin);
    let status = child
        .wait_for_exit(CHILD_CLEANUP_TIMEOUT)
        .expect("wait for stdio MCP child");
    assert!(
        !status.code().is_some_and(|code| code == 101),
        "stdio MCP child panicked: {status}"
    );
}

#[cfg(feature = "http")]
#[tokio::test]
async fn release_http_metadata_and_unauthorized_mcp_boundary() -> anyhow::Result<()> {
    let listener = std::net::TcpListener::bind("127.0.0.1:0")?;
    let addr = listener.local_addr()?;
    drop(listener);

    let child = Command::new(env!("CARGO_BIN_EXE_esragpro-mcp"))
        .arg("http")
        .arg("--addr")
        .arg(addr.to_string())
        .env("ESRAGPRO_BACKEND_URL", "http://127.0.0.1:1/api/v1")
        .env("ESRAGPRO_HTTP_ENABLED", "true")
        .env("RUST_LOG", "info")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn()?;
    let mut child = OwnedChild::new(child);

    let client = reqwest::Client::builder()
        .connect_timeout(Duration::from_millis(250))
        .timeout(Duration::from_secs(2))
        .build()?;
    let base = format!("http://{addr}");

    let readiness_deadline = tokio::time::Instant::now() + HTTP_READINESS_TIMEOUT;
    let metadata = loop {
        if let Some(status) = child.try_wait()? {
            anyhow::bail!("HTTP MCP child exited before readiness: {status}");
        }
        let remaining = readiness_deadline.saturating_duration_since(tokio::time::Instant::now());
        if remaining == Duration::ZERO {
            anyhow::bail!("HTTP MCP child did not become ready before the deadline");
        }
        match tokio::time::timeout(
            remaining.min(Duration::from_secs(2)),
            client
                .get(format!("{base}/.well-known/oauth-protected-resource"))
                .send(),
        )
        .await
        {
            Ok(Ok(response)) if response.status().is_success() => break response,
            Ok(Ok(_)) | Ok(Err(_)) | Err(_) => {}
        }
        let remaining = readiness_deadline.saturating_duration_since(tokio::time::Instant::now());
        if remaining > Duration::ZERO {
            tokio::time::sleep(remaining.min(Duration::from_millis(50))).await;
        }
    };

    let metadata_body: serde_json::Value = metadata.json().await?;
    assert_eq!(
        metadata_body["resource"],
        format!("{base}/mcp"),
        "metadata must advertise the MCP resource"
    );
    assert!(
        metadata_body["authorization_servers"]
            .as_array()
            .is_some_and(|servers| !servers.is_empty()),
        "metadata must advertise an authorization server"
    );

    let unauthorized = client
        .post(format!("{base}/mcp"))
        .header("content-type", "application/json")
        .header("accept", "application/json, text/event-stream")
        .body(
            serde_json::json!({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "release-smoke", "version": env!("CARGO_PKG_VERSION")}
                }
            })
            .to_string(),
        )
        .send()
        .await?;
    assert_eq!(
        unauthorized.status(),
        reqwest::StatusCode::UNAUTHORIZED,
        "HTTP MCP endpoint must reject unauthenticated initialize"
    );

    child.terminate()?;
    Ok(())
}

#[cfg(feature = "http")]
async fn read_sse_json(mut response: reqwest::Response) -> anyhow::Result<serde_json::Value> {
    let deadline = tokio::time::Instant::now() + Duration::from_secs(3);
    let mut body = String::new();

    while tokio::time::Instant::now() < deadline {
        let remaining = deadline.saturating_duration_since(tokio::time::Instant::now());
        let chunk =
            tokio::time::timeout(remaining.min(Duration::from_millis(500)), response.chunk())
                .await??;
        let Some(chunk) = chunk else {
            break;
        };
        body.push_str(std::str::from_utf8(&chunk)?);

        if let Some(data) = body
            .lines()
            .find_map(|line| line.strip_prefix("data:"))
            .map(str::trim)
        {
            return Ok(serde_json::from_str(data)?);
        }
    }

    anyhow::bail!("timed out waiting for Streamable HTTP SSE data: {body:?}");
}

#[cfg(feature = "http")]
#[tokio::test]
async fn release_http_authenticated_initialize_list_call_when_configured() -> anyhow::Result<()> {
    let required = [
        "MCP_SMOKE_HTTP_BEARER_TOKEN",
        "MCP_SMOKE_OAUTH_ISSUER",
        "MCP_SMOKE_OAUTH_AUDIENCE",
        "MCP_SMOKE_JWKS_URL",
    ];
    let missing: Vec<_> = required
        .iter()
        .copied()
        .filter(|name| {
            std::env::var(name)
                .ok()
                .filter(|value| !value.is_empty())
                .is_none()
        })
        .collect();

    if !missing.is_empty() {
        eprintln!(
            "MISSING_PREREQUISITE: authenticated HTTP protocol test requires {}",
            missing.join(", ")
        );
        return Ok(());
    }

    let listener = std::net::TcpListener::bind("127.0.0.1:0")?;
    let addr = listener.local_addr()?;
    drop(listener);

    let child = Command::new(env!("CARGO_BIN_EXE_esragpro-mcp"))
        .arg("http")
        .arg("--addr")
        .arg(addr.to_string())
        .env("ESRAGPRO_BACKEND_URL", "http://127.0.0.1:1/api/v1")
        .env("ESRAGPRO_HTTP_ENABLED", "true")
        .env(
            "ESRAGPRO_OAUTH_ISSUER",
            std::env::var("MCP_SMOKE_OAUTH_ISSUER")?,
        )
        .env(
            "ESRAGPRO_OAUTH_AUDIENCE",
            std::env::var("MCP_SMOKE_OAUTH_AUDIENCE")?,
        )
        .env("ESRAGPRO_JWKS_URL", std::env::var("MCP_SMOKE_JWKS_URL")?)
        .env("ESRAGPRO_MANAGEMENT_PROFILE_ENABLED", "true")
        .env("RUST_LOG", "info")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()?;
    let mut child = OwnedChild::new(child);

    let result = async {
        let client = reqwest::Client::builder()
            .connect_timeout(Duration::from_millis(250))
            .timeout(Duration::from_secs(5))
            .build()?;
        let base = format!("http://{addr}");
        let token = std::env::var("MCP_SMOKE_HTTP_BEARER_TOKEN")?;

        let readiness_deadline = tokio::time::Instant::now() + HTTP_READINESS_TIMEOUT;
        let mut initialize_response = None;
        loop {
            if let Some(status) = child.try_wait()? {
                anyhow::bail!("authenticated HTTP child exited before readiness: {status}");
            }
            let remaining =
                readiness_deadline.saturating_duration_since(tokio::time::Instant::now());
            if remaining == Duration::ZERO {
                break;
            }
            match tokio::time::timeout(
                remaining.min(Duration::from_secs(5)),
                client
                    .post(format!("{base}/mcp"))
                    .bearer_auth(&token)
                    .header("content-type", "application/json")
                    .header("accept", "application/json, text/event-stream")
                    .header("MCP-Protocol-Version", "2025-11-25")
                    .body(
                        serde_json::json!({
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "initialize",
                            "params": {
                                "protocolVersion": "2025-11-25",
                                "capabilities": {},
                                "clientInfo": {
                                    "name": "release-smoke",
                                    "version": env!("CARGO_PKG_VERSION")
                                }
                            }
                        })
                        .to_string(),
                    )
                    .send(),
            )
            .await
            {
                Ok(Ok(response)) if response.status().is_success() => {
                    initialize_response = Some(response);
                    break;
                }
                Ok(Ok(_)) | Ok(Err(_)) | Err(_) => {}
            }
            let remaining =
                readiness_deadline.saturating_duration_since(tokio::time::Instant::now());
            if remaining > Duration::ZERO {
                tokio::time::sleep(remaining.min(Duration::from_millis(50))).await;
            }
        }

        let initialize_response =
            initialize_response.ok_or_else(|| anyhow::anyhow!("authenticated HTTP initialize never became ready"))?;
        let session_id = initialize_response
            .headers()
            .get("mcp-session-id")
            .and_then(|value| value.to_str().ok())
            .ok_or_else(|| anyhow::anyhow!("initialize response missing Mcp-Session-Id"))?
            .to_string();
        let initialize = read_sse_json(initialize_response).await?;
        anyhow::ensure!(
            initialize["id"] == 1 && initialize["result"].is_object(),
            "authenticated HTTP initialize failed: {initialize}"
        );

        let initialized = client
            .post(format!("{base}/mcp"))
            .bearer_auth(&token)
            .header("content-type", "application/json")
            .header("accept", "application/json, text/event-stream")
            .header("Mcp-Session-Id", &session_id)
            .body(
                serde_json::json!({
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized"
                })
                .to_string(),
            )
            .send()
            .await?;
        anyhow::ensure!(
            initialized.status().is_success(),
            "notifications/initialized failed: {}",
            initialized.status()
        );

        let list_response = client
            .post(format!("{base}/mcp"))
            .bearer_auth(&token)
            .header("content-type", "application/json")
            .header("accept", "application/json, text/event-stream")
            .header("Mcp-Session-Id", &session_id)
            .body(
                serde_json::json!({
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/list",
                    "params": {}
                })
                .to_string(),
            )
            .send()
            .await?;
        anyhow::ensure!(
            list_response.status().is_success(),
            "authenticated HTTP tools/list failed: {}",
            list_response.status()
        );
        let list = read_sse_json(list_response).await?;
        anyhow::ensure!(
            list["id"] == 2 && list["result"]["tools"].as_array().is_some_and(|tools| !tools.is_empty()),
            "authenticated HTTP tools/list failed: {list}"
        );

        let call_response = client
            .post(format!("{base}/mcp"))
            .bearer_auth(&token)
            .header("content-type", "application/json")
            .header("accept", "application/json, text/event-stream")
            .header("Mcp-Session-Id", &session_id)
            .body(
                serde_json::json!({
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "__release_smoke_unknown_tool__",
                        "arguments": {}
                    }
                })
                .to_string(),
            )
            .send()
            .await?;
        anyhow::ensure!(
            call_response.status().is_success(),
            "authenticated HTTP tools/call failed: {}",
            call_response.status()
        );
        let call = read_sse_json(call_response).await?;
        anyhow::ensure!(
            call["id"] == 3 && call["error"].is_object(),
            "unknown authenticated HTTP tools/call must return a JSON-RPC error: {call}"
        );

        if let Ok(negative_token) = std::env::var("MCP_SMOKE_NEGATIVE_BEARER_TOKEN") {
            let management_response = client
                .post(format!("{base}/mcp"))
                .bearer_auth(negative_token)
                .header("content-type", "application/json")
                .header("accept", "application/json, text/event-stream")
                .header("Mcp-Session-Id", &session_id)
                .body(
                    serde_json::json!({
                        "jsonrpc": "2.0",
                        "id": 4,
                        "method": "tools/call",
                        "params": {
                            "name": "admin.create_ingest_job",
                            "arguments": {
                                "knowledge_base_id": "00000000-0000-0000-0000-000000000001",
                                "source_ref": "release-smoke",
                                "idempotency_key": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
                            }
                        }
                    })
                    .to_string(),
                )
                .send()
                .await?;
            anyhow::ensure!(
                management_response.status().is_success(),
                "management ACL-negative call failed: {}",
                management_response.status()
            );
            let management = read_sse_json(management_response).await?;
            anyhow::ensure!(
                management["id"] == 4 && management["error"].is_object(),
                "management ACL-negative call must return a JSON-RPC error: {management}"
            );
        } else {
            eprintln!(
                "MISSING_PREREQUISITE: MCP_SMOKE_NEGATIVE_BEARER_TOKEN not provided; management-scope ACL-negative check not claimed"
            );
        }

        Ok::<(), anyhow::Error>(())
    }
    .await;

    child.terminate()?;
    result
}
