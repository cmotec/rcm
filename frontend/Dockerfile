# syntax=docker/dockerfile:1.7
FROM node:22-alpine AS build
WORKDIR /src
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.28-alpine
# Templates in /etc/nginx/templates are rendered with envsubst at start-up.
COPY nginx/default.conf.template /etc/nginx/templates/default.conf.template
COPY nginx/security-headers.inc /etc/nginx/templates/security-headers.inc
COPY --from=build /src/dist /usr/share/nginx/html
ENV API_UPSTREAM=api:8000 \
    CLIENT_MAX_BODY_SIZE=0 \
    NGINX_ENVSUBST_FILTER="^(API_UPSTREAM|CLIENT_MAX_BODY_SIZE|SERVER_NAME)$"
EXPOSE 8080 8443
HEALTHCHECK --interval=15s --timeout=3s --retries=5 CMD wget -q -O /dev/null http://127.0.0.1:8080/web-health || exit 1
