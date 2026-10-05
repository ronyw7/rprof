"""Pydantic model of the profile file (design Appendix A.1).

Knob fields keep the raw YAML value (``"800Mi"``, ``"max"``) so profiles can be
echoed back exactly; validators check them with the parsers in ``rprof.knobs``.
``schema.json`` next to this file is generated from these models with
``python -m rprof.profile.schema``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any, Literal, Optional

from pydantic import (BaseModel, BeforeValidator, ConfigDict, Field, WithJsonSchema,
                      create_model, field_validator)

from .. import knobs as K
from .. import units as u

# Profile names and run labels end up in directory names: letters, digits, '.', '_', '-'.
NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
NAME_RULE = "must start with a letter or digit and contain only letters, digits, '.', '_' and '-' (at most 64)"

_BYTES = r"^(max|\d+\s*(Ki|Mi|Gi|Ti|K|M|G|T)?)$"
_DUR = r"^\d+(\.\d+)?\s*(ms|s|m)$"
_KNOB_SCHEMA: dict[str, dict] = {
    "cpu.cores": {"anyOf": [{"type": "number", "exclusiveMinimum": 0}, {"const": "max"}]},
    "cpu.cpus": {"anyOf": [{"type": "integer", "minimum": 0},
                           {"type": "string", "pattern": r"^(all|\d+(-\d+)?(,\d+(-\d+)?)*)$"}]},
    "cpu.period": {"type": "string", "pattern": _DUR},
    "mem.high": {"anyOf": [{"type": "integer", "minimum": 1}, {"type": "string", "pattern": _BYTES}]},
    "mem.max": {"anyOf": [{"type": "integer", "minimum": 1}, {"type": "string", "pattern": _BYTES}]},
    "mem.swap_max": {"anyOf": [{"type": "integer", "minimum": 0}, {"type": "string", "pattern": _BYTES}]},
    "io.rbps": {"anyOf": [{"type": "integer", "minimum": 1}, {"type": "string", "pattern": _BYTES}]},
    "io.wbps": {"anyOf": [{"type": "integer", "minimum": 1}, {"type": "string", "pattern": _BYTES}]},
    "io.riops": {"anyOf": [{"type": "integer", "minimum": 1}, {"const": "max"}]},
    "io.wiops": {"anyOf": [{"type": "integer", "minimum": 1}, {"const": "max"}]},
    "pids.max": {"anyOf": [{"type": "integer", "minimum": 1}, {"const": "max"}]},
    "net.rate": {"type": "string",
                 "pattern": r"^(max|\d+(\.\d+)?\s*([kKmMgGtT][iI]?)?([bB][iI][tT]|[bB][pP][sS]))$"},
    "net.delay": {"type": "string", "pattern": _DUR},
    "net.jitter": {"type": "string", "pattern": _DUR},
    "net.loss": {"type": "string", "pattern": r"^\d+(\.\d+)?\s*%$"},
    "net.partition": {"enum": ["none", "reject", "drop"]},
    "net.allow": {"type": "array", "items": {"type": "string"}},
    "disk.capacity": {"anyOf": [{"type": "integer", "minimum": 1}, {"type": "string", "pattern": _BYTES}]},
    "harness.deadline": {"anyOf": [{"type": "string", "pattern": _DUR}, {"const": "none"}]},
    "harness.feedback": {"enum": ["none", "errno", "explain"]},
}


def _checker(name: str):
    parse = K.KNOBS[name].parse

    def check(v):
        try:
            parse(v)
        except u.UnitError as e:
            raise ValueError(str(e)) from None
        return v
    return check


def _group_model(group: str) -> type[BaseModel]:
    fields = {}
    for kn in K.KNOBS.values():
        if kn.group != group:
            continue
        typ = Annotated[Any, BeforeValidator(_checker(kn.name)), WithJsonSchema(_KNOB_SCHEMA[kn.name])]
        fields[kn.key] = (Optional[typ], Field(default=None, description=f"{kn.name}, default {kn.default_raw!r}"))
    return create_model(f"{group.capitalize()}Knobs", __config__=ConfigDict(extra="forbid"), **fields)


CpuKnobs = _group_model("cpu")
MemKnobs = _group_model("mem")
IoKnobs = _group_model("io")
PidsKnobs = _group_model("pids")
NetKnobs = _group_model("net")
DiskKnobs = _group_model("disk")
HarnessKnobs = _group_model("harness")

UnifiedMap = dict[Annotated[str, Field(pattern=r"^[a-z_]+\.[a-z_.]+$")], str]


class KnobMap(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cpu: Optional[CpuKnobs] = None          # type: ignore[valid-type]
    mem: Optional[MemKnobs] = None          # type: ignore[valid-type]
    io: Optional[IoKnobs] = None            # type: ignore[valid-type]
    pids: Optional[PidsKnobs] = None        # type: ignore[valid-type]
    net: Optional[NetKnobs] = None          # type: ignore[valid-type]
    disk: Optional[DiskKnobs] = None        # type: ignore[valid-type]
    harness: Optional[HarnessKnobs] = None  # type: ignore[valid-type]
    unified: Optional[UnifiedMap] = Field(
        default=None, description="cgroup v2 file name -> exact string to write")

    @field_validator("unified", mode="before")
    @classmethod
    def _unified_values(cls, v):
        if isinstance(v, dict):
            return {k: (str(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else x)
                    for k, x in v.items()}
        return v

    def flat_raw(self) -> dict[str, Any]:
        """Knobs this map sets explicitly, as {knob: raw value}."""
        out: dict[str, Any] = {}
        for g in K.GROUPS:
            gm = getattr(self, g)
            if gm is None:
                continue
            for key, val in gm.model_dump(exclude_none=True).items():
                out[f"{g}.{key}"] = val
        for f, val in (self.unified or {}).items():
            out[K.UNIFIED_PREFIX + f] = val
        return out


class SegmentModel(KnobMap):
    from_: float = Field(alias="from", ge=0)
    to: float
    label: Optional[str] = None

    @field_validator("from_", "to", mode="before")
    @classmethod
    def _number(cls, v):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError(f"{v} is not a number of seconds")
        return v


class ProfileModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1]
    name: str = Field(pattern=NAME_PATTERN)
    clock: Literal["wall"] = "wall"
    visibility: Literal["none", "current", "full"] = "none"
    source: dict[str, Any] = Field(default_factory=dict)
    defaults: KnobMap = Field(default_factory=KnobMap)
    segments: list[SegmentModel] = Field(default_factory=list)


def json_schema() -> dict:
    s = ProfileModel.model_json_schema(by_alias=True)
    s["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    s["title"] = "rprof profile"
    return s


SCHEMA_PATH = Path(__file__).with_name("schema.json")

if __name__ == "__main__":
    SCHEMA_PATH.write_text(json.dumps(json_schema(), indent=2) + "\n")
    print(f"wrote {SCHEMA_PATH}")
