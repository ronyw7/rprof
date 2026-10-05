"""Host-side validation: does the target host support every knob a profile uses?"""

from __future__ import annotations

from .. import controllers as C
from .. import knobs as K
from . import Profile


def used_knobs(profile: Profile) -> set[str]:
    """Knobs the profile sets to a limiting value somewhere (what capability gating checks)."""
    used = set()
    for name in profile.explicit:
        if K.is_unified(name):
            used.add(name)
            continue
        vals = [profile.defaults[name]] + [s.values[name] for s in profile.segments if name in s.values]
        if any(not K.is_default(name, v) for v in vals):
            used.add(name)
    return {k for k in used if K.is_unified(k) or K.KNOBS[k].enforced}


def managed_knobs(profile: Profile) -> set[str]:
    """Every enforced knob in a group the profile mentions, plus explicit unified files.

    Groups a profile never mentions are left exactly as the container had them.
    """
    groups = {K.KNOBS[k].group for k in profile.explicit if not K.is_unified(k)}
    out = {k.name for k in K.KNOBS.values() if k.group in groups and k.enforced}
    out |= {k for k in profile.explicit if K.is_unified(k)}
    return out


def host_capabilities(target, profile: Profile | None = None, selftest: dict | None = None) -> dict[str, str | None]:
    """knob -> None if the host can enforce it, else a reason (static checks plus selftest results)."""
    unified = {k[len(K.UNIFIED_PREFIX):] for k in (profile.explicit if profile else ()) if K.is_unified(k)}
    caps = C.capabilities(C.build(target, unified_files=unified))
    for k, v in ((selftest or {}).get("knobs") or {}).items():
        if isinstance(v, dict) and v.get("ok") is False and caps.get(k) is None:
            caps[k] = f"failed selftest: {v.get('detail', '')}".strip()
    return caps


def host_problems(target, profile: Profile, selftest: dict | None = None) -> list[tuple[str, str]]:
    caps = host_capabilities(target, profile, selftest)
    return [(f"host.{k}", caps[k]) for k in sorted(used_knobs(profile)) if caps.get(k)]
