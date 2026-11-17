# Frontend 开发指南

## 安装和开发

```powershell
cd D:\dingsun\esRAGPro\frontend
npm install --registry=https://registry.npmmirror.com
npm run dev
```

## 构建

```powershell
cd D:\dingsun\esRAGPro\frontend
npm run build
```

## 修改边界

- 页面只通过 `src/api/index.js` 调用后端。
- 新增 Memory Plane 页面前，应先确认是否真的需要 Web 管理面，避免把 TUI/MCP 核心能力重新绑定到 Frontend。
- 图谱可视化是诊断和管理能力，不应成为 Memory Core 的事实来源。
- Frontend 删除前必须先确认文档管理、评测、图谱检查和生命周期运维已有 API/TUI 替代。
