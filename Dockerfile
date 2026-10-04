# syntax=docker/dockerfile:1
#
# Optional Docker deployment, in parallel to the LXC install path (see install.sh).
# Builds the same "app" package via pip - no separate packaging mechanism. There is
# no systemd and no privileged companion agent in this image: in-app updates and
# NTP/timezone management are handled by the LXC deployment only (see app/main.py,
# PVE_USV_DEPLOYMENT).
#
# Multi-arch: published for linux/amd64, linux/arm64 (Raspberry Pi 3/4/5 on a 64-bit OS)
# and linux/arm/v7 (Raspberry Pi 3/4/5 on a 32-bit OS). amd64 and arm64 get prebuilt
# wheels for every dependency; on arm/v7 several (cffi, argon2-cffi-bindings, httptools,
# uvloop, pyyaml, websockets) only ship sources, so they are compiled in the build stage
# below and the runtime image stays free of a compiler.
FROM python:3.11-slim AS build

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libffi-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src

# Only what "pip install ." needs: pyproject.toml references README.md as the
# package's long description.
COPY pyproject.toml README.md ./
COPY app ./app

# The app and all its dependencies as wheels: downloaded where available, built otherwise.
RUN pip wheel --no-cache-dir --wheel-dir /wheels .

FROM python:3.11-slim AS runtime

# Dedicated non-root user, mirroring the "pveusv" system user used by the LXC deploy.
RUN useradd --system --create-home --home-dir /opt/pve-usv --shell /usr/sbin/nologin pveusv

WORKDIR /opt/pve-usv

# Offline install from the build stage's wheels - nothing is compiled or fetched here.
RUN --mount=type=bind,from=build,source=/wheels,target=/wheels \
    pip install --no-cache-dir --no-index --find-links=/wheels pve-usv \
    && mkdir -p /etc/pve-usv /var/lib/pve-usv \
    && chown -R pveusv:pveusv /etc/pve-usv /var/lib/pve-usv

ENV PVE_USV_DEPLOYMENT=docker \
    PVE_USV_CONFIG=/etc/pve-usv/config.yaml \
    PVE_USV_DB=/var/lib/pve-usv/events.db \
    PYTHONUNBUFFERED=1

USER pveusv
EXPOSE 8080
VOLUME ["/etc/pve-usv", "/var/lib/pve-usv"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health').status==200 else 1)"

# Console script from pyproject.toml [project.scripts]; binds 0.0.0.0:8080 (app/main.py).
CMD ["pve-usv"]
