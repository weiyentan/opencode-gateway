#!/bin/sh
# ── Aurora Glass Container Entrypoint ─────────────────────────────────────
# Substitutes the GATEWAY_UPSTREAM environment variable into the nginx
# configuration at container start, then launches nginx.
#
# If GATEWAY_UPSTREAM is not set, defaults to http://gateway:8000
# (the docker-compose service name for local development).
# ──────────────────────────────────────────────────────────────────────────

set -e

# Substitute the Gateway upstream URL in the nginx config template.
# Only substitute GATEWAY_UPSTREAM to avoid mangling other nginx variables.
DEFAULT_UPSTREAM="http://gateway:8000"
: "${GATEWAY_UPSTREAM:=${DEFAULT_UPSTREAM}}"
export GATEWAY_UPSTREAM

envsubst '${GATEWAY_UPSTREAM}${GATEWAY_API_KEY}' \
  < /etc/nginx/conf.d/default.conf \
  > /tmp/default.conf \
  && mv /tmp/default.conf /etc/nginx/conf.d/default.conf

echo "Aurora Glass: proxying API requests to ${GATEWAY_UPSTREAM}"

# Execute the CMD (nginx)
exec "$@"
