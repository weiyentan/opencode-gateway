#!/bin/sh
# Substitute the frontend proxy target at runtime without touching nginx vars.

set -e

DEFAULT_UPSTREAM="http://gateway:8000"
: "${GATEWAY_UPSTREAM:=${DEFAULT_UPSTREAM}}"
export GATEWAY_UPSTREAM

envsubst '${GATEWAY_UPSTREAM}' \
  < /etc/nginx/conf.d/default.conf \
  > /tmp/default.conf
mv /tmp/default.conf /etc/nginx/conf.d/default.conf

exec "$@"
