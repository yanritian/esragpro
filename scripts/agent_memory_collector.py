"""
esRAGPro Agent Memory Collector
================================
把本机各编码 Agent（Claude Code / Codex / PI）的对话增量采集进 esRAGPro
memory-plane（POST /api/v1/memory-plane/remember）。

设计要点:
- 增量: 每个会话文件记录字节偏移, 只读新增部分 (state 文件)
- 容错: 解析失败的行跳过; 后端不可用时偏移不推进, 下次重试
- 限流: 每次运行最多推送 MAX_ITEMS_PER_RUN 条, 防止首次运行洪水
- 隐私: 全程只走 http://127.0.0.1:8000, 数据不出本机

由计划任务 esragpro-memory-collector 每 5 分钟调用一次。
"""

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

BACKEND_REMEMBER_URL = "http://127.0.0.1:8000/api/v1/memory-plane/remember"
STATE_PATH = Path(r"D:\dingsun\esRAGPro\scripts\.collector-state.json")
MAX_ITEMS_PER_RUN = 100
CHUNK_CHARS = 1200
HOME = Path(r"C:\Users\11239")

SOURCES = [
    # (agent 名, glob 列表)
    ("claude-code", [HOME / ".claude" / "projects" / "**" / "*.jsonl"]),
    ("codex", [HOME / ".codex" / "sessions" / "**" / "rollout-*.jsonl"]),
    ("pi", [HOME / ".pi" / "agent" / "sessions" / "**" / "*.jsonl",
            HOME / ".pi" / "agent" / "sessions" / "**" / "*.json"]),
]


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def extract_text_claude(obj: dict) -> str | None:
    """Claude Code: {"type":"user"/"assistant","message":{"content": str|list}}"""
    if obj.get("type") not in {"user", "assistant"}:
        return None
    msg = obj.get("message") or {}
    role = msg.get("role", obj["type"])
    content = msg.get("content")
    parts: list[str] = []
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
    text = "\n".join(p for p in parts if p).strip()
    if not text or text.startswith("<") or text.startswith("Caveat:"):
        return None
    return f"[{role}] {text}"


def extract_text_codex(obj: dict) -> str | None:
    """Codex rollout: {"type":"response_item"/"event_msg","payload":{...}}"""
    p = obj.get("payload") or {}
    ptype = p.get("type", "")
    role = p.get("role", "")
    parts: list[str] = []
    content = p.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                t = block.get("text") or block.get("input_text") or ""
                if isinstance(t, str):
                    parts.append(t)
    text = "\n".join(x for x in parts if x).strip()
    if not text:
        return None
    if ptype == "message" and role in {"user", "assistant"}:
        return f"[{role}] {text}"
    if ptype == "agent_reasoning" or "reasoning" in ptype:
        return None  # 思考过程不入库
    return None


def extract_text_pi(obj: dict) -> str | None:
    """PI session: 容错提取 role+text"""
    if not isinstance(obj, dict):
        return None
    msg = obj.get("message") or obj
    role = msg.get("role", "")
    content = msg.get("content")
    parts: list[str] = []
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
    text = "\n".join(p for p in parts if p).strip()
    if not text or role not in {"user", "assistant"}:
        return None
    return f"[{role}] {text}"


EXTRACTORS = {"claude-code": extract_text_claude, "codex": extract_text_codex, "pi": extract_text_pi}


def chunk(text: str, size: int = CHUNK_CHARS):
    for i in range(0, len(text), size):
        yield text[i:i + size]


def post_remember(client: httpx.Client, content: str, agent: str, project: str, session_file: str) -> bool:
    payload = {
        "content": content,
        "metadata": {
            "agent": agent,
            "project": project,
            "session_file": session_file,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "kind": "agent-conversation",
        },
    }
    try:
        r = client.post(BACKEND_REMEMBER_URL, json=payload, timeout=20)
        return r.status_code in (200, 201)
    except Exception as e:
        print(f"  ! backend unreachable: {e}", file=sys.stderr)
        return False


def main() -> int:
    state = load_state()
    pushed = 0
    client = httpx.Client()
    try:
        for agent, globs in SOURCES:
            extractor = EXTRACTORS[agent]
            files: list[Path] = []
            for g in globs:
                root = Path(str(g).split("**")[0])
                pattern = g.name
                if root.exists():
                    files.extend(p for p in root.rglob(pattern) if p.is_file())
            for f in sorted(set(files)):
                key = f"{agent}::{str(f)}"
                offset = int(state.get(key, 0))
                try:
                    size = f.stat().st_size
                except OSError:
                    continue
                if size <= offset:
                    continue
                pending: list[str] = []
                with f.open("r", encoding="utf-8", errors="replace") as fh:
                    fh.seek(offset)
                    raw = fh.read()
                new_offset = offset + len(raw.encode("utf-8", errors="replace"))
                project = f.parent.name
                for line in raw.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    try:
                        text = extractor(obj)
                    except Exception:
                        text = None
                    if text:
                        pending.append(text)
                if not pending:
                    state[key] = new_offset
                    continue
                ok_all = True
                for text in pending:
                    if pushed >= MAX_ITEMS_PER_RUN:
                        ok_all = False
                        break
                    for piece in chunk(text):
                        if not post_remember(client, piece, agent, project, f.name):
                            ok_all = False
                            break
                        pushed += 1
                    if not ok_all:
                        break
                if ok_all:
                    state[key] = new_offset
                if pushed >= MAX_ITEMS_PER_RUN:
                    print(f"run cap {MAX_ITEMS_PER_RUN} reached; rest next cycle")
                    save_state(state)
                    return 0
        save_state(state)
        print(f"collector done: pushed {pushed} items")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
