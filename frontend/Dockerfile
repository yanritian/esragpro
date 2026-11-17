# Frontend Dockerfile - Multi-stage build
FROM node:20-alpine as builder

WORKDIR /app

# Copy package files
COPY package*.json ./

# Install dependencies
RUN npm install --registry=https://registry.npmmirror.com

# Copy source code
COPY . .

# Build application
RUN npm run build

# Production stage with nginx
FROM nginx:alpine

ARG NGINX_CONFIG=nginx.conf

# Copy custom nginx config
COPY ${NGINX_CONFIG} /etc/nginx/conf.d/default.conf

# Copy built assets from builder
COPY --from=builder /app/dist /usr/share/nginx/html

# Copy error pages (from public directory, copied to dist by Vite)
# These are already included in dist if they exist in public/
# But we ensure they exist here:
RUN if [ ! -f /usr/share/nginx/html/429.html ]; then \
        echo '<!DOCTYPE html><html><head><title>429</title></head><body><h1>请求过于频繁</h1><p>请稍后重试</p></body></html>' > /usr/share/nginx/html/429.html; \
    fi && \
    if [ ! -f /usr/share/nginx/html/50x.html ]; then \
        echo '<!DOCTYPE html><html><head><title>Error</title></head><body><h1>服务暂时不可用</h1><p>请稍后重试</p></body></html>' > /usr/share/nginx/html/50x.html; \
    fi

# Expose port
EXPOSE 80 443

# Start nginx
CMD ["nginx", "-g", "daemon off;"]
