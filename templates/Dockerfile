# Project image for __NAME__, layered on the shared boxen base.
# Add this project's toolchain here, then run `boxen update __NAME__` (or
# `boxen update` inside the container, where this file is /boxen/Dockerfile).
# Install outside the home folder (/opt, /usr/local): the home folder is a
# volume that masks whatever the image puts there. Tools installed under the
# home folder from inside the container persist without a rebuild.
# End with USER __USER__: the container runs as that user either way.
FROM boxen-base:latest

# Example:
# USER root
# RUN apt-get update \
#  && apt-get install -y --no-install-recommends golang-go \
#  && rm -rf /var/lib/apt/lists/*
# USER __USER__
