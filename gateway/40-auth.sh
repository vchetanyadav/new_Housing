#!/bin/sh
# Optional login for the whole site: set BASIC_AUTH_USER and BASIC_AUTH_PASSWORD in .env
set -e
if [ -n "$BASIC_AUTH_USER" ] && [ -n "$BASIC_AUTH_PASSWORD" ]; then
  printf '%s:%s\n' "$BASIC_AUTH_USER" "$(openssl passwd -apr1 "$BASIC_AUTH_PASSWORD")" > /etc/nginx/.htpasswd
  printf 'auth_basic "Housing Opportunity Finder";\nauth_basic_user_file /etc/nginx/.htpasswd;\n' > /etc/nginx/snippets/auth.conf
  echo "gateway: login enabled for user $BASIC_AUTH_USER"
else
  : > /etc/nginx/snippets/auth.conf
  echo "gateway: login disabled (set BASIC_AUTH_USER / BASIC_AUTH_PASSWORD to enable)"
fi
