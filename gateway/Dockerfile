FROM nginx:1.27-alpine
RUN apk add --no-cache openssl
COPY gateway/default.conf.template /etc/nginx/templates/default.conf.template
COPY gateway/snippets/ /etc/nginx/snippets/
COPY gateway/40-auth.sh /docker-entrypoint.d/40-auth.sh
RUN chmod +x /docker-entrypoint.d/40-auth.sh && touch /etc/nginx/snippets/auth.conf
COPY frontend/ /usr/share/nginx/html/
ENV API_UPSTREAM=api:8000
HEALTHCHECK --interval=30s --timeout=5s CMD wget -qO- http://127.0.0.1/api/health >/dev/null || exit 1
