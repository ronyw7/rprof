"""Network: netem rate/delay/jitter/loss and an iptables partition, per container netns.

Layout on each side of the veth (only while shaping is on):

    root 1: prio bands 2, every priority -> band 2 (1:2)
      1:1  plain FIFO      <- u32 filters for net.allow CIDRs
      1:2  20: netem ...   <- everything else

Container egress (``eth0`` in the netns) gets delay, jitter, loss and rate; the
host-side veth (container ingress) gets rate only. ``net.partition`` installs
``RPROF-OUT``/``RPROF-IN`` chains in the container netns; loopback and
``net.allow`` CIDRs return early, everything else is rejected or dropped.
"""

from __future__ import annotations

import ipaddress
import json
import re
import shutil
import threading
from typing import Any

from ..target.resolve import NetTarget
from ..util import run_cmd
from .base import Controller, FileCache

PRIOMAP = ["1"] * 16
CHAINS = (("RPROF-OUT", "OUTPUT", "-o", "-d"), ("RPROF-IN", "INPUT", "-i", "-s"))


def netem_args(rate_bps: int | None, delay_ms: float = 0, jitter_ms: float = 0, loss_pct: float = 0) -> list[str]:
    a: list[str] = []
    if delay_ms or jitter_ms:
        a += ["delay", f"{delay_ms:g}ms"]
        if jitter_ms:
            a.append(f"{jitter_ms:g}ms")
    if loss_pct:
        a += ["loss", f"{loss_pct:g}%"]
    if rate_bps:
        a += ["rate", f"{int(rate_bps)}bit"]
    return a


class _Side:
    """One end of the veth: the container's eth0 (in its netns) or the host veth."""

    def __init__(self, nt: NetTarget, host: bool):
        self.nt, self.host = nt, host
        self.dev = nt.host_veth if host else nt.ifname
        self.installed = False
        self.netem: list[str] | None = None
        self.allow: list[str] | None = None
        self.last_drops = 0

    def tc(self, *args: str) -> list[str]:
        cmd = ["tc", *args]
        return cmd if self.host else self.nt.nsenter(*cmd)

    @property
    def entry(self) -> dict:
        return {"side": "host" if self.host else "container", "dev": self.dev, "pid": self.nt.pid,
                "container_id": self.nt.container_id}


