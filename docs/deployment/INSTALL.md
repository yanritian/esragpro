# esRAGPro 完整安装指南

## 目录

1. [环境要求](#一环境要求)
2. [购买云服务器](#二购买云服务器)
3. [连接服务器](#三连接服务器)
4. [安装 Docker](#四安装-docker)
5. [上传代码](#五上传代码)
6. [配置环境变量](#六配置环境变量)
7. [启动服务](#七启动服务)
8. [配置 HTTPS](#八配置-https)
9. [安全加固](#九安全加固)
10. [验证部署](#十验证部署)
11. [常见问题](#十一常见问题)

---

## 一、环境要求

### 服务器配置

| 项目 | 最低要求 | 推荐配置 |
|------|---------|---------|
| CPU | 2 核 | 4 核+ |
| 内存 | 8 GB | 16 GB+ |
| 硬盘 | 50 GB SSD | 200 GB SSD |
| 系统 | Ubuntu 22.04 | Ubuntu 22.04 |
| 带宽 | 3 Mbps | 10 Mbps+ |

### 你需要准备

- [ ] 云服务器一台（阿里云/腾讯云/华为云）
- [ ] 域名一个（可选，用于 HTTPS）
- [ ] 智谱 AI API Key（推荐）或其他 LLM API Key

---

## 二、购买云服务器

### 2.1 选择云厂商

推荐：
- **阿里云**：https://www.aliyun.com
- **腾讯云**：https://cloud.tencent.com
- **华为云**：https://www.huaweicloud.com

### 2.2 选择配置

```
实例规格：2核8G（测试）/ 4核16G（生产）
镜像：Ubuntu 22.04 64位
系统盘：50GB SSD
带宽：3Mbps（测试）/ 10Mbps（生产）
```

### 2.3 安全组配置

购买时或购买后，开放以下端口：

| 端口 | 用途 | 来源 |
|------|------|------|
| 22 | SSH | 你的 IP |
| 80 | HTTP | 0.0.0.0/0 |
| 443 | HTTPS | 0.0.0.0/0 |

**注意：不要开放 9200、5432、6379、8000 端口！**

---

## 三、连接服务器

### Windows 用户

```powershell
# 打开 PowerShell 或 Git Bash
ssh root@你的服务器IP

# 首次连接输入 yes，然后输入密码
```

### Mac/Linux 用户

```bash
ssh root@你的服务器IP
```

### 建议设置 SSH 密钥登录（可选但推荐）

```bash
# 本地执行（Windows 用 Git Bash）
ssh-keygen -t ed25519
ssh-copy-id root@你的服务器IP

# 之后再连接就不需要密码了
ssh root@你的服务器IP
```

---

## 四、安装 Docker

### 4.1 一键安装

连接服务器后，执行：

```bash
# 更新系统
apt update && apt upgrade -y

# 安装 Docker
curl -fsSL https://get.docker.com | bash

# 安装 Docker Compose 插件
apt install docker-compose-plugin -y

# 启动 Docker
systemctl start docker
systemctl enable docker
```

### 4.2 配置 Docker 镜像加速（国内必做）

```bash
# 创建配置文件
mkdir -p /etc/docker

cat > /etc/docker/daemon.json <<EOF
{
    "registry-mirrors": [
        "https://docker.mirrors.ustc.edu.cn",
        "https://hub-mirror.c.163.com"
    ]
}
EOF

# 重启 Docker
systemctl daemon-reload
systemctl restart docker
```

### 4.3 验证安装

```bash
docker --version
# 输出: Docker version 24.x.x, build xxx

docker compose version
# 输出: Docker Compose version v2.x.x
```

---

## 五、上传代码

### 方式一：Git 克隆（推荐）

```bash
# 安装 Git
apt install git -y

# 创建目录
mkdir -p /opt/esragpro
cd /opt/esragpro

# 克隆代码（替换为你的仓库地址）
git clone https://github.com/你的用户名/esRAGPro.git .
```

### 方式二：SCP 上传

```bash
# 在本地 Windows 执行（PowerShell）
# 先压缩项目文件夹

# 上传
scp esRAGPro.zip root@服务器IP:/opt/

# 在服务器上解压
ssh root@服务器IP
cd /opt
unzip esRAGPro.zip
mv esRAGPro-main esragpro
```

### 方式三：手动创建

如果以上方式都不可行，可以在服务器上手动创建文件：

```bash
mkdir -p /opt/esragpro
cd /opt/esragpro

# 创建必要的目录结构
mkdir -p backend/app/{core,services,models,api}
mkdir -p frontend/src/{views,api,router}
mkdir -p docs certs uploads
```

---

## 六、配置环境变量

### 6.1 复制配置模板

```bash
cd /opt/esragpro
cp .env.example .env
```

### 6.2 编辑配置文件

```bash
nano .env
```

### 6.3 必填配置项

```bash
# ===== 数据库密码（必须修改）=====
DB_PASSWORD=请替换为强密码

# ===== JWT 密钥（必须修改）=====
# 使用 openssl rand -hex 32 生成
JWT_SECRET_KEY=请替换为随机字符串

# ===== LLM 配置（必填）=====
# 推荐：智谱 AI
LLM_PROVIDER=zhipu
LLM_API_KEY=你的智谱API密钥
LLM_MODEL=glm-4-flash

# 或者使用通义千问
# LLM_PROVIDER=qwen
# LLM_API_KEY=你的通义API密钥
# LLM_MODEL=qwen-turbo
```

### 6.4 生成安全密码

```bash
# 生成数据库密码
openssl rand -base64 24

# 生成 JWT 密钥
openssl rand -hex 32
```

### 6.5 获取智谱 AI API Key

1. 访问 https://open.bigmodel.cn
2. 注册账号（手机号）
3. 完成实名认证
4. 点击「API 密钥」创建
5. 复制密钥到 `.env` 文件

---

## 七、启动服务

### 7.1 使用标准配置启动

```bash
cd /opt/esragpro

# 构建并启动
docker compose up -d --build

# 查看日志
docker compose logs -f
```

### 7.2 使用安全配置启动（推荐）

```bash
cd /opt/esragpro

# 使用安全版配置
docker compose -f docker-compose.secure.yml up -d --build

# 查看日志
docker compose logs -f
```

### 7.3 等待服务就绪

```bash
# 查看服务状态
docker compose ps

# 等待 Elasticsearch 启动（约 1-2 分钟）
# 看到 esragpro-es 状态为 "healthy" 即可
```

### 7.4 验证服务

```bash
# 检查后端健康
curl http://localhost:8000/health

# 检查 Elasticsearch
curl http://localhost:9200/_cluster/health

# 检查前端
curl http://localhost:80
```

---

## 八、配置 HTTPS

### 8.1 使用免费证书（Let's Encrypt）

**前提：需要域名并已解析到服务器 IP**

```bash
cd /opt/esragpro

# 运行 HTTPS 配置脚本
chmod +x setup-https.sh
./setup-https.sh 你的域名 你的邮箱

# 示例
./setup-https.sh kb.example.com admin@example.com
```

### 8.2 手动配置

```bash
# 安装 Certbot
apt install certbot -y

# 申请证书
certbot certonly --webroot -w /var/www/certbot -d 你的域名 --email 你的邮箱 --agree-tos

# 复制证书
cp /etc/letsencrypt/live/你的域名/fullchain.pem /opt/esragpro/certs/
cp /etc/letsencrypt/live/你的域名/privkey.pem /opt/esragpro/certs/

# 设置自动续期
echo "0 3 * * * certbot renew --quiet && cp /etc/letsencrypt/live/你的域名/*.pem /opt/esragpro/certs/ && docker restart esragpro-frontend" | crontab -

# 使用安全配置重启
docker compose -f docker-compose.secure.yml up -d --build
```

### 8.3 使用自签名证书（仅测试）

```bash
cd /opt/esragpro/certs

# 生成自签名证书
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
    -keyout privkey.pem \
    -out fullchain.pem \
    -subj "/C=CN/ST=Shanghai/L=Shanghai/O=Test/CN=localhost"
```

---

## 九、安全加固

### 9.1 运行安全脚本

```bash
cd /opt/esragpro

# 赋予执行权限
chmod +x setup-security.sh

# 运行安全配置
./setup-security.sh
```

脚本会自动配置：
- 强密码生成
- Fail2ban 防暴力破解
- 防火墙规则
- IP 白名单（可选）
- 管理后台基础认证
- 系统安全加固

### 9.2 配置 IP 白名单（强烈推荐）

```bash
# 编辑白名单文件
nano /opt/esragpro/nginx-whitelist.conf

# 添加允许的 IP
allow 1.2.3.4;        # 公司公网 IP
allow 10.0.0.0/8;     # 内网
deny all;             # 拒绝其他所有

# 在 nginx.conf 中启用
nano /opt/esragpro/frontend/nginx.conf
# 在 server 块中添加: include /opt/esragpro/nginx-whitelist.conf;

# 重启前端服务
docker compose restart frontend
```

### 9.3 安全检查清单

```bash
# 检查开放端口（应该只有 22, 80, 443）
ss -tlnp

# 检查防火墙状态
ufw status

# 检查 Fail2ban
fail2ban-client status

# 查看安全配置摘要
cat /opt/esragpro/SECURITY.md
```

---

## 十、验证部署

### 10.1 访问测试

```
浏览器访问: http://你的服务器IP
或: https://你的域名
```

### 10.2 功能测试

1. **首页** - 显示系统状态
2. **文档管理** - 上传测试文档（PDF/DOCX/TXT）
3. **知识检索** - 输入问题测试搜索

### 10.3 API 测试

```bash
# 健康检查
curl http://你的服务器IP/api/v1/kb/health

# 查看文档列表
curl http://你的服务器IP/api/v1/kb/documents

# API 文档
# 浏览器访问: http://你的服务器IP:8000/docs
```

### 10.4 查看日志

```bash
# 查看所有日志
docker compose logs -f

# 只看后端日志
docker compose logs -f backend

# 只看前端日志
docker compose logs -f frontend
```

---

## 十一、常见问题

### Q1: Elasticsearch 启动失败

**症状：** esragpro-es 容器不断重启

**原因：** 内存不足

**解决：**
```bash
# 调整 ES 内存限制
# 编辑 docker-compose.yml，修改 ES_JAVA_OPTS
environment:
  - "ES_JAVA_OPTS=-Xms512m -Xmx512m"

# 或增加服务器内存
```

### Q2: 前端无法访问

**症状：** 浏览器访问显示 502

**排查：**
```bash
# 检查服务状态
docker compose ps

# 检查后端是否运行
docker compose logs backend

# 检查 Nginx 配置
docker compose logs frontend
```

### Q3: 上传文档失败

**症状：** 上传时报错

**排查：**
```bash
# 检查 uploads 目录权限
ls -la /opt/esragpro/uploads

# 修复权限
chmod 777 /opt/esragpro/uploads
```

### Q4: LLM 调用失败

**症状：** 搜索无回答或报错

**排查：**
```bash
# 检查 API Key 是否正确
cat /opt/esragpro/.env | grep LLM

# 测试智谱 API
curl https://open.bigmodel.cn/api/paas/v4/chat/completions \
  -H "Authorization: Bearer 你的API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "glm-4-flash", "messages": [{"role": "user", "content": "你好"}]}'
```

### Q5: 如何更新部署

```bash
cd /opt/esragpro

# 拉取最新代码
git pull

# 重新构建并启动
docker compose up -d --build

# 清理旧镜像
docker image prune -f
```

### Q6: 如何备份数据

```bash
# 备份 Elasticsearch
docker exec esragpro-es elasticsearch-backup ...

# 备份 PostgreSQL
docker exec esragpro-postgres pg_dump -U postgres esragpro > backup.sql

# 备份上传文件
tar -czf uploads-backup.tar.gz /opt/esragpro/uploads
```

### Q7: 如何查看资源使用

```bash
# 查看 Docker 资源使用
docker stats

# 查看磁盘使用
df -h

# 查看内存使用
free -h
```

### Q8: 如何重置密码

```bash
# 重新生成密码
NEW_PASSWORD=$(openssl rand -base64 24)

# 更新 .env
sed -i "s/^DB_PASSWORD=.*/DB_PASSWORD=$NEW_PASSWORD/" /opt/esragpro/.env

# 重启服务
docker compose restart
```

---

## 十二、常用命令速查

```bash
# ===== 服务管理 =====
docker compose up -d          # 启动服务
docker compose down           # 停止服务
docker compose restart        # 重启服务
docker compose ps             # 查看状态
docker compose logs -f        # 查看日志

# ===== 安全管理 =====
ufw status                    # 防火墙状态
fail2ban-client status        # 封禁状态
fail2ban-client set esragpro-api unbanip IP  # 解封IP

# ===== 数据管理 =====
docker exec -it esragpro-backend bash  # 进入后端容器
docker exec -it esragpro-es bash       # 进入ES容器

# ===== 系统管理 =====
htop                          # 查看系统资源
df -h                         # 查看磁盘
free -h                       # 查看内存
```

---

## 十三、部署完成检查清单

- [ ] Docker 已安装并运行
- [ ] 代码已上传到 /opt/esragpro
- [ ] .env 配置已完成
- [ ] 服务已启动（docker compose ps 显示所有服务 running）
- [ ] 健康检查通过（curl localhost:8000/health）
- [ ] 网页可访问（http://服务器IP）
- [ ] HTTPS 已配置（可选）
- [ ] Fail2ban 已启用（可选）
- [ ] IP 白名单已配置（可选）
- [ ] 备份策略已制定

---

## 十四、获取帮助

- **项目文档**：`/opt/esragpro/docs/`
- **安全指南**：`/opt/esragpro/docs/security-guide.md`
- **LLM 配置**：`/opt/esragpro/docs/llm-options.md`
- **API 文档**：`http://服务器IP:8000/docs`

---

**祝你部署顺利！** 🎉