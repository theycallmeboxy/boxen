# The build container for __NAME__: the toolchain this project builds and
# runs with. Add what it needs, then apply with `boxen env apply` (from the
# project folder). You're root here, so apt-get works directly; there's no
# sudo and no need for USER lines.
#
# Install system packages and SDKs here, not at run time: anything installed
# at run time is lost when the container is recreated. Only the project
# folder and the download caches (/root/.cache) survive. Set paths with ENV
# (~/.bashrc isn't read for boxen run).
FROM boxen-env:latest

# Example:
# RUN apt-get update \
#  && apt-get install -y --no-install-recommends golang-go \
#  && rm -rf /var/lib/apt/lists/*
