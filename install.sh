#!/bin/bash
# ============================================================
# esRAGPro 一键安装脚本
# 适用于 Ubuntu 22.04 / CentOS 8
# 使用方法: bash install.sh
# ============================================================

set -e

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# 日志函数
log() { echo -e "${GREEN}[INFO]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }
step() { echo -e "\n${BLUE}=====> $1${NC}\n"; }

set_env_value() {
    local key="$1"
    local value="$2"

    if grep -q "^${key}=" ".env"; then
        sed -i "s|^${key}=.*|${key}=${value}|" ".env"
    else
        printf '%s=%s\n' "$key" "$value" >> ".env"
    fi
}

# 检查 root
if [ "$EUID" -ne 0 ]; then
    error "请使用 root 用户或 sudo 执行此脚本"
fi

clear
echo "============================================================"
echo "           esRAGPro 一键安装脚本"
echo "============================================================"
echo ""

# ============================================
# 第一步：安装 Docker
# ============================================
step "1/6 安装 Docker"

if command -v docker &> /dev/null; then
    log "Docker 已安装: $(docker --version)"
else
    log "正在安装 Docker..."
    apt update && apt upgrade -y 2>/dev/null || yum update -y
    curl -fsSL https://get.docker.com | bash

    # 配置镜像加速
    mkdir -p /etc/docker
    cat > /etc/docker/daemon.json <<EOF
{
    "registry-mirrors": [
        "https://docker.mirrors.ustc.edu.cn",
        "https://hub-mirror.c.163.com"
    ]
}
EOF

    systemctl daemon-reload
    systemctl start docker
    systemctl enable docker
    log "Docker 安装完成"
fi

# 安装 Docker Compose
if ! docker compose version &> /dev/null; then
    apt install docker-compose-plugin -y 2>/dev/null || yum install docker-compose-plugin -y
fi

log "Docker Compose: $(docker compose version)"

# ============================================
# 第二步：创建项目目录
# ============================================
step "2/6 创建项目目录"

PROJECT_DIR="/opt/esragpro"
mkdir -p "$PROJECT_DIR"
cd "$PROJECT_DIR"
log "项目目录: $PROJECT_DIR"

# ============================================
# 第三步：下载或创建代码
# ============================================
step "3/6 准备代码"

if [ -f "docker-compose.yml" ]; then
    log "代码已存在"
else
    if command -v git &> /dev/null; then
        log "请输入 Git 仓库地址（留空跳过）:"
        read -p "Git URL: " git_url

        if [ -n "$git_url" ]; then
            git clone "$git_url" .
            log "代码克隆完成"
        else
            warn "请手动上传代码到 $PROJECT_DIR"
            warn "上传后重新运行此脚本"
            exit 0
        fi
    else
        warn "请手动上传代码到 $PROJECT_DIR"
        warn "上传后重新运行此脚本"
        apt install git -y
        exit 0
    fi
fi

# ============================================
# 第四步：配置环境变量
# ============================================
step "4/6 配置环境变量"

if [ ! -f ".env" ]; then
    log "创建 .env 配置文件..."

    # 生成安全密码
    DB_PASSWORD=$(openssl rand -base64 24 | tr -d '/+=' | head -c 24)
    ES_PASSWORD=$(openssl rand -base64 24 | tr -d '/+=' | head -c 24)
    REDIS_PASSWORD=$(openssl rand -base64 24 | tr -d '/+=' | head -c 24)
    NEO4J_PASSWORD=$(openssl rand -base64 24 | tr -d '/+=' | head -c 24)
    MINIO_ACCESS_KEY=$(openssl rand -base64 24 | tr -d '/+=' | head -c 24)
    MINIO_SECRET_KEY=$(openssl rand -base64 24 | tr -d '/+=' | head -c 24)
    JWT_SECRET=$(openssl rand -hex 32)

    cp .env.example .env 2>/dev/null || true

    # 如果没有 .env.example，创建一个
    if [ ! -f ".env" ]; then
        cat > .env <<EOF
# 数据库配置
DB_USER=esragpro
DB_PASSWORD=$DB_PASSWORD

# Elasticsearch
ES_PASSWORD=$ES_PASSWORD

# Redis
REDIS_PASSWORD=$REDIS_PASSWORD

# JWT 密钥
JWT_SECRET_KEY=$JWT_SECRET

# LLM 配置（必填）
LLM_PROVIDER=zhipu
LLM_API_KEY=请填写你的API密钥
LLM_MODEL=glm-4-flash
EOF
    fi

    set_env_value "DB_PASSWORD" "$DB_PASSWORD"
    set_env_value "ES_PASSWORD" "$ES_PASSWORD"
    set_env_value "REDIS_PASSWORD" "$REDIS_PASSWORD"
    set_env_value "NEO4J_PASSWORD" "$NEO4J_PASSWORD"
    set_env_value "MINIO_ACCESS_KEY" "$MINIO_ACCESS_KEY"
    set_env_value "MINIO_SECRET_KEY" "$MINIO_SECRET_KEY"
    set_env_value "JWT_SECRET_KEY" "$JWT_SECRET"
    chmod 600 .env

    log "密码已自动生成"
fi

echo ""
warn "============================================"
warn "  重要：请编辑 .env 文件配置 API 密钥"
warn "============================================"
echo ""
echo "  nano $PROJECT_DIR/.env"
echo ""
echo "  必填项："
echo "    LLM_API_KEY=你的智谱API密钥"
echo ""
read -p "配置完成后按回车继续..."

# ============================================
# 第五步：配置防火墙
# ============================================
step "5/6 配置防火墙"

if command -v ufw &> /dev/null; then
    ufw --force reset 2>/dev/null || true
    ufw default deny incoming
    ufw default allow outgoing
    ufw allow 22/tcp
    ufw allow 80/tcp
    ufw allow 443/tcp
    ufw --force enable
    log "UFW 防火墙已配置"
elif command -v firewall-cmd &> /dev/null; then
    firewall-cmd --permanent --add-port=22/tcp
    firewall-cmd --permanent --add-port=80/tcp
    firewall-cmd --permanent --add-port=443/tcp
    firewall-cmd --reload
    log "Firewalld 防火墙已配置"
fi

# 系统优化
cat >> /etc/sysctl.conf <<EOF

# esRAGPro 优化
vm.max_map_count=262144
EOF
sysctl -p 2>/dev/null

# ============================================
# 第六步：启动服务
# ============================================
step "6/6 启动服务"

COMPOSE_FILE="docker-compose.secure.yml"
PUBLIC_SCHEME="https"
PUBLIC_PORT="${HTTPS_PORT:-443}"
if [ ! -f "$COMPOSE_FILE" ]; then
    COMPOSE_FILE="docker-compose.yml"
    PUBLIC_SCHEME="http"
    PUBLIC_PORT="${HTTP_PORT:-80}"
fi

if [ "$COMPOSE_FILE" = "docker-compose.secure.yml" ]; then
    [ -s "certs/fullchain.pem" ] || error "缺少 certs/fullchain.pem，请先运行 ./setup-security.sh"
    [ -s "certs/privkey.pem" ] || error "缺少 certs/privkey.pem，请先运行 ./setup-security.sh"
    [ -s ".htpasswd" ] || error "缺少 .htpasswd，请先运行 ./setup-security.sh"
fi

if [ -z "${EMBEDDING_MODEL_SOURCE:-}" ] && [ -f ".env" ]; then
    EMBEDDING_MODEL_SOURCE=$(sed -n 's/^EMBEDDING_MODEL_SOURCE=//p' .env | tail -n 1)
fi

log "构建并启动服务（首次启动约需 5-10 分钟）..."
if [ "$COMPOSE_FILE" = "docker-compose.secure.yml" ]; then
    log "构建嵌入模型初始化镜像..."
    docker compose -f "$COMPOSE_FILE" build embedding-model-init

    log "校验已发布的嵌入模型卷..."
    if bash scripts/provision-embedding-model.sh verify --secure; then
        log "嵌入模型卷校验通过，复用已发布内容"
    elif [ -n "${EMBEDDING_MODEL_SOURCE:-}" ]; then
        log "检测到离线导入源（路径状态：已提供）"
        bash scripts/provision-embedding-model.sh import --secure --source "$EMBEDDING_MODEL_SOURCE"
    else
        log "未提供离线导入源，使用锁定清单获取嵌入模型"
        bash scripts/provision-embedding-model.sh acquire --secure
    fi
else
    warn "Using non-secure compose; embedding model provisioning is disabled."
fi

docker compose -f "$COMPOSE_FILE" up -d --build

wait_for_health() {
    local container="$1"
    local display_name="$2"

    log "等待 $display_name 健康..."
    for i in $(seq 1 60); do
        health_status=$(docker inspect --format '{{.State.Health.Status}}' "$container" 2>/dev/null || true)
        if [ "$health_status" = "healthy" ]; then
            log "$display_name 健康检查通过"
            return 0
        fi
        sleep 5
    done
    error "$display_name 未通过容器健康检查"
}

wait_for_health esragpro-es "Elasticsearch"
if [ "$COMPOSE_FILE" = "docker-compose.secure.yml" ]; then
    wait_for_health esragpro-postgres "PostgreSQL"
    wait_for_health esragpro-redis "Redis"
    wait_for_health esragpro-neo4j "Neo4j"
    wait_for_health esragpro-minio "MinIO"
    wait_for_health esragpro-backend "后端"
    wait_for_health esragpro-frontend "前端"
fi

echo ""
docker compose -f "$COMPOSE_FILE" ps
SERVER_IP=$(curl -s ifconfig.me 2>/dev/null || curl -s ip.sb 2>/dev/null || echo "服务器IP")

echo ""
echo "============================================================"
echo -e "${GREEN}           安装完成！${NC}"
echo "============================================================"
echo ""
echo "访问地址："
echo "  前端：${PUBLIC_SCHEME}://$SERVER_IP:$PUBLIC_PORT"
echo "  API 文档：${PUBLIC_SCHEME}://$SERVER_IP:$PUBLIC_PORT/docs"
echo ""
echo "常用命令："
echo "  查看日志：docker compose -f $COMPOSE_FILE logs -f"
echo "  重启服务：docker compose -f $COMPOSE_FILE restart"
echo "  停止服务：docker compose -f $COMPOSE_FILE down"
echo ""
echo "安全配置："
echo "  运行安全加固：./setup-security.sh"
echo "  配置 HTTPS：./setup-https.sh 你的域名 你的邮箱"
echo ""
echo "配置文件："
echo "  环境变量：$PROJECT_DIR/.env"
echo "  安全配置：$PROJECT_DIR/SECURITY.md"
echo ""
echo "============================================================"
