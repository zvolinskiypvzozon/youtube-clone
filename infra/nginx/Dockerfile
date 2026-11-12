# syntax=docker/dockerfile:1
# Сборка SPA и упаковка вместе с nginx (непривилегированный, порт 8080).
FROM node:22.23.3-alpine@sha256:0a7108bf6c7bf5de370ffb1a3ed6be93d405b43ff159f681a8d18c0e2bc2e402 AS build
WORKDIR /src
# Телеметрия @scarf/scarf (зависимость swagger-ui-dist) отключена и здесь, и в package.json.
ENV SCARF_ANALYTICS=false
COPY frontend/package.json frontend/package-lock.json ./
# До 3 попыток: npm ci изредка падает сам по себе («Exit handler never called», сеть) — на
# чистой машине это обрывало бы весь деплой (день 9).
RUN for i in 1 2 3; do npm ci --no-audit --no-fund && exit 0; echo "npm ci: попытка $i не удалась" >&2; sleep 5; done; exit 1
COPY frontend/ ./
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.28.2-alpine@sha256:7377697a821c131a924a7105fafbe7414db4e9fcc77a6f08f776f33f141ec3f8
COPY infra/nginx/security-headers.conf /etc/nginx/snippets/security-headers.conf
COPY infra/nginx/storage-proxy.conf /etc/nginx/snippets/storage-proxy.conf
COPY infra/nginx/default.conf /etc/nginx/conf.d/default.conf
COPY --from=build /src/dist /usr/share/nginx/html
# Swagger UI своими файлами (без CDN): статика из swagger-ui-dist и своя страница /docs.
COPY --from=build /src/node_modules/swagger-ui-dist/swagger-ui-bundle.js \
     /src/node_modules/swagger-ui-dist/swagger-ui.css \
     /src/node_modules/swagger-ui-dist/favicon-32x32.png /usr/share/nginx/html/swagger-ui/
COPY infra/nginx/swagger/ /usr/share/nginx/html/swagger-ui/
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
  CMD wget -qO- http://127.0.0.1:8080/nginx-health >/dev/null || exit 1
