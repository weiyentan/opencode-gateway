# ── Aurora Glass Frontend Container ─────────────────────────────────────
# Serves the OpenCode Gateway observability dashboard static files via nginx.
# API requests are proxied to the Gateway backend.

FROM nginx:alpine

# Install curl for HEALTHCHECK.
RUN apk add --no-cache curl

# Copy static frontend files.
COPY . /usr/share/nginx/html/

# Remove the Dockerfile and development files from the web root.
RUN rm -f /usr/share/nginx/html/Dockerfile /usr/share/nginx/html/nginx.conf

# Copy nginx configuration.
COPY nginx.conf /etc/nginx/conf.d/default.conf

# Verify the frontend files are in place.
RUN test -f /usr/share/nginx/html/index.html && \
    test -f /usr/share/nginx/html/app.js && \
    test -f /usr/share/nginx/html/style.css

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:80/ || exit 1

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
