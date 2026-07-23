# syntax=docker/dockerfile:1.7
ARG PYTHON_IMAGE=python:3.13.5-slim-bookworm@sha256:4c2cf9917bd1cbacc5e9b07320025bdb7cdf2df7b0ceaccb55e9dd7e30987419

FROM ${PYTHON_IMAGE} AS builder
ARG SETUPTOOLS_VERSION=80.9.0
WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m pip install --no-cache-dir "setuptools==${SETUPTOOLS_VERSION}" \
    && python -m pip wheel --no-cache-dir --no-deps --no-build-isolation \
       --wheel-dir /build/dist .

FROM ${PYTHON_IMAGE} AS runtime
ARG VERSION=0.1.0.dev0
ARG REVISION=unknown
ARG CREATED=unknown
LABEL org.opencontainers.image.title="RPi Streamer indexer" \
      org.opencontainers.image.description="Indexes a local MP4 collection and generates a static catalogue" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.created="${CREATED}" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.source="https://github.com/bdobrica/RPiStreamer"

RUN groupadd --gid 10001 rpi-streamer \
    && useradd --uid 10001 --gid 10001 --no-create-home \
       --shell /usr/sbin/nologin rpi-streamer \
    && install -d -o 10001 -g 10001 -m 0750 /state \
    && install -d -o 10001 -g 10001 -m 0755 /media
COPY --from=builder /build/dist/ /tmp/wheels/
RUN python -m pip install --no-cache-dir /tmp/wheels/*.whl \
    && rm -r /tmp/wheels

USER 10001:10001
VOLUME ["/state"]
ENTRYPOINT ["rpi-streamer"]
CMD ["serve"]
