"""Disk capacity: a ballast file on the data filesystem.

capacity C leaves the workload C bytes in total: the ballast is sized so that
the workload's files plus the space still writable equal C. If the workload
already uses more than C, the ballast shrinks to fit (free space 0) and rprof logs
``disk_overcommitted``. Writable space is ``f_bavail``: ext4 hides ~2% of clusters
(``s_resv_clusters``) from writers, and ``f_bfree`` would count them. Make the data
filesystem with ``mkfs.ext4 -m 0`` so root and non-root writers see the same limit.
"""

from __future__ import annotations

import os
from typing import Any

from .base import Controller, FileCache

BALLAST_NAME = ".rprof-ballast"


class DiskController(Controller):
    name = "disk"
    knobs = ("disk.capacity",)

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        d = self.target.data
        self.dir = d.host_path if d else None
        self.ballast = os.path.join(self.dir, BALLAST_NAME) if self.dir else None
        self.ballast_bytes = 0

    def capabilities(self):
        if not self.dir:
            return {"disk.capacity": "no data filesystem (mount one at --data-path or pass --data-dir)"}
        if not os.path.isdir(self.dir):
            return {"disk.capacity": f"data dir {self.dir} is not reachable"}
        return {"disk.capacity": None}

    def _ballast_size(self) -> int:
        try:
            return os.stat(self.ballast).st_blocks * 512  # type: ignore[arg-type]
        except OSError:
            return 0

    def apply(self, limits: dict[str, Any], changed: set[str]) -> list[str]:
        if "disk.capacity" not in changed or not self.ballast:
            return []
        cap = limits["disk.capacity"]
        errs: list[str] = []
        try:
            if cap is None:
                if os.path.exists(self.ballast):
                    os.unlink(self.ballast)
                self.ballast_bytes = 0
                if self.snap is not None:
                    self.snap.set_ballast(None)
                return errs
            st = os.statvfs(self.dir)  # type: ignore[arg-type]
            bs = st.f_frsize
            avail = st.f_bavail * bs
            cur = self._ballast_size()
            workload = max(0, (st.f_blocks - st.f_bfree) * bs - cur)
            # Free space after resizing should be cap - workload: shift space between avail and ballast.
            want = cur + avail - (cap - workload)
            if workload > cap:
                want = cur + avail
                self.warn("disk_overcommitted",
                          f"workload already uses {workload} B > capacity {cap} B; ballast fills all free space")
            want = max(0, (want // bs) * bs)
            if self.snap is not None:
                self.snap.set_ballast(self.ballast)
            fd = os.open(self.ballast, os.O_RDWR | os.O_CREAT, 0o400)
            try:
                if want > cur:
                    try:
                        os.posix_fallocate(fd, 0, want)
                    except OSError as e:
                        if e.errno != 28:  # ENOSPC: filled what we could, which is the point
                            raise
                else:
                    os.ftruncate(fd, want)
            finally:
                os.close(fd)
            self.ballast_bytes = self._ballast_size()
        except OSError as e:
            errs.append(f"ballast {self.ballast}: {e.strerror or e}")
        return errs

    def restore(self) -> list[str]:
        if self.ballast and os.path.exists(self.ballast):
            try:
                os.unlink(self.ballast)
            except OSError as e:
                return [f"remove ballast {self.ballast}: {e.strerror}"]
        if self.snap is not None:
            self.snap.set_ballast(None)
        self.ballast_bytes = 0
        return []

    def sample(self, out: dict, fc: FileCache) -> None:
        if not self.dir:
            return
        try:
            st = os.statvfs(self.dir)
        except OSError:
            return
        bs = st.f_frsize
        out["disk"] = {"used_bytes": (st.f_blocks - st.f_bfree) * bs, "free_bytes": st.f_bavail * bs,
                       "ballast_bytes": self.ballast_bytes}
