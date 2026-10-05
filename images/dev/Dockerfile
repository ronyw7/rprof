# rprof-dev: run rprof against Docker Desktop's Linux VM (cgroup v2) from a Mac.
# The repo is bind-mounted at /src; nothing is copied in. See images/dev/README.md.
FROM python:3.12-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends docker-cli iproute2 iptables util-linux procps git \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /src
CMD ["bash"]
