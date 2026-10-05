# rprof-netpeer: the far end of network tests (iperf3 server on 5201, HTTP on 80).
FROM debian:bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends iperf3 python3 iputils-ping \
 && rm -rf /var/lib/apt/lists/*
RUN mkdir -p /srv && head -c 10485760 /dev/zero > /srv/10M.bin && echo ok > /srv/index.html
WORKDIR /srv
EXPOSE 80 5201
# iperf3 -s exits on some malformed connections (e.g. a bare port probe), so keep restarting it.
CMD ["sh", "-c", "(while true; do iperf3 -s >/dev/null 2>&1; sleep 0.1; done) & exec python3 -m http.server 80"]
