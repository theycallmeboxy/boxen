# boxen core image (tagged boxen-base:core): shared by every project container.
# Built by `boxen setup` / `boxen update` with your VM user's name and UID/GID,
# so files Claude writes in ~/src show up as owned by you on the VM.
#
# Don't edit this file to add tools (it would block `boxen self-update`).
# Put additions for every project in ~/.config/boxen/base.local.Dockerfile
# (starting with FROM boxen-base:core), and per-project ones in that
# project's Dockerfile.
FROM ubuntu:24.04

ARG USERNAME
ARG USER_UID=1000
ARG USER_GID=1000

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

# Core tools.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl gnupg git openssh-client tmux less jq ripgrep \
      build-essential python3 python3-venv python3-pip \
      unzip zip xz-utils procps tzdata \
 && rm -rf /var/lib/apt/lists/*

# Signed apt repos: GitHub CLI, and Claude Code (stable channel).
# The Claude Code key fingerprint is checked against the one Anthropic publishes.
RUN install -d -m 0755 /etc/apt/keyrings \
 && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
 && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
 && curl -fsSL https://downloads.claude.ai/keys/claude-code.asc \
      -o /etc/apt/keyrings/claude-code.asc \
 && gpg --show-keys --with-colons /etc/apt/keyrings/claude-code.asc \
      | grep -q '31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE' \
 && echo "deb [signed-by=/etc/apt/keyrings/claude-code.asc] https://downloads.claude.ai/claude-code/apt/stable stable main" \
      > /etc/apt/sources.list.d/claude-code.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends gh claude-code \
 && rm -rf /var/lib/apt/lists/*

# Your user, with the same UID/GID as on the VM. Ubuntu 24.04 images ship an
# "ubuntu" user at UID 1000, so remove it first.
#
# No sudo, on purpose: root in the container could leave a setuid-root binary
# in ~/src, which is your home on the VM. Containers also run with
# no-new-privileges. Install tools through the Dockerfiles instead.
RUN test -n "$USERNAME" || (echo "USERNAME build arg is required" >&2; exit 1) \
 && (userdel -r ubuntu 2>/dev/null || true) \
 && (getent group "$USER_GID" >/dev/null || groupadd -g "$USER_GID" "$USERNAME") \
 && useradd -l -m -s /bin/bash -u "$USER_UID" -g "$USER_GID" "$USERNAME" \
 && install -d -o "$USER_UID" -g "$USER_GID" \
      "/home/$USERNAME/.claude" "/home/$USERNAME/src"

# GitHub access: git and gh both read the project's short-lived token from
# /run/boxen/gh-token (mounted read-only; boxen refreshes it on the VM).
COPY git-credential-boxen gh /usr/local/bin/
RUN chmod 0755 /usr/local/bin/git-credential-boxen /usr/local/bin/gh

USER $USERNAME
WORKDIR /home/$USERNAME

# Claude's login, settings, memory and history live in the shared claude-config
# volume mounted here. CLAUDE_CONFIG_DIR keeps .claude.json (the login) inside it.
ENV CLAUDE_CONFIG_DIR=/home/$USERNAME/.claude

# For github.com, git asks git-credential-boxen (and only it) for credentials.
# SSH-style GitHub remotes (common in repos brought over from a Mac) are
# rewritten to HTTPS, since containers authenticate with tokens, not SSH keys.
RUN git config --global init.defaultBranch main \
 && git config --global credential.https://github.com.helper '' \
 && git config --global --add credential.https://github.com.helper boxen \
 && git config --global url.https://github.com/.insteadOf git@github.com: \
 && git config --global --add url.https://github.com/.insteadOf ssh://git@github.com/

CMD ["bash"]
