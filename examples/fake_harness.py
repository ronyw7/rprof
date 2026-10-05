#!/usr/bin/env python3
"""A scripted stand-in for an agent harness: runs tool calls with `docker exec` and reports them to rprof.

    sudo rprof run --target docker:sbx --profile p.yaml -- python examples/fake_harness.py sbx \
        "0.5:hog-mem 128M 2" "4:hog-mem 512M 2" "8.5:stress-ng --cpu 2 --timeout 3s"

Each argument is "START_S:COMMAND"; calls start at the given run time and may overlap.
"""

import shlex
import subprocess
import sys
import threading
import time

from rprof.client import Client


def main() -> int:
    container, specs = sys.argv[1], sys.argv[2:]
    rp = Client()
    t0 = time.monotonic() - rp.state().t      # align with the run clock
    print("view:", (rp.view().text if rp.view() else None))

    def one(i: int, start: float, cmd: str):
        time.sleep(max(0.0, t0 + start - time.monotonic()))
        cid = f"c{i}"
        info = rp.tool_start(cid, cmd, step=i)
        a = time.monotonic()
        try:
            r = subprocess.run(["docker", "exec", container, *shlex.split(cmd)], capture_output=True,
                               text=True, timeout=info.deadline_s)
            code, timed_out, output = r.returncode, False, r.stdout + r.stderr
        except subprocess.TimeoutExpired:
            code, timed_out, output = None, True, ""
        fb = rp.tool_end(cid, code, time.monotonic() - a, timed_out, output=output)
        print(f"{cid} seg={info.segment} exit={code} cause={fb.cause} explain={fb.explain}", flush=True)

    ths = []
    for i, s in enumerate(specs, 1):
        start, _, cmd = s.partition(":")
        th = threading.Thread(target=one, args=(i, float(start), cmd))
        th.start()
        ths.append(th)
    for th in ths:
        th.join()
    rp.mark("task_done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
