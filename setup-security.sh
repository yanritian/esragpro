#!/bin/bash
# esRAGPro 安全配置脚本
# 在服务器上执行此脚本以增强安全性

set -e

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

PROJECT_DIR="/opt/esragpro"

set_env_value() {
    local key="$1"
    local value="$2"
    local env_file="$PROJECT_DIR/.env"

    if grep -q "^${key}=" "$env_file"; then
        sed -i "s|^${key}=.*|${key}=${value}|" "$env_file"
    else
        printf '%s=%s\n' "$key" "$value" >> "$env_file"
    fi
}

echo "=========================================="
echo "    esRAGPro 安全配置"
echo "=========================================="
echo ""

# 1. 生成强密码
log_info "生成安全密码..."
generate_password() {
    openssl rand -base64 24 | tr -d '/+=' | head -c 32
}

DB_PASSWORD=$(generate_password)
ES_PASSWORD=$(generate_password)
REDIS_PASSWORD=$(generate_password)
NEO4J_PASSWORD=$(generate_password)
MINIO_ACCESS_KEY=$(generate_password)
MINIO_SECRET_KEY=$(generate_password)
JWT_SECRET=$(openssl rand -hex 32)

log_info "已生成强密码，请妥善保存："
echo ""
echo "  DB_PASSWORD=$DB_PASSWORD"
echo "  ES_PASSWORD=$ES_PASSWORD"
echo "  REDIS_PASSWORD=$REDIS_PASSWORD"
echo "  NEO4J_PASSWORD=$NEO4J_PASSWORD"
echo "  MINIO_ACCESS_KEY=$MINIO_ACCESS_KEY"
echo "  MINIO_SECRET_KEY=$MINIO_SECRET_KEY"
echo "  JWT_SECRET_KEY=$JWT_SECRET"
echo ""

# 2. 更新 .env 文件
mkdir -p "$PROJECT_DIR"
if [ -f "$PROJECT_DIR/.env" ]; then
    log_info "更新 .env 配置..."

    # 备份原文件
    cp "$PROJECT_DIR/.env" "$PROJECT_DIR/.env.bak.$(date +%Y%m%d%H%M%S)"
else
    touch "$PROJECT_DIR/.env"
fi
chmod 600 "$PROJECT_DIR/.env"
set_env_value "DB_PASSWORD" "$DB_PASSWORD"
set_env_value "ES_PASSWORD" "$ES_PASSWORD"
set_env_value "REDIS_PASSWORD" "$REDIS_PASSWORD"
set_env_value "NEO4J_PASSWORD" "$NEO4J_PASSWORD"
set_env_value "MINIO_ACCESS_KEY" "$MINIO_ACCESS_KEY"
set_env_value "MINIO_SECRET_KEY" "$MINIO_SECRET_KEY"
set_env_value "JWT_SECRET_KEY" "$JWT_SECRET"
log_info ".env 已更新"

# 3. 配置 Fail2ban（防暴力破解）
log_info "配置 Fail2ban..."

if command -v fail2ban-client &> /dev/null; then
    log_info "Fail2ban 已安装"
else
    log_info "安装 Fail2ban..."
    apt install -y fail2ban 2>/dev/null || yum install -y fail2ban

    # 创建 esRAGPro 专用配置
    cat > /etc/fail2ban/jail.d/esragpro.conf <<'EOF'
[esragpro-api]
enabled = true
port = http,https
filter = esragpro
logpath = /var/log/nginx/access.log
maxretry = 10
findtime = 60
bantime = 3600
action = iptables-multiport[name=esragpro, port="http,https", protocol=tcp]
EOF

    # 创建过滤规则
    cat > /etc/fail2ban/filter.d/esragpro.conf <<'EOF'
[Definition]
failregex = ^<HOST> -.*"(GET|POST|PUT|DELETE).*" (401|403|429).*$
ignoreregex =
EOF

    systemctl enable fail2ban
    systemctl restart fail2ban
    log_info "Fail2ban 配置完成"
fi

# 4. 配置 IP 白名单
echo ""
log_warn "=========================================="
log_warn "  IP 白名单配置"
log_warn "=========================================="
echo ""
echo "是否配置 IP 白名单？（只允许指定 IP 访问）"
read -p "输入 y 启用，其他跳过: " enable_whitelist

