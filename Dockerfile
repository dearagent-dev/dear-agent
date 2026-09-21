# Herald container image: control plane and runner share one image.
# Base is UBI 9 (Red Hat Universal Base Image) with the distro Python 3.12, so the image
# matches the OpenShift platform it runs on. OpenShift runs containers with an arbitrary
# UID, so the image must be group-writable where it writes and must not assume root.
FROM registry.access.redhat.com/ubi9/python-312:latest

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# The UBI python image drops to uid 1001; install system packages as root, then return.
USER 0

# git and openssh-clients come with the UBI python image; make sure they are present for the
# clone/push over SSH. bubblewrap is not shipped in UBI repos: the harness sandbox falls back
# to NoSandbox, and in Kubernetes the pod/Job is the isolation boundary (see docs/security.md).
RUN dnf install -y --setopt=install_weak_deps=False \
        git openssh-clients ca-certificates \
    && dnf clean all \
    && rm -rf /var/cache/dnf /var/cache/yum

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --upgrade pip && pip install .

# The git worktree lives in an emptyDir; make /work group-writable for the arbitrary UID.
RUN mkdir -p /work && chgrp -R 0 /work && chmod -R g+rwX /work

# UBI's python image ships uid/gid 1001; OpenShift overrides it with a namespace UID.
USER 1001

ENV HERALD_BACKEND=memory \
    HERALD_HOST=0.0.0.0 \
    HERALD_PORT=8080

EXPOSE 8080

# Default to the control plane; the runner Job overrides the command.
CMD ["herald-http"]
