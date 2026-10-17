# syntax=docker/dockerfile:1.7
# The SPA is static: build it once on the build machine's own platform, also for arm64 images.
FROM --platform=$BUILDPLATFORM node:22-alpine AS build
WORKDIR /src
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.28-alpine
# openssl creates the self-signed certificate at start-up, curl runs the health check over HTTPS;
# /etc/nginx/certs belongs to nginx so a fresh volume there is writable (Docker copies the owner
# into new named volumes).
USER root
RUN apk add --no-cache openssl curl \
    && mkdir -p /etc/nginx/certs \
    && chown 101:101 /etc/nginx/certs && chmod 0750 /etc/nginx/certs
USER 101
# Templates in /etc/nginx/templates are rendered with envsubst at start-up.
COPY nginx/default.conf.template /etc/nginx/templates/default.conf.template
COPY nginx/security-headers.inc /etc/nginx/templates/security-headers.inc
COPY --chmod=0755 nginx/15-tls.sh /docker-entrypoint.d/15-tls.sh
COPY --from=build /src/dist /usr/share/nginx/html
ENV API_UPSTREAM=api:8000 \
    CLIENT_MAX_BODY_SIZE=0 \
    NGINX_ENTRYPOINT_LOCAL_RESOLVERS=1 \
    NGINX_ENVSUBST_FILTER="^(API_UPSTREAM|CLIENT_MAX_BODY_SIZE|SERVER_NAME|NGINX_LOCAL_RESOLVERS)$"
VOLUME ["/etc/nginx/certs"]
EXPOSE 8443
HEALTHCHECK --interval=15s --timeout=3s --retries=5 CMD curl -fsk -o /dev/null https://127.0.0.1:8443/web-health || exit 1
