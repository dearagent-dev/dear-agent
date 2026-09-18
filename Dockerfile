# Herald container image: control plane and runner share one image.
# OpenShift runs containers with an arbitrary UID, so the image must be group-writable
# where it writes and must not assume root.
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN groupadd --gid 1001 herald \
    && useradd --uid 1001 --gid 1001 --create-home --shell /usr/sbin/nologin herald

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --upgrade pip && pip install .

# The git worktree lives in an emptyDir; make /work group-writable for the arbitrary UID.
RUN mkdir -p /work && chgrp -R 0 /work && chmod -R g+rwX /work

USER 1001

ENV HERALD_BACKEND=memory \
    HERALD_HOST=0.0.0.0 \
    HERALD_PORT=8080

EXPOSE 8080

# Default to the control plane; the runner Job overrides the command.
CMD ["herald-http"]
