"""两路取气选择快照。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class IntakeSnapshot:
    """取气选择器某一时刻的状态：配额、压力、活动路与切换台账。"""

    policy: str
    active_source: str
    switching: bool
    drawn_total_kg: float
    switch_count: int
    failed_switches: int
    epoch: int
    sources: dict

    def as_dict(self) -> dict:
        return {
            "policy": self.policy,
            "active_source": self.active_source,
            "switching": self.switching,
            "drawn_total_kg": round(self.drawn_total_kg, 3),
            "switch_count": self.switch_count,
            "failed_switches": self.failed_switches,
            "epoch": self.epoch,
            "sources": self.sources,
        }
