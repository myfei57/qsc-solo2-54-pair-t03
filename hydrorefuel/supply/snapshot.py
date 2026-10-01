"""双路气源状态快照。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GasSourceSnapshot:
    """单路气源某一时刻的状态。"""

    key: str
    label: str
    pressure_mpa: float
    measured: bool
    valve_open: bool
    low_mpa: float
    quota_kg: float
    used_kg: float
    switch_mode: str
    faulted: bool

    @property
    def remaining_kg(self) -> float:
        """剩余配额。"""

        return max(0.0, self.quota_kg - self.used_kg)

    @property
    def above_low(self) -> bool:
        """实测压力是否仍高于下限。"""

        return self.measured and self.pressure_mpa > self.low_mpa

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "pressure_mpa": round(self.pressure_mpa, 3),
            "measured": self.measured,
            "valve_open": self.valve_open,
            "low_mpa": round(self.low_mpa, 3),
            "quota_kg": round(self.quota_kg, 3),
            "used_kg": round(self.used_kg, 3),
            "remaining_kg": round(self.remaining_kg, 3),
            "above_low": self.above_low,
            "switch_mode": self.switch_mode,
            "faulted": self.faulted,
        }


@dataclass(frozen=True)
class SupplySnapshot:
    """双路气源汇流排某一时刻的状态。"""

    active: str | None
    rate_kg_per_min: float
    policy: str
    sources: dict[str, GasSourceSnapshot]

    def as_dict(self) -> dict:
        return {
            "active": self.active,
            "rate_kg_per_min": round(self.rate_kg_per_min, 3),
            "policy": self.policy,
            "sources": {key: self.sources[key].as_dict() for key in sorted(self.sources)},
            "total": {
                "quota_kg": round(sum(item.quota_kg for item in self.sources.values()), 3),
                "used_kg": round(sum(item.used_kg for item in self.sources.values()), 3),
                "remaining_kg": round(sum(item.remaining_kg for item in self.sources.values()), 3),
            },
        }
