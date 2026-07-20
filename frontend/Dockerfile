# ── Aurora Glass Frontend Container ─────────────────────────────────────
# Serves the OpenCode Gateway observability dashboard static files via nginx.
# API requests are proxied to the Gateway backend.

FROM nginx:alpine

# Install curl (HEALTHCHECK) and gettext (envsubst for runtime config).
RUN apk add --no-cache curl gettext

# Copy static frontend files.
<<<<<<< HEAD
# Uses ./frontend/ prefix so the Dockerfile works when the build context is
# the repo root — both `docker build -f frontend/Dockerfile -t aurora-glass .`
# and docker-compose (context: .) resolve the same way.
COPY ./frontend/ /usr/share/nginx/html/

# Copy nginx configuration (template — GATEWAY_UPSTREAM substituted at runtime).
COPY ./frontend/nginx.conf /etc/nginx/conf.d/default.conf

# Copy and install the entrypoint that substitutes env vars at runtime.
=======
# Uses ./frontend/ so the Dockerfile works with a repo-root build context.
COPY ./frontend/ /usr/share/nginx/html/

# Copy nginx configuration template and runtime entrypoint.
COPY ./frontend/nginx.conf /etc/nginx/conf.d/default.conf
>>>>>>> origin/fix/recovery-1784533104-issue-222
COPY ./frontend/docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

# Verify the frontend files are in place.
RUN test -f /usr/share/nginx/html/index.html && \
    test -f /usr/share/nginx/html/app.js && \
    test -f /usr/share/nginx/html/style.css

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:80/ || exit 1

EXPOSE 80

ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["nginx", "-g", "daemon off;"]
