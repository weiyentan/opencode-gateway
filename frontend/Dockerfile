# ── Aurora Glass Frontend Container ─────────────────────────────────────
# Serves the OpenCode Gateway observability dashboard static files via nginx.
# API requests are proxied to the Gateway backend.
#
# The Gateway API proxy URL is configurable at runtime via the
# GATEWAY_API_URL environment variable (default: http://gateway:8000).
#
# Build:
#   docker build -f frontend/Dockerfile frontend/
#
# Run (standalone, pointing at an external Gateway):
#   docker run -e GATEWAY_API_URL=http://host.docker.internal:8000 -p 8080:80 aurora-glass
#
# Run (with docker-compose):
#   docker compose up -d    # uses the built-in network, default URL works

FROM nginx:alpine

# Install curl for HEALTHCHECK and gettext for envsubst (runtime config
# substitution of GATEWAY_API_URL in nginx.conf).
RUN apk add --no-cache curl gettext

# Copy static frontend files (excluding nginx.conf template which goes
# to a different location).
COPY index.html app.js style.css /usr/share/nginx/html/
COPY tests/ /usr/share/nginx/html/tests/

# Copy nginx configuration template.
COPY nginx.conf /etc/nginx/conf.d/default.conf

# Copy the docker entrypoint that performs envsubst substitution.
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

# Default Gateway API URL (docker-compose service name).
ENV GATEWAY_API_URL=http://gateway:8000

# Verify the frontend files are in place.
RUN test -f /usr/share/nginx/html/index.html && \
    test -f /usr/share/nginx/html/app.js && \
    test -f /usr/share/nginx/html/style.css

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:80/ || exit 1

EXPOSE 80

ENTRYPOINT ["/docker-entrypoint.sh"]
