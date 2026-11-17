# esRAGPro 云服务器部署指南

## 一、服务器配置建议

### 最低配置（测试/演示）

| 配置项 | 建议 |
|--------|------|
| CPU | 2 核 |
| 内存 | 8 GB（ES 占用较大） |
| 硬盘 | 50 GB SSD |
| 带宽 | 3 Mbps |
| 系统 | Ubuntu 22.04 / CentOS 8 |

**推荐云厂商：**
- 阿里云：ECS ecs.c6.large (2核4G) ~100元/月
- 腾讯云：CVM 标准型S5 ~90元/月
- 华为云：ECS 通用计算型 ~85元/月

### 生产配置（正式使用）

| 配置项 | 建议 |
|--------|------|
| CPU | 4 核+ |
| 内存 | 16 GB+ |
| 硬盘 | 200 GB SSD |
| 带宽 | 10 Mbps+ |

---

## 二、部署步骤

### 第一步：购买服务器

1. 选择云厂商，购买服务器
2. 选择 Ubuntu 22.04 系统
3. 开放安全组端口：
   - 22 (SSH)
   - 80 (HTTP)
   - 443 (HTTPS)
   - 9200 (ES，可选，内网访问)

### 第二步：连接服务器

```bash
# Windows 使用 PowerShell 或 Git Bash
ssh root@你的服务器IP
```

### 第三步：安装 Docker

```bash
# 更新系统
apt update && apt upgrade -y

# 安装 Docker
curl -fsSL https://get.docker.com | bash

# 安装 Docker Compose
apt install docker-compose-plugin -y

# 启动 Docker
systemctl start docker
systemctl enable docker

# 验证安装
docker --version
docker compose version
```

### 第四步：上传代码

**方式一：Git 克隆（推荐）**

```bash
# 安装 Git
apt install git -y

# 克隆项目
git clone https://github.com/你的用户名/esRAGPro.git
cd esRAGPro
```

**方式二：SCP 上传**

```bash
# 在本地 Windows 执行
scp -r d:\dingsun\esRAGPro root@服务器IP:/root/
```

### 第五步：配置环境变量

```bash
# 复制配置文件
cp .env.example .env

# 编辑配置
nano .env
```

**关键配置：**

```bash
# Elasticsearch
ES_HOST=http://elasticsearch:9200

# LLM 配置（必填）
LLM_PROVIDER=openai
LLM_API_KEY=sk-xxxxx你的密钥
LLM_MODEL=gpt-4o-mini

# JWT 密钥（必须修改）
JWT_SECRET_KEY=生成一个随机字符串

# 安全配置
DEBUG=false
```

### 第六步：启动服务

```bash
# 构建并启动
docker compose up -d --build

# 查看日志
docker compose logs -f

# 检查状态
docker compose ps
```

### 第七步：验证部署

```bash
# 检查 ES
curl http://localhost:9200/_cluster/health

# 检查后端
curl http://localhost:8000/health

# 访问前端
# 浏览器打开 http://服务器IP
```

---

## 三、域名与 HTTPS（可选）

### 配置域名

```bash
# 安装 Nginx（如果用外部 Nginx）
apt install nginx -y

# 配置域名解析到服务器 IP
```

### 配置 HTTPS（Let's Encrypt）

```bash
# 安装 Certbot
apt install certbot python3-certbot-nginx -y

# 申请证书
certbot --nginx -d your-domain.com

# 自动续期
certbot renew --dry-run
```

---

## 四、常用运维命令

```bash
# 查看服务状态
docker compose ps

# 查看日志
docker compose logs -f backend
docker compose logs -f frontend

# 重启服务
docker compose restart

# 停止服务
docker compose down

# 更新部署
git pull
docker compose up -d --build

# 进入容器调试
docker exec -it esragpro-backend bash

# 备份数据
docker run --rm -v esragpro_es_data:/data -v $(pwd)/backup:/backup alpine tar czf /backup/es_backup.tar.gz /data
```

---

## 五、成本估算

### 测试环境（月费用）

| 项目 | 配置 | 费用 |
|------|------|------|
| 云服务器 | 2核8G | ~100 元 |
| 带宽 | 3 Mbps | ~30 元 |
| **合计** | | **~130 元/月** |

### 生产环境（月费用）

| 项目 | 配置 | 费用 |
|------|------|------|
| 云服务器 | 4核16G | ~300 元 |
| 带宽 | 10 Mbps | ~100 元 |
| LLM API | GPT-4o-mini | 按量计费 |
| **合计** | | **~400+ 元/月** |

---

## 六、安全检查清单

- [ ] 修改所有默认密码
- [ ] JWT_SECRET_KEY 使用强随机字符串
- [ ] ES 端口 9200 不对公网开放
- [ ] 配置防火墙规则
- [ ] 定期备份数据
- [ ] 启用 HTTPS

---

## 七、故障排查

### ES 启动失败

```bash
# 检查内存
free -h

# ES 需要至少 4GB 可用内存
# 调整 ES 内存限制
# docker-compose.yml 中添加:
# environment:
#   - "ES_JAVA_OPTS=-Xms512m -Xmx512m"
```

### 后端启动失败

```bash
# 查看详细日志
docker compose logs backend

# 常见问题：
# 1. ES 未就绪 - 等待 ES 健康检查通过
# 2. 模型下载慢 - 配置 HuggingFace 镜像
```

### 前端无法访问

```bash
# 检查 Nginx 配置
docker compose logs frontend

# 确保端口开放
curl http://localhost:80
```

---

## 八、快速部署脚本

将以下内容保存为 `deploy-cloud.sh`，上传到服务器执行：

```bash
#!/bin/bash
set -e

echo "=== esRAGPro 云部署脚本 ==="

# 1. 安装 Docker
if ! command -v docker &> /dev/null; then
    echo "安装 Docker..."
    curl -fsSL https://get.docker.com | bash
    apt install docker-compose-plugin -y
    systemctl start docker
    systemctl enable docker
fi

# 2. 配置环境
if [ ! -f .env ]; then
    echo "请先创建 .env 文件！"
    exit 1
fi

# 3. 启动服务
echo "启动服务..."
docker compose up -d --build

# 4. 等待 ES 就绪
echo "等待 Elasticsearch 启动..."
sleep 30
until curl -s http://localhost:9200/_cluster/health | grep -q "green\|yellow"; do
    echo "等待中..."
    sleep 5
done

echo "=== 部署完成 ==="
echo "前端: http://$(curl -s ifconfig.me)"
echo "后端: http://$(curl -s ifconfig.me):8000"
```