if [ "$enable_whitelist" = "y" ]; then
    echo ""
    echo "请输入允许访问的 IP 地址（多个用空格分隔）："
    echo "示例: 1.2.3.4 10.0.0.0/8 192.168.1.0/24"
    read -p "IP 列表: " allowed_ips

    # 创建 IP 白名单配置片段
    WHITELIST_CONF="$PROJECT_DIR/nginx-whitelist.conf"
    echo "# IP 白名单 - 由安全脚本生成" > "$WHITELIST_CONF"
    echo "# 在 nginx.conf 的 server 块中 include 此文件" >> "$WHITELIST_CONF"

    for ip in $allowed_ips; do
        echo "allow $ip;" >> "$WHITELIST_CONF"
    done
    echo "deny all;" >> "$WHITELIST_CONF"

    log_info "IP 白名单已生成: $WHITELIST_CONF"
    log_warn "请在 nginx.conf 中添加: include $WHITELIST_CONF;"
fi

# 5. 创建管理后台基础认证
echo ""
log_info "配置管理后台基础认证..."
read -p "管理员用户名 [admin]: " admin_user
admin_user=${admin_user:-admin}

read -s -p "管理员密码: " admin_pass
echo ""

# 生成 htpasswd 文件
HTPASSWD_FILE="$PROJECT_DIR/.htpasswd"
if command -v htpasswd &> /dev/null; then
    htpasswd -bc "$HTPASSWD_FILE" "$admin_user" "$admin_pass"
else
    # 手动生成 htpasswd
    HASH=$(openssl passwd -apr1 "$admin_pass")
    echo "$admin_user:$HASH" > "$HTPASSWD_FILE"
fi
chmod 644 "$HTPASSWD_FILE"

log_info "管理后台认证已配置"
log_warn "用户名: $admin_user"

# 6. 生成自签名 HTTPS 证书（用于测试）
CERTS_DIR="$PROJECT_DIR/certs"
CERTBOT_WEBROOT="$PROJECT_DIR/certbot_www"
mkdir -p "$CERTS_DIR" "$CERTBOT_WEBROOT"
if [ ! -s "$CERTS_DIR/fullchain.pem" ] || [ ! -s "$CERTS_DIR/privkey.pem" ]; then
    log_info "生成自签名 HTTPS 证书..."

    openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
        -keyout "$CERTS_DIR/privkey.pem" \
        -out "$CERTS_DIR/fullchain.pem" \
        -subj "/C=CN/ST=Shanghai/L=Shanghai/O=esRAGPro/OU=IT/CN=localhost"

    log_warn "已生成自签名证书（仅用于测试）"
    log_warn "生产环境请使用 Let's Encrypt 或购买证书"
fi

# 7. 配置防火墙
log_info "配置防火墙..."
if command -v ufw &> /dev/null; then
    ufw --force reset
    ufw default deny incoming
    ufw default allow outgoing
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

# 8. 系统安全加固
log_info "系统安全加固..."

# 禁用 root SSH 登录（可选）
# sed -i 's/#PermitRootLogin yes/PermitRootLogin no/' /etc/ssh/sshd_config
# systemctl restart sshd

# 内核安全参数
cat >> /etc/sysctl.conf <<EOF

# esRAGPro 安全加固
net.ipv4.tcp_syncookies = 1
net.ipv4.conf.all.rp_filter = 1
net.ipv4.conf.all.accept_source_route = 0
net.ipv4.conf.all.accept_redirects = 0
net.ipv4.conf.all.send_redirects = 0
net.ipv4.icmp_echo_ignore_broadcasts = 1
EOF
sysctl -p 2>/dev/null

# 9. 保存安全配置摘要
cat > "$PROJECT_DIR/SECURITY.md" <<EOF
# esRAGPro 安全配置记录

配置时间: $(date)

## 已启用安全措施

- ✅ 强密码策略
- ✅ Fail2ban 防暴力破解
- ✅ 防火墙 (仅开放 22/80/443)
- ✅ HTTPS 加密传输
- ✅ 管理后台基础认证
- ✅ API 速率限制
- ✅ 系统内核加固

## 敏感信息（请妥善保管）

- 数据库密码: $DB_PASSWORD
- ES 密码: $ES_PASSWORD
- Redis 密码: $REDIS_PASSWORD
- JWT 密钥: 见 .env 文件

## 后续建议

1. 申请正式 HTTPS 证书 (Let's Encrypt)
2. 配置定期备份
3. 启用日志审计
4. 定期更新系统和依赖

## 管理命令

# 查看被封禁的 IP
fail2ban-client status esragpro-api

# 解封 IP
fail2ban-client set esragpro-api unbanip <IP>

# 查看防火墙状态
ufw status

# 查看访问日志
tail -f /var/log/nginx/access.log
EOF

echo ""
log_info "=========================================="
log_info "  安全配置完成！"
log_info "=========================================="
echo ""
echo "安全配置摘要已保存到: $PROJECT_DIR/SECURITY.md"
echo ""
log_warn "重要提醒："
echo "  1. 请记录上方生成的密码"
echo "  2. 生产环境请申请正式 HTTPS 证书"
echo "  3. 重启服务使配置生效: docker compose -f docker-compose.secure.yml up -d"
