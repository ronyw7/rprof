#!/usr/bin/env python3
"""A scripted fake agent (no LLM) for rprof's end-to-end test.

It runs a fixed list of SQL tool calls against SQLite (inside the sandbox) and
Postgres (a sidecar container), reporting each call to rprof. When a call fails it
does what an agent that reschedules would do: wait for the current limit window
to end (from rprof's `state` reply), then retry the same call. Every statement is
idempotent, so the final database state must match an unconstrained run.

    sudo rprof run --target cgroup:<parent slice> --net docker:sbx --profile p.yaml -- \
        python examples/sql_agent.py --sbx sbx --pg pg --steps 20 --dump out/
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

from rprof.client import Client

PSQL = "PGPASSWORD=pw psql -h {pg} -U postgres -d postgres -v ON_ERROR_STOP=1 -q -X -1 -c \"{sql}\""
SQLITE = "sqlite3 /data/agent.db \"{sql}\""


def calls(steps: int, pg: str) -> list[str]:
    out = [SQLITE.format(sql="CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY, name TEXT, qty INTEGER);"),
           PSQL.format(pg=pg, sql="CREATE TABLE IF NOT EXISTS orders (id INT PRIMARY KEY, item INT, qty INT);")]
    i = 0
    while len(out) < steps:
        i += 1
        if i % 2:
            out.append(SQLITE.format(sql=f"INSERT OR IGNORE INTO items VALUES ({i}, 'item-{i}', {i * 3});"))
        else:
            out.append(PSQL.format(pg=pg, sql=f"INSERT INTO orders VALUES ({i}, {i - 1}, {i * 2}) "
                                              "ON CONFLICT (id) DO NOTHING;"))
    return out[:steps]


def run(container: str, cmd: str, timeout: float | None) -> tuple[int | None, str, bool]:
    try:
        p = subprocess.run(["docker", "exec", container, "sh", "-c", cmd], capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, p.stdout + p.stderr, False
    except subprocess.TimeoutExpired:
        return None, "timed out", True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sbx", required=True)
    ap.add_argument("--pg", required=True)
    ap.add_argument("--steps", type=int, default=20)
    ap.add_argument("--pace", type=float, default=2.0, help="seconds between call starts")
    ap.add_argument("--max-retries", type=int, default=10)
    ap.add_argument("--dump", type=Path, help="write sqlite.sql and pg.sql here at the end")
    a = ap.parse_args()
    rp = Client(timeout_s=5)
    plan = calls(a.steps, a.pg)
    t_next = time.monotonic()
    for step, cmd in enumerate(plan, 1):
        time.sleep(max(0.0, t_next - time.monotonic()))
        t_next = time.monotonic() + a.pace
        for attempt in range(a.max_retries + 1):
            cid = f"s{step}" + (f"r{attempt}" if attempt else "")
            info = rp.tool_start(cid, cmd, step=step)
            t0 = time.monotonic()
            code, out, to = run(a.sbx, cmd, info.deadline_s)
            fb = rp.tool_end(cid, code, time.monotonic() - t0, to, output=out)
            print(f"step {step} try {attempt}: seg={info.segment} exit={code} cause={fb.cause}"
                  + (f" ({out.strip()[:80]})" if code else ""), flush=True)
            if code == 0:
                break
            st = rp.state()
            wait = (st.next["t"] - st.t + 0.3) if st.next else 1.0
            time.sleep(max(0.5, wait))   # reschedule: retry once the window is over
        else:
            print(f"step {step}: gave up", flush=True)
            return 1
    rp.mark("task_done", steps=a.steps)
    if a.dump:
        a.dump.mkdir(parents=True, exist_ok=True)
        _, s, _ = run(a.sbx, "sqlite3 /data/agent.db .dump", 60)
        (a.dump / "sqlite.sql").write_text(s)
        _, s, _ = run(a.pg, "pg_dump -U postgres --data-only --inserts -t orders postgres", 60)
        # Drop comments and pg_dump's per-dump random \restrict key so dumps compare by content.
        (a.dump / "pg.sql").write_text("\n".join(ln for ln in s.splitlines()
                                                 if not ln.startswith(("--", "\\restrict", "\\unrestrict"))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
