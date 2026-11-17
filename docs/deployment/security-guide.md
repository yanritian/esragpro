# 安全配置完整指南

## 安全架构图

```
                        ┌─────────────────────────────────────────┐
                        │            互联网 (公网)                 │
                        └───────────────────┬─────────────────────┘
                                            │
                    ┌───────────────────────┴───────────────────────┐
                    │                                               │
                    ▼                                               ▼
            ┌───────────────┐                               ┌───────────────┐
            │   Fail2ban    │                               │   防火墙      │
            │  防暴力破解    │                               │  UFW/云安全组 │
            └───────┬───────┘                               └───────┬───────┘
                    │                                               │
                    └───────────────────────┬───────────────────────┘
                                            │
                                            ▼
                    ┌─────────────────────────────────────────┐
                    │   Nginx 反向代理 (唯一入口)              │
                    │   ├── HTTPS (TLS 1.2/1.3)               │
                    │   ├── 速率限制 (10 req/s)               │
                    │   ├── 并发限制 (20 conn/IP)             │
                    │   ├── IP 白名单 (可选)                  │
                    │   ├── 基础认证 (管理后台)               │
                    │   └── 安全头                            │
                    │   端口: 80 → 443                        │
                    └───────────────────┬─────────────────────┘
                                        │ (内部网络)
                                        ▼
                    ┌─────────────────────────────────────────┐
                    │              Backend                     │
                    │           (FastAPI)                      │
                    │           端口: 8000 (内部)              │
                    └───────────────────┬─────────────────────┘
                                        │
                    ┌───────────────────┼───────────────────┐
                    │                   │                   │
                    ▼                   ▼                   ▼
            ┌───────────┐       ┌───────────┐       ┌───────────┐
            │    ES     │       │ PostgreSQL│       │   Redis   │
            │  +认证    │       │  +认证    │       │  +密码    │
            │ (内部)    │       │ (内部)    │       │ (内部)    │
            └───────────┘       └───────────┘       └───────────┘
                    │                   │                   │
                    └───────────────────┴───────────────────┘
                            (完全隔离，外部无法访问)
```

---

## 一、安全措施清单

### ✅ 已实现（配置即用）

| 层级 | 措施 | 状态 | 说明 |
|------|------|------|------|
| 网络层 | 端口隔离 | ✅ | 只有 80/443 对外暴露 |
| 网络层 | 内部网络 | ✅ | ES/DB/Redis 完全隔离 |
| 传输层 | HTTPS | ✅ | TLS 1.2/1.3 加密 |
| 传输层 | HSTS | ✅ | 强制 HTTPS |
| 应用层 | 速率限制 | ✅ | API 10/s, 上传 2/m |
| 应用层 | 并发限制 | ✅ | 每 IP 20 连接 |
| 应用层 | 安全头 | ✅ | CSP, X-Frame-Options 等 |
| 应用层 | 请求过滤 | ✅ | 禁止敏感文件访问 |
| 认证层 | JWT | ✅ | Token 认证 |
| 认证层 | 基础认证 | ✅ | 管理后台双因素 |

### ✅ 可选增强（脚本配置）

| 层级 | 措施 | 脚本 | 说明 |
|------|------|------|------|
| 网络层 | IP 白名单 | setup-security.sh | 只允许指定 IP |
| 网络层 | Fail2ban | setup-security.sh | 自动封禁攻击 IP |
| 网络层 | 防火墙 | setup-security.sh | UFW 配置 |
| 认证层 | 强密码 | setup-security.sh | 自动生成 |
| 存储层 | ES 认证 | docker-compose.secure.yml | 启用 xpack.security |
| 存储层 | Redis 密码 | docker-compose.secure.yml | requirepass |

---

## 二、快速配置

### 步骤 1：使用安全版配置

```bash
cd /opt/esragpro

# 复制安全版配置
cp frontend/nginx-secure.conf frontend/nginx.conf

# 使用安全版 docker-compose
docker compose -f docker-compose.secure.yml up -d --build
```

### 步骤 2：运行安全配置脚本

```bash
chmod +x setup-security.sh
./setup-security.sh
```

脚本会自动：
- 生成强密码
- 配置 Fail2ban
- 配置防火墙
- 生成 HTTPS 证书（自签名）
- 创建管理后台认证

### 步骤 3：配置正式 HTTPS 证书

```bash
# 使用 Let's Encrypt 免费证书
./setup-https.sh 你的域名 你的邮箱

# 示例
./setup-https.sh kb.example.com admin@example.com
```

### 步骤 4：配置 IP 白名单（可选但推荐）

编辑 `nginx-whitelist.conf`：

```nginx
# 只允许这些 IP 访问
allow 1.2.3.4;        # 公司 IP
allow 10.0.0.0/8;     # 内网
deny all;             # 拒绝其他
```

