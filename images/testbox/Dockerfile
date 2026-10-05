# rprof-testbox: workloads for rprof's smoke, fidelity and enforcement tests.
FROM debian:bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends stress-ng fio iperf3 curl iputils-ping python3 sqlite3 \
      postgresql-client procps iproute2 ca-certificates coreutils \
 && rm -rf /var/lib/apt/lists/*
COPY hog-mem /usr/local/bin/hog-mem
RUN chmod 0755 /usr/local/bin/hog-mem
CMD ["sleep", "infinity"]
