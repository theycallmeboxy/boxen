# boxen-env:core, the base of every project's build container.
# Built by `boxen setup` / `boxen update` under rootless Docker, where root in
# a container is your own user on the VM: files a build writes in ~/src show
# up as yours, and apt works without sudo.
#
# Don't edit this file to add tools (it would block `boxen self-update`).
# Additions for every project go in ~/.config/boxen/env.local.Dockerfile
# (starting with FROM boxen-env:core); per-project ones in that project's
# .boxen/Dockerfile.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl wget gnupg git openssh-client less jq ripgrep file \
      build-essential cmake pkg-config python3 python3-venv python3-pip \
      unzip zip xz-utils procps tzdata \
 && rm -rf /var/lib/apt/lists/*

# `boxen run -d/--logs/--stop/--ps`: background processes in this container.
COPY boxen-bg /usr/local/bin/boxen-bg
RUN chmod 0755 /usr/local/bin/boxen-bg

WORKDIR /root
CMD ["sleep", "infinity"]
