# syntax=docker/dockerfile:1.7
ARG NGINX_IMAGE=nginx:1.28.0-alpine3.21@sha256:30f1c0d78e0ad60901648be663a710bdadf19e4c10ac6782c235200619158284
FROM ${NGINX_IMAGE}
ARG VERSION=0.1.0.dev0
ARG REVISION=unknown
ARG CREATED=unknown
LABEL org.opencontainers.image.title="RPi Streamer web server" \
      org.opencontainers.image.description="Serves the generated catalogue and MP4 byte ranges" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.created="${CREATED}" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.source="https://github.com/bdobrica/RPiStreamer"

COPY deployment/container/nginx.conf /etc/nginx/nginx.conf
COPY deployment/container/site.conf /etc/nginx/conf.d/default.conf
USER 101:10001
EXPOSE 8080
STOPSIGNAL SIGQUIT
CMD ["nginx", "-g", "daemon off;"]
