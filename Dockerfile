# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm AS builder
WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY custom_components/house_climate/controller/ ./custom_components/house_climate/controller/
RUN python -m pip wheel --no-cache-dir --no-deps --wheel-dir /wheels .

FROM python:3.12-slim-bookworm
ARG BUILD_VERSION=0.7.2
LABEL org.opencontainers.image.source="https://github.com/matthewpucc/home-climate" \
      org.opencontainers.image.title="Home Climate controller" \
      io.hass.type="addon" \
      io.hass.version="${BUILD_VERSION}" \
      io.hass.arch="aarch64|amd64"
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
COPY --from=builder /wheels /wheels
RUN python -m pip install --no-cache-dir --no-deps /wheels/*.whl \
    && rm -rf /wheels
COPY config.example.toml /opt/home-climate/config.example.toml
WORKDIR /opt/home-climate
# Supervisor owns /data/options.json and the mounted HA config directory.
# No host privileges, host networking, or Supervisor management API are needed.
ENTRYPOINT ["python", "-m", "house_climate.addon"]
