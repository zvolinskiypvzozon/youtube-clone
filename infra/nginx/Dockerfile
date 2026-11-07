# syntax=docker/dockerfile:1
# Сборка SPA и упаковка вместе с nginx (непривилегированный, порт 8080).
FROM node:22.23.3-alpine AS build
WORKDIR /src
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.28.2-alpine
COPY infra/nginx/security-headers.conf /etc/nginx/snippets/security-headers.conf
COPY infra/nginx/storage-proxy.conf /etc/nginx/snippets/storage-proxy.conf
COPY infra/nginx/default.conf /etc/nginx/conf.d/default.conf
COPY --from=build /src/dist /usr/share/nginx/html
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
  CMD wget -qO- http://127.0.0.1:8080/nginx-health >/dev/null || exit 1