在 `nginx.conf` 的 server 块中添加：

```nginx
server {
    include /opt/esragpro/nginx-whitelist.conf;
    # ...
}
```

---

## 三、安全配置文件说明

| 文件 | 用途 |
|------|------|
| `docker-compose.secure.yml` | 安全版 Docker 编排 |
| `frontend/nginx-secure.conf` | 安全版 Nginx 配置 |
| `setup-security.sh` | 一键安全配置脚本 |
| `setup-https.sh` | HTTPS 证书配置脚本 |

---

## 四、运维命令

### 查看安全状态

```bash
# 查看被封禁的 IP
fail2ban-client status esragpro-api

# 查看防火墙状态
ufw status

# 查看访问日志（检查异常请求）
tail -f /var/log/nginx/access.log

# 查看错误日志
tail -f /var/log/nginx/error.log
```

### 解封 IP

```bash
# 解封特定 IP
fail2ban-client set esragpro-api unbanip 1.2.3.4
```

### 更新密码

```bash
# 编辑 .env 文件
nano /opt/esragpro/.env

# 重启服务
docker compose -f docker-compose.secure.yml restart
```

### 查看安全报告

```bash
cat /opt/esragpro/SECURITY.md
```

---

## 五、安全检查清单

### 部署前检查

- [ ] 所有默认密码已修改
- [ ] JWT_SECRET_KEY 已设置（32+ 位）
- [ ] LLM_API_KEY 已配置
- [ ] 防火墙已配置（只开放 22/80/443）
- [ ] HTTPS 证书已配置

### 运行时检查

- [ ] Fail2ban 正常运行
- [ ] 日志无异常请求
- [ ] 定期备份已配置
- [ ] 系统补丁已更新

### 定期维护

- [ ] 每周检查访问日志
- [ ] 每月检查安全更新
- [ ] 每季度更换敏感密钥

---

## 六、常见攻击防护

### DDoS 攻击

| 措施 | 说明 |
|------|------|
| 速率限制 | Nginx 已配置 |
| 云 DDoS | 开启云厂商免费防护 |
| CDN | 使用阿里云 CDN / 腾讯云 CDN |

### SQL 注入

| 措施 | 说明 |
|------|------|
| 参数化查询 | 代码已使用 ORM |
| 输入验证 | Pydantic 验证 |
| WAF | 开启云 WAF |

### XSS 攻击

| 措施 | 说明 |
|------|------|
| CSP 头 | 已配置 Content-Security-Policy |
| 输入转义 | Vue 自动转义 |
| 输出编码 | 后端 API 返回纯文本 |

### 暴力破解

| 措施 | 说明 |
|------|------|
| Fail2ban | 自动封禁 |
| 速率限制 | 登录接口 5次/分钟 |
| 强密码 | 强制复杂密码 |

---

## 七、成本估算

### 零成本安全方案

| 措施 | 成本 |
|------|------|
| HTTPS (Let's Encrypt) | 免费 |
| Fail2ban | 免费 |
| UFW 防火墙 | 免费 |
| 云安全组 | 免费 |
| 速率限制 | 免费 |
| **总计** | **0 元** |

### 增强安全方案

| 措施 | 费用 |
|------|------|
| 云 WAF | 200-500 元/月 |
| CDN 加速 | 50-200 元/月 |
| 安全审计服务 | 500+ 元/月 |

---

## 八、应急响应

### 发现攻击时

```bash
# 1. 立即查看攻击来源
tail -f /var/log/nginx/access.log | grep -i "error\|attack"

# 2. 手动封禁 IP
ufw deny from 可疑IP

# 3. 查看服务状态
docker compose ps

# 4. 必要时重启服务
docker compose restart

# 5. 检查数据完整性
curl http://localhost:9200/_cat/indices?v
```

### 数据恢复

```bash
# 恢复 ES 数据
docker exec esragpro-es elasticsearch-restore ...

# 恢复 PostgreSQL
docker exec esragpro-postgres pg_restore ...
```

---

## 九、总结

### 推荐配置

```
零成本高安全配置：
├── HTTPS (Let's Encrypt)
├── Fail2ban 防暴力破解
├── UFW 防火墙
├── IP 白名单 (仅公司网络)
├── 强密码策略
└── 定期备份

可抵御攻击：
├── ✅ DDoS (小规模)
├── ✅ 暴力破解
├── ✅ SQL 注入
├── ✅ XSS 攻击
├── ✅ 中间人攻击
└── ✅ 端口扫描
```

### 安全级别评估

| 配置 | 安全级别 | 适用场景 |
|------|---------|---------|
| 基础配置 | 🟡 中等 | 内网测试 |
| + 安全脚本 | 🟢 高 | 企业生产 |
| + 云 WAF | 🟢 很高 | 高安全要求 |