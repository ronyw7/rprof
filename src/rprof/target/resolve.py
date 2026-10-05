"""Target resolver: ``docker:NAME`` or ``cgroup:PATH`` -> cgroup, PIDs, netns, block device, data fs."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..util import RprofError, TargetNotFound, run_cmd
from . import docker
from .cgroup import Cgroup, cgroup_of_pid, cgroup_root, is_cgroup2, proc_root


@dataclass
class NetTarget:
    spec: str
    pid: int
    ifname: str
    host_veth: str | None
    addrs: list[str] = field(default_factory=list)
    container_id: str | None = None

    def nsenter(self, *cmd: str) -> list[str]:
        return ["nsenter", "-t", str(self.pid), "-n", "--", *cmd]


@dataclass
class DataMount:
    container_path: str | None
    host_path: str              # where rprof reads statvfs and writes the ballast
    source: str | None          # as reported by docker
    fstype: str | None
    device: str | None          # MAJ:MIN of the filesystem


@dataclass
class Target:
    spec: str
    kind: str                   # docker | cgroup
    cgroup: Cgroup
    container_id: str | None = None
    container_name: str | None = None
    init_pid: int | None = None
    net: list[NetTarget] = field(default_factory=list)
    io_device: str | None = None        # whole-disk MAJ:MIN for io.max
    io_device_name: str | None = None
    io_device_source: str | None = None
    data: DataMount | None = None

    def pids(self) -> list[int]:
        return self.cgroup.procs(recursive=True)

    @property
    def lock_key(self) -> str:
        return self.cgroup.rel.strip("/").replace("/", "_") or "root"

    def to_meta(self) -> dict:
        return {
            "spec": self.spec,
            "kind": self.kind,
            "cgroup_path": str(self.cgroup.path),
            "container_id": self.container_id,
            "container_name": self.container_name,
            "init_pid": self.init_pid,
            "net": [asdict(n) for n in self.net],
            "io_device": self.io_device,
            "io_device_name": self.io_device_name,
            "io_device_source": self.io_device_source,
            "data_mount": asdict(self.data) if self.data else None,
        }


def _same_mount_ns_as_init() -> bool:
    try:
        a, b = os.stat("/proc/1/root/"), os.stat("/")
        return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)
    except OSError:
        return True


def host_path(path: str) -> str:
    """A path in the host (PID 1) mount namespace, reachable from wherever rprof runs."""
    if _same_mount_ns_as_init():
        return path
    return "/proc/1/root" + path


def whole_disk(majmin: str) -> tuple[str, str | None]:
    """Partition MAJ:MIN -> whole-disk MAJ:MIN (io.max is per disk)."""
    sysdev = Path("/sys/dev/block") / majmin
    try:
        real = sysdev.resolve()
        if (real / "partition").exists():
            parent = real.parent
            return (parent / "dev").read_text().strip(), parent.name
        return majmin, real.name
    except OSError:
        return majmin, None


def _dev_of(path: str) -> str | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return f"{os.major(st.st_dev)}:{os.minor(st.st_dev)}"


def parse_io_device(spec: str) -> tuple[str, str | None]:
    if ":" in spec and spec.replace(":", "").isdigit():
        return whole_disk(spec)
    try:
        st = os.stat(spec)
    except OSError as e:
        raise RprofError(f"--io-device {spec}: {e.strerror}") from None
    return whole_disk(f"{os.major(st.st_rdev)}:{os.minor(st.st_rdev)}")


def _mountinfo(pid: int) -> list[dict]:
    out = []
    try:
        text = (proc_root() / str(pid) / "mountinfo").read_text()
    except OSError:
        return out
    for line in text.splitlines():
        pre, _, post = line.partition(" - ")
        a, b = pre.split(), post.split()
        if len(a) >= 5 and len(b) >= 2:
            out.append({"majmin": a[2], "root": a[3], "mountpoint": a[4], "fstype": b[0], "source": b[1]})
    return out


def _net_targets(spec: str) -> list[NetTarget]:
    if not spec.startswith("docker:"):
        raise RprofError(f"--net {spec}: only docker:NAME is supported")
    name = spec.split(":", 1)[1]
    info, pid = docker.running_pid(name)
    r = run_cmd(["nsenter", "-t", str(pid), "-n", "--", "ip", "-j", "addr", "show"], timeout=5)
    if not r.ok:
        raise RprofError(f"cannot list interfaces of {spec}: {r.err}")
    host_idx = {}
    for d in Path("/sys/class/net").iterdir() if Path("/sys/class/net").exists() else []:
        try:
            host_idx[int((d / "ifindex").read_text())] = d.name
        except (OSError, ValueError):
            pass
    out = []
    for link in json.loads(r.out or "[]"):
        if link.get("ifname") == "lo" or link.get("link_type") == "loopback":
            continue
        peer = link.get("link_index")
        if not peer or "UP" not in link.get("flags", []):
            continue  # tunl0/gre0/sit0-style pseudo-devices: no veth peer, down
        addrs = [f"{a['local']}/{a['prefixlen']}" for a in link.get("addr_info", []) if "local" in a]
        out.append(NetTarget(spec=spec, pid=pid, ifname=link["ifname"],
                             host_veth=host_idx.get(peer) if peer else None,
                             addrs=addrs, container_id=info.get("Id")))
    return out


def resolve(spec: str, net_specs: list[str] | None = None, io_device: str | None = None,
            data_path: str | None = "/data", data_dir: str | None = None,
            with_net: bool = True) -> Target:
    if not is_cgroup2():
        raise RprofError(f"{cgroup_root()} is not cgroup v2; rprof needs a unified (v2) hierarchy", 73)

    if spec.startswith("docker:"):
        name = spec.split(":", 1)[1]
        info, pid = docker.running_pid(name)
        cid = info["Id"]
        cg_path = cgroup_of_pid(pid)
        # If PID 1 sits in a sub-cgroup the container created itself, climb to the container's own.
        p = cg_path
        while p != cgroup_root() and cid[:12] not in p.name and p.parent != p:
            p = p.parent
        if cid[:12] in p.name:
            cg_path = p
        t = Target(spec=spec, kind="docker", cgroup=Cgroup(cg_path), container_id=cid,
                   container_name=info.get("Name", "").lstrip("/"), init_pid=pid)
        if with_net and not net_specs:
            mode = info.get("HostConfig", {}).get("NetworkMode", "")
            if mode not in ("host", "none") and not mode.startswith("container:"):
                net_specs = [spec]
        # Data filesystem: the mount at data_path inside the container.
        mounts = {m.get("Destination"): m for m in info.get("Mounts", [])}
        minfo = {m["mountpoint"]: m for m in _mountinfo(pid)}
        if data_dir:
            t.data = DataMount(None, data_dir, None, None, _dev_of(data_dir))
        elif data_path and data_path in minfo:
            m = mounts.get(data_path)
            src = m.get("Source") if m else None
            hp = host_path(src) if src and (m or {}).get("Type") in ("bind", "volume") else f"/proc/{pid}/root{data_path}"
            mi = minfo[data_path]
            t.data = DataMount(data_path, hp, src, mi["fstype"], mi["majmin"])
        # Block device: data volume's disk, else Docker's root directory.
        if io_device:
            t.io_device, t.io_device_name = parse_io_device(io_device)
            t.io_device_source = "--io-device"
        else:
            dev = t.data.device if t.data else None
            if dev and not dev.startswith("0:"):
                t.io_device, t.io_device_name = whole_disk(dev)
                t.io_device_source = f"data mount {data_path}"
            else:
                dev = _dev_of(host_path(docker.root_dir()))
                if dev and not dev.startswith("0:"):
                    t.io_device, t.io_device_name = whole_disk(dev)
                    t.io_device_source = f"docker root {docker.root_dir()}"
    elif spec.startswith("cgroup:"):
        raw = spec.split(":", 1)[1]
        path = Path(raw) if raw.startswith("/") else cgroup_root() / raw
        if str(path).startswith("/sys/fs/cgroup") and cgroup_root() != Path("/sys/fs/cgroup"):
            path = cgroup_root() / path.relative_to("/sys/fs/cgroup")
        cg = Cgroup(path)
        if not cg.exists():
            raise TargetNotFound(f"cgroup {path} does not exist")
        t = Target(spec=spec, kind="cgroup", cgroup=cg)
        if data_dir:
            t.data = DataMount(None, data_dir, None, None, _dev_of(data_dir))
        if io_device:
            t.io_device, t.io_device_name = parse_io_device(io_device)
            t.io_device_source = "--io-device"
        elif t.data and t.data.device and not t.data.device.startswith("0:"):
            t.io_device, t.io_device_name = whole_disk(t.data.device)
            t.io_device_source = "--data-dir"
    else:
        raise RprofError(f"bad target {spec!r}: use docker:NAME or cgroup:PATH", 2)

    if with_net:
        for ns in net_specs or []:
            t.net.extend(_net_targets(ns))
    return t
