#!/bin/bash
# HTTPS 证书配置脚本（Let's Encrypt）

set -e

DOMAIN=${1:-}
EMAIL=${2:-}

if [ -z "$DOMAIN" ] || [ -z "$EMAIL" ]; then
    echo "用法: bash setup-https.sh 你的域名 你的邮箱"
    echo "示例: bash setup-https.sh example.com admin@example.com"
    exit 1
fi

echo "=========================================="
echo "  配置 HTTPS 证书"
echo "  域名: $DOMAIN"
echo "  邮箱: $EMAIL"
echo "=========================================="

# 安装 Certbot
if ! command -v certbot &> /dev/null; then
    echo "安装 Certbot..."
    apt update && apt install -y certbot 2>/dev/null || yum install -y certbot
fi

# 创建证书目录
mkdir -p /opt/esragpro/certs
mkdir -p /var/www/certbot

# 临时启动一个简单的 HTTP 服务器用于验证
echo "申请证书..."
certbot certonly --webroot \
    -w /var/www/certbot \
    -d "$DOMAIN" \
    --email "$EMAIL" \
    --agree-tos \
    --no-eff-email

# 复制证书到项目目录
cp /etc/letsencrypt/live/$DOMAIN/fullchain.pem /opt/esragpro/certs/
cp /etc/letsencrypt/live/$DOMAIN/privkey.pem /opt/esragpro/certs/

# 设置自动续期
echo "配置自动续期..."
(crontab -l 2>/dev/null; echo "0 3 * * * certbot renew --quiet && cp /etc/letsencrypt/live/$DOMAIN/*.pem /opt/esragpro/certs/ && docker restart esragpro-frontend") | crontab -

echo ""
echo "✅ HTTPS 证书配置完成！"
echo ""
echo "证书位置: /opt/esragpro/certs/"
echo "自动续期已配置"
echo ""
echo "请重启服务:"
echo "  docker compose -f docker-compose.secure.yml restart frontend"