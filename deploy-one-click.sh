#!/bin/bash
# esRAGPro 一键部署脚本
# 适用于 Ubuntu 22.04 / CentOS 8
# 使用方法: bash deploy-one-click.sh

set -e

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

echo "=========================================="
echo "    esRAGPro 一键部署脚本"
echo "=========================================="
echo ""

# 检查是否为 root 用户
if [ "$EUID" -ne 0 ]; then
    log_error "请使用 root 用户或 sudo 执行此脚本"
    exit 1
fi

# 1. 系统更新
log_info "更新系统软件包..."
apt update && apt upgrade -y 2>/dev/null || yum update -y

# 2. 安装 Docker
if ! command -v docker &> /dev/null; then
    log_info "安装 Docker..."
    curl -fsSL https://get.docker.com | bash

    # 配置 Docker 镜像加速（国内）
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
    log_info "Docker 安装完成"
else
    log_info "Docker 已安装"
fi

# 3. 安装 Docker Compose
if ! docker compose version &> /dev/null; then
    log_info "安装 Docker Compose..."
    apt install docker-compose-plugin -y 2>/dev/null || yum install docker-compose-plugin -y
fi

# 4. 安装其他工具
log_info "安装必要工具..."
apt install -y git curl wget htop 2>/dev/null || yum install -y git curl wget htop

# 5. 创建项目目录
PROJECT_DIR="/opt/esragpro"
if [ ! -d "$PROJECT_DIR" ]; then
    log_info "创建项目目录..."
    mkdir -p "$PROJECT_DIR"
fi
cd "$PROJECT_DIR"

# 6. 下载代码（如果不存在）
if [ ! -f "docker-compose.yml" ]; then
    log_info "请上传项目代码到 $PROJECT_DIR 目录"
    log_warn "或使用 git clone: git clone https://github.com/你的仓库/esRAGPro.git ."
    echo ""
    read -p "是否已上传代码? (y/n): " code_uploaded
    if [ "$code_uploaded" != "y" ]; then
        log_error "请先上传代码后重新执行脚本"
        exit 1
    fi
fi

# 7. 配置环境变量
if [ ! -f ".env" ]; then
    log_info "创建环境变量配置..."
    cp .env.example .env

    # 生成随机 JWT 密钥
    JWT_SECRET=$(openssl rand -hex 32)
    sed -i "s/JWT_SECRET_KEY=.*/JWT_SECRET_KEY=$JWT_SECRET/" .env

    echo ""
    log_warn "=========================================="
    log_warn "  重要：请编辑 .env 文件配置以下内容"
    log_warn "=========================================="
    echo ""
    echo "nano $PROJECT_DIR/.env"
    echo ""
    echo "必填配置："
    echo "  LLM_PROVIDER=zhipu    # 智谱AI"
    echo "  LLM_API_KEY=your_key  # API密钥"
    echo "  LLM_MODEL=glm-4-flash # 模型名称"
    echo ""
    read -p "配置完成后按回车继续..."
fi

# 8. 配置防火墙
log_info "配置防火墙..."
if command -v ufw &> /dev/null; then
    ufw allow 22/tcp
    ufw allow 80/tcp
    ufw allow 443/tcp
    ufw --force enable
    log_info "UFW 防火墙已配置"
elif command -v firewall-cmd &> /dev/null; then
    firewall-cmd --permanent --add-port=22/tcp
    firewall-cmd --permanent --add-port=80/tcp
    firewall-cmd --permanent --add-port=443/tcp
    firewall-cmd --reload
    log_info "Firewalld 防火墙已配置"
fi

# 9. 优化系统参数（ES 需要）
log_info "优化系统参数..."
cat >> /etc/sysctl.conf <<EOF

# ES 优化
vm.max_map_count=262144
net.core.somaxconn=65535
EOF
sysctl -p

# 10. 启动服务
log_info "构建并启动服务..."
docker compose up -d --build

# 11. 等待 Elasticsearch 就绪
log_info "等待 Elasticsearch 启动（可能需要 1-2 分钟）..."
sleep 10
for i in {1..30}; do
    if curl -s http://localhost:9200/_cluster/health | grep -q "green\|yellow"; then
        log_info "Elasticsearch 启动成功!"
        break
    fi
    echo -n "."
    sleep 5
done
echo ""

# 12. 检查服务状态
log_info "检查服务状态..."
sleep 5
docker compose ps

# 13. 获取服务器 IP
SERVER_IP=$(curl -s ifconfig.me || curl -s ip.sb || echo "服务器IP")

# 14. 输出结果
echo ""
log_info "=========================================="
log_info "  部署完成!"
log_info "=========================================="
echo ""
echo "访问地址:"
echo "  前端:  http://$SERVER_IP"
echo "  后端:  http://$SERVER_IP:8000"
echo "  API文档: http://$SERVER_IP:8000/docs"
echo ""
echo "常用命令:"
echo "  查看日志:   docker compose logs -f"
echo "  重启服务:   docker compose restart"
echo "  停止服务:   docker compose down"
echo "  更新部署:   git pull && docker compose up -d --build"
echo ""
log_warn "安全提醒:"
echo "  1. 修改 .env 中的所有密钥"
echo "  2. ES 端口 9200 未对外开放"
echo "  3. 建议配置 HTTPS"
echo ""

# 15. 保存部署信息
cat > "$PROJECT_DIR/deploy-info.txt" <<EOF
部署时间: $(date)
服务器IP: $SERVER_IP
前端地址: http://$SERVER_IP
后端地址: http://$SERVER_IP:8000
JWT密钥: $JWT_SECRET
EOF

log_info "部署信息已保存到 $PROJECT_DIR/deploy-info.txt"