class NetController(Controller):
    name = "net"
    knobs = ("net.rate", "net.delay", "net.jitter", "net.loss", "net.partition", "net.allow")

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.sides: list[_Side] = []
        for nt in self.target.net:
            self.sides.append(_Side(nt, host=False))
            if nt.host_veth:
                self.sides.append(_Side(nt, host=True))
        self.partition: dict[int, tuple[str, tuple[str, ...]] | None] = {}
        self.counters_lock = threading.Lock()
        # rprof's own qdiscs and rules: their counters are 0 until rprof installs something.
        enforcing = self.snap is not None and bool(self.target.net)
        self.qdisc_drops: int | None = 0 if enforcing else None
        self.partition_hits: int | None = 0 if enforcing else None
        self._ip6_ok = shutil.which("ip6tables") is not None
        self.apply_log: list[dict] = []
        # tc and iptables counters restart when rprof reinstalls a qdisc or refills a chain;
        # bases carry the earlier totals so the sampled counters stay monotonic.
        self._drops_base = 0
        self._hits_base = 0
        self._last_hits: dict[int, int] = {}
        self.retrans: int | None = None

    def capabilities(self):
        if not self.target.net:
            r = "no container network namespace (target has no --net)"
            return {k: r for k in self.knobs}
        missing = [b for b in ("tc", "nsenter", "iptables") if shutil.which(b) is None]
        if missing:
            r = f"missing tools: {', '.join(missing)}"
            return {k: r for k in self.knobs}
        out = {k: None for k in self.knobs}
        if any(nt.host_veth is None for nt in self.target.net):
            out["net.rate"] = "host-side veth not found; net.rate would shape egress only"
        return out

    # ------------------------------------------------------------ helpers
    def _run(self, cmd: list[str], errs: list[str], ok_errors: tuple[str, ...] = ()) -> bool:
        r = run_cmd(cmd)
        self.apply_log.append({"cmd": " ".join(cmd), "rc": r.rc, "ms": round(r.ms, 1)})
        if not r.ok and not any(s in r.err for s in ok_errors):
            errs.append(f"{' '.join(cmd[-8:])}: {r.err or r.rc}")
            return False
        return True

    def _filters(self, side: _Side, allow: list[str], errs: list[str]) -> None:
        self._run(side.tc("filter", "del", "dev", side.dev, "parent", "1:"), errs,
                  ok_errors=("No such file", "Cannot find", "Invalid", "not found", "Error"))
        match = "src" if side.host else "dst"
        for i, cidr in enumerate(allow):
            net = ipaddress.ip_network(cidr)
            proto, sel = ("ip", "ip") if net.version == 4 else ("ipv6", "ip6")
            self._run(side.tc("filter", "add", "dev", side.dev, "parent", "1:", "protocol", proto,
                              "prio", str(i + 1), "u32", "match", sel, match, cidr, "flowid", "1:1"), errs)

    def _shape(self, side: _Side, args: list[str] | None, allow: list[str], errs: list[str]) -> None:
        if not args:
            if side.installed:
                if self._run(side.tc("qdisc", "del", "dev", side.dev, "root"), errs,
                             ok_errors=("No such file", "Cannot find device")):
                    side.installed = False
                    with self.counters_lock:
                        self._drops_base += side.last_drops
                        side.last_drops = 0
                    if self.snap is not None:
                        self.snap.remove_tc(side.entry)
            side.netem, side.allow = None, None
            return
        if not side.installed:
            if self.snap is not None:
                self.snap.add_tc(side.entry)  # persisted before the qdisc exists
            if not self._run(side.tc("qdisc", "replace", "dev", side.dev, "root", "handle", "1:", "prio",
                                     "bands", "2", "priomap", *PRIOMAP), errs):
                return
            side.installed = True
            side.allow = None
        if args != side.netem:
            if self._run(side.tc("qdisc", "replace", "dev", side.dev, "parent", "1:2", "handle", "20:",
                                 "netem", *args), errs):
                side.netem = args
        if allow != side.allow:
            self._filters(side, allow, errs)
            side.allow = list(allow)

    def _ipt(self, nt: NetTarget, binary: str, *args: str) -> list[str]:
        return nt.nsenter(binary, "-w", "5", *args)

    def _restore_text(self, fam: int, mode: str, allow: list[str], comment: str, install: bool,
                      had_jumps: bool) -> str:
        lines = ["*filter"]
        for chain, parent, ifopt, addropt in CHAINS:
            tag = f'-m comment --comment "{comment}"'
            if not install:
                if had_jumps:
                    lines.append(f"-D {parent} {tag} -j {chain}")
                lines += [f"-F {chain}", f"-X {chain}"]
                continue
            lines += [f":{chain} - [0:0]", f"-F {chain}", f"-A {chain} {ifopt} lo -j RETURN"]
            for cidr in allow:
                if ipaddress.ip_network(cidr).version == fam:
                    lines.append(f"-A {chain} {addropt} {cidr} -j RETURN")
            if mode == "reject":
                lines += [f"-A {chain} -p tcp {tag} -j REJECT --reject-with tcp-reset",
                          f"-A {chain} {tag} -j REJECT"]
            else:
                lines.append(f"-A {chain} {tag} -j DROP")
            if not had_jumps:
                lines.append(f"-I {parent} 1 {tag} -j {chain}")
        lines.append("COMMIT")
        return "\n".join(lines) + "\n"

    def _partition_fast(self, nt: NetTarget, mode: str, allow: list[str], comment: str, prev) -> bool:
        """Install/remove the chains with one iptables-restore per family; False to fall back."""
        install = mode != "none"
        fams = [("iptables", 4)] + ([("ip6tables", 6)] if self._ip6_ok else [])
        for binary, fam in fams:
            if self.snap is not None:
                for chain, parent, *_ in CHAINS:
                    e = {"binary": binary, "chain": chain, "parent": parent, "comment": comment,
                         "pid": nt.pid, "container_id": nt.container_id}
                    (self.snap.add_iptables if install else self.snap.remove_iptables)(e)
            text = self._restore_text(fam, mode, allow, comment, install, had_jumps=prev is not None)
            r = run_cmd(nt.nsenter(f"{binary}-restore", "--noflush", "-w", "5"), input=text)
            self.apply_log.append({"cmd": f"{binary}-restore", "rc": r.rc, "ms": round(r.ms, 1)})
            if not r.ok:
                if fam == 6:
                    self._ip6_ok = False
                    self.warn("ip6tables_unavailable", r.err[:300])
                    continue
                return False
        return True

    def _partition(self, nt: NetTarget, mode: str, allow: list[str], errs: list[str]) -> None:
        key = nt.pid
        want = None if mode == "none" else (mode, tuple(allow))
        prev = self.partition.get(key)
        if prev == want:
            return
        comment = f"rprof:{self.run_id}"
        with self.counters_lock:
            self._hits_base += self._last_hits.pop(key, 0)
        if self._partition_fast(nt, mode, allow, comment, prev):
            self.partition[key] = want
            return
        fams = [("iptables", 4)] + ([("ip6tables", 6)] if self._ip6_ok else [])
        for binary, fam in fams:
            ferrs: list[str] = []
            for chain, parent, ifopt, addropt in CHAINS:
                entry = {"binary": binary, "chain": chain, "parent": parent, "comment": comment,
                         "pid": nt.pid, "container_id": nt.container_id}
                if want is None:
                    self._run(self._ipt(nt, binary, "-D", parent, "-m", "comment", "--comment", comment,
                                        "-j", chain), ferrs, ok_errors=("No chain", "does not exist", "Bad rule"))
                    self._run(self._ipt(nt, binary, "-F", chain), ferrs, ok_errors=("No chain", "not exist"))
                    self._run(self._ipt(nt, binary, "-X", chain), ferrs, ok_errors=("No chain", "not exist"))
                    if self.snap is not None and not ferrs:
                        self.snap.remove_iptables(entry)
                    continue
                if self.snap is not None:
                    self.snap.add_iptables(entry)
                self._run(self._ipt(nt, binary, "-N", chain), ferrs, ok_errors=("exists",))
                self._run(self._ipt(nt, binary, "-F", chain), ferrs)
                self._run(self._ipt(nt, binary, "-A", chain, ifopt, "lo", "-j", "RETURN"), ferrs)
                for cidr in allow:
                    if ipaddress.ip_network(cidr).version == fam:
                        self._run(self._ipt(nt, binary, "-A", chain, addropt, cidr, "-j", "RETURN"), ferrs)
                tag = ["-m", "comment", "--comment", comment]
                if mode == "reject":
                    self._run(self._ipt(nt, binary, "-A", chain, "-p", "tcp", *tag, "-j", "REJECT",
                                        "--reject-with", "tcp-reset"), ferrs)
                    self._run(self._ipt(nt, binary, "-A", chain, *tag, "-j", "REJECT"), ferrs)
                else:
                    self._run(self._ipt(nt, binary, "-A", chain, *tag, "-j", "DROP"), ferrs)
                chk = run_cmd(self._ipt(nt, binary, "-C", parent, *tag, "-j", chain))
                if not chk.ok:
                    self._run(self._ipt(nt, binary, "-I", parent, "1", *tag, "-j", chain), ferrs)
            if fam == 6 and ferrs:
                self._ip6_ok = False
                self.warn("ip6tables_unavailable", "; ".join(ferrs[:2]))
            else:
                errs.extend(ferrs)
        self.partition[key] = want

    # ------------------------------------------------------------ apply
    def apply(self, limits: dict[str, Any], changed: set[str]) -> list[str]:
        if not changed & set(self.knobs):
            return []
        errs: list[str] = []
        self.apply_log = []
        rate, delay, jitter, loss = (limits["net.rate"], limits["net.delay"], limits["net.jitter"],
                                     limits["net.loss"])
        allow = list(limits.get("net.allow") or [])
        egress = netem_args(rate, delay, jitter, loss)
        ingress = netem_args(rate)
        for side in self.sides:
            self._shape(side, ingress if side.host else egress, allow, errs)
        for nt in self.target.net:
            self._partition(nt, limits["net.partition"], allow, errs)
        return errs

    def restore(self) -> list[str]:
        errs: list[str] = []
        for side in self.sides:
            self._shape(side, None, [], errs)
        for nt in self.target.net:
            self._partition(nt, "none", [], errs)
        return errs

    # ------------------------------------------------------------ sampling
    def _key(self, nt: NetTarget) -> str:
        if len({n.spec for n in self.target.net}) > 1:
            return f"{nt.spec.split(':', 1)[1]}/{nt.ifname}"
        return nt.ifname

    def sample(self, out: dict, fc: FileCache) -> None:
        if not self.target.net:
            return
        net: dict[str, Any] = {}
        seen_pids = set()
        for nt in self.target.net:
            if nt.pid in seen_pids:
                continue
            seen_pids.add(nt.pid)
            txt = fc.read(f"/proc/{nt.pid}/net/dev")
            if txt is None:
                continue
            wanted = {n.ifname: n for n in self.target.net if n.pid == nt.pid}
            for line in txt.splitlines()[2:]:
                name, _, rest = line.partition(":")
                name = name.strip()
                if name not in wanted:
                    continue
                f = rest.split()
                if len(f) < 16:
                    continue
                net[self._key(wanted[name])] = {
                    "rx_bytes": int(f[0]), "rx_packets": int(f[1]), "rx_drop": int(f[3]),
                    "tx_bytes": int(f[8]), "tx_packets": int(f[9]), "tx_drop": int(f[11])}
        with self.counters_lock:
            if self.retrans is not None:
                net["tcp_retrans_segs"] = self.retrans
            if self.qdisc_drops is not None:
                net["qdisc_drops"] = self.qdisc_drops
            if self.partition_hits is not None:
                net["partition_hits"] = self.partition_hits
        if net:
            out["net"] = net

    def _poll_retrans(self) -> None:
        """TCP RetransSegs from /proc/<pid>/net/snmp (a counter; 1 Hz keeps the tick cheap)."""
        total, seen, ok = 0, set(), False
        for nt in self.target.net:
            if nt.pid in seen:
                continue
            seen.add(nt.pid)
            try:
                with open(f"/proc/{nt.pid}/net/snmp") as f:
                    tcp = [ln.split()[1:] for ln in f if ln.startswith("Tcp:")]
            except OSError:
                continue
            if len(tcp) >= 2 and "RetransSegs" in tcp[0]:
                total += int(tcp[1][tcp[0].index("RetransSegs")])
                ok = True
        if ok:
            with self.counters_lock:
                self.retrans = total

    def slow_poll(self) -> None:
        """TCP retransmits, netem drops and partition-rule hits (1 Hz)."""
        self._poll_retrans()
        any_shaping = False
        for side in self.sides:
            if not side.installed:
                continue
            any_shaping = True
            r = run_cmd(side.tc("-s", "-j", "qdisc", "show", "dev", side.dev), quiet=True)
            if not r.ok:
                continue
            d = 0
            try:
                for q in json.loads(r.out or "[]"):
                    if q.get("kind") == "netem":
                        d += int(q.get("drops", 0))
            except (json.JSONDecodeError, ValueError):
                d = sum(int(x) for x in re.findall(r"qdisc netem .*?dropped (\d+)", r.out, re.S))
            with self.counters_lock:
                side.last_drops = max(d, side.last_drops)
        any_partition = False
        for nt in self.target.net:
            if not self.partition.get(nt.pid):
                continue
            any_partition = True
            h = 0
            for binary in ["iptables"] + (["ip6tables"] if self._ip6_ok else []):
                for chain, *_ in CHAINS:
                    r = run_cmd(self._ipt(nt, binary, "-L", chain, "-v", "-x", "-n"), quiet=True)
                    if not r.ok:
                        continue
                    for ln in r.out.splitlines()[2:]:
                        f = ln.split()
                        if len(f) >= 3 and f[2] in ("REJECT", "DROP") and f[0].isdigit():
                            h += int(f[0])
            with self.counters_lock:
                self._last_hits[nt.pid] = max(h, self._last_hits.get(nt.pid, 0))
        with self.counters_lock:
            if any_shaping or self.qdisc_drops is not None:
                self.qdisc_drops = self._drops_base + sum(s.last_drops for s in self.sides)
            if any_partition or self.partition_hits is not None:
                self.partition_hits = self._hits_base + sum(self._last_hits.values())
