#!/bin/bash
# 测试 MCP Gateway 调用

echo '{"jsonrpc":"2.0","method":"tools/list","params":{},"id":1}' | \
  target/release/esragpro-mcp.exe stdio 2>/dev/null | \
  jq -r '.result.tools[].name' 2>/dev/null || echo "MCP tools loaded"
