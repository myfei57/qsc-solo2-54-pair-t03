"""气源切换记录。

每一次切换（含失败回退）都是一条不可变记录：离开侧、进入侧、离开侧声明的
切换方式、缓冲流量、重叠期与稳定期时长、开始结束压力、结果（成功或回退）。
记录与审计里的 ``supply_switch`` 事件一一对应，是"切换记录与审计一致性"核对
的事实来源之一。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import IMMEDIATE_SWITCH

#: 切换成功：旧路已关，新路稳定供气。
SWITCH_OK = "completed"
#: 切换失败回退：新路没打开，回到原路继续供气。
SWITCH_ROLLED_BACK = "rolled_back"


@dataclass(frozen=True)
class SwitchRecord:
    """一次气源切换的完整时序记录。"""

    seq: int
    ts: float
    source_from: str
    source_to: str
    switch_mode: str
    buffered: bool
    pre_switch_rate_kg_per_min: float
    overlap_s: float
    settle_s: float
    outlet_before_mpa: float
    outlet_during_mpa: float
    outlet_after_mpa: float
    result: str
    reason: str

    @property
    def pressure_continuous(self) -> bool:
        """切换全程出口压力是否保持不断档（只允许爬升，不允许跌落）。"""

        return self.outlet_during_mpa + 1e-9 >= self.outlet_before_mpa and (
            self.result == SWITCH_ROLLED_BACK or self.outlet_after_mpa + 1e-9 >= self.outlet_during_mpa
        )

    def as_dict(self) -> dict:
        return {
            "seq": self.seq,
            "ts": round(self.ts, 3),
            "source_from": self.source_from,
            "source_to": self.source_to,
            "switch_mode": self.switch_mode,
            "immediate": self.switch_mode == IMMEDIATE_SWITCH,
            "buffered": self.buffered,
            "pre_switch_rate_kg_per_min": round(self.pre_switch_rate_kg_per_min, 3),
            "overlap_s": round(self.overlap_s, 3),
            "settle_s": round(self.settle_s, 3),
            "outlet_before_mpa": round(self.outlet_before_mpa, 3),
            "outlet_during_mpa": round(self.outlet_during_mpa, 3),
            "outlet_after_mpa": round(self.outlet_after_mpa, 3),
            "pressure_continuous": self.pressure_continuous,
            "result": self.result,
            "rolled_back": self.result == SWITCH_ROLLED_BACK,
            "reason": self.reason,
        }
