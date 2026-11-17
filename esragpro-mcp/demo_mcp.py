#!/usr/bin/env python3
"""
MCP Gateway 演示脚本
展示 AI Agent 如何通过 MCP 协议调用知识库
"""
import subprocess
import json
import sys

def call_mcp(method, params, id=1):
    """调用 MCP Gateway"""
    request = {
        "jsonrpc": "2.0",
        "method": method,
        "params": params,
        "id": id
    }
    
    # 启动 MCP Gateway 并发送请求
    process = subprocess.Popen(
        ["target/release/esragpro-mcp.exe", "stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    
    # 发送请求
    process.stdin.write(json.dumps(request) + "\n")
    process.stdin.flush()
    
    # 读取响应
    response = process.stdout.readline()
    process.terminate()
    
    return json.loads(response) if response else None

# 演示 1: 列出可用工具
print("=== MCP 工具列表 ===")
result = call_mcp("tools/list", {})
if result and "result" in result:
    tools = result["result"].get("tools", [])
    print(f"可用工具: {len(tools)}")
    for tool in tools:
        print(f"  - {tool['name']}: {tool.get('description', '')[:50]}")

# 演示 2: 检索证据
print("\n=== 检索证据 ===")
search_params = {
    "name": "kb.search_evidence",
    "arguments": {
        "knowledge_base_ids": ["default"],
        "query": "合同违约怎么处理",
        "top_k": 3
    }
}
result = call_mcp("tools/call", search_params, id=2)
if result and "result" in result:
    content = result["result"].get("content", [])
    if content:
        print(f"检索结果: {content[0].get('text', '')[:200]}")

print("\n✅ MCP Gateway 工作正常")
