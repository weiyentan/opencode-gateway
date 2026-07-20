#!/bin/sh
# ── Aurora Glass Docker Entrypoint ─────────────────────────────────────────
# Substitutes the GATEWAY_API_URL environment variable into the nginx
# configuration template, then starts nginx.
#
# GATEWAY_API_URL defaults to http://gateway:8000 (the docker-compose
# service name) when not set. Users running the container standalone
# should set this to their Gateway instance URL, e.g.:
#   docker run -e GATEWAY_API_URL=http://host.docker.internal:8000 ...
#
# Only ${GATEWAY_API_URL} is substituted — nginx variables like $host,
# $remote_addr, $proxy_add_x_forwarded_for, $scheme, and $uri are
# preserved as-is.
# ────────────────────────────────────────────────────────────────────────────

set -e

# Default to the docker-compose service URL if not provided.
: "${GATEWAY_API_URL:=http://gateway:8000}"
export GATEWAY_API_URL

echo "Aurora Glass: configuring proxy target → ${GATEWAY_API_URL}"

# Substitute only ${GATEWAY_API_URL} in the nginx config.
# Using envsubst with an explicit list of variables ensures nginx's own
# $variables (host, remote_addr, etc.) are left untouched.
envsubst '${GATEWAY_API_URL}' \
  < /etc/nginx/conf.d/default.conf \
  > /tmp/default.conf

# Atomically replace the config file.
mv /tmp/default.conf /etc/nginx/conf.d/default.conf

echo "Aurora Glass: starting nginx…"

exec nginx -g "daemon off;"
