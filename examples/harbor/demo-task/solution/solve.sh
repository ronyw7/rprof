#!/bin/bash
# Harbor's oracle agent runs this script, a scripted stand-in for an agent's work:
# CPU, then memory (retried smaller if it is killed), then disk.
stress-ng --cpu 2 --timeout 20s --quiet                 # 0–20 s: two busy cores
sleep 12
hog-mem 1G 4 || hog-mem 256M 4                          # ~32 s: 1 GiB for 4 s; 256 MiB if that is killed
dd if=/dev/zero of=/app/blob bs=1M count=300 oflag=direct status=none
echo done > /app/out.txt
