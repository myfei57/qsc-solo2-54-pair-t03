"""单路气源模型。

每路气源保存实测入口压力、阀门开合、配额用量与一个故障标志。压力只由
:meth:`GasSource.measure` 显式上报（模拟变送器），抽气按压降系数折算压力
下降；未测量过的气源一律视为不可用，避免拿零压硬抽。阀门互锁与故障注入都在
这里收口，汇流排切换时序只调用这些原语。
"""

from __future__ import annotations

from ..config import GasSourceLimits
from ..errors import InterlockError, PressureError

CLOSED = False
OPEN = True


class GasSource:
    """一路槽车或瓶组气源。"""

    def __init__(self, limits: GasSourceLimits) -> None:
        self._limits = limits
        self._pressure_mpa = 0.0
        self._measured = False
        self._valve = CLOSED
        self._used_kg = 0.0
        self._faulted = False

    @property
    def key(self) -> str:
        return self._limits.key

    @property
    def label(self) -> str:
        return self._limits.label

    @property
    def limits(self) -> GasSourceLimits:
        return self._limits

    @property
    def pressure_mpa(self) -> float:
        return self._pressure_mpa

    @property
    def measured(self) -> bool:
        return self._measured

    @property
    def valve_open(self) -> bool:
        return self._valve == OPEN

    @property
    def used_kg(self) -> float:
        return self._used_kg

    @property
    def faulted(self) -> bool:
        return self._faulted

    def remaining_kg(self) -> float:
        """剩余配额。"""

        return max(0.0, self._limits.quota_kg - self._used_kg)

    def above_low(self) -> bool:
        """实测压力是否仍高于自身下限。"""

        return self._measured and self._pressure_mpa > self._limits.low_mpa

    def available(self) -> bool:
        """压力高于下限且仍有配额（故障不影响计划阶段的资格判定）。"""

        return self.above_low() and self.remaining_kg() > 1e-9

    def measure(self, pressure_mpa: float) -> float:
        """上报一次入口实测压力，返回采用值。"""

        pressure = float(pressure_mpa)
        if pressure <= 0.0:
            raise PressureError(f"{self.label}入口压力必须为正值", component="supply")
        self._pressure_mpa = pressure
        self._measured = True
        return pressure

    def open_valve(self) -> None:
        """打开该路阀门；故障阀门打不开（切换失败的模拟点）。"""

        if self._valve == OPEN:
            raise InterlockError(f"{self.label}阀门已开", component="supply")
        if self._faulted:
            raise InterlockError(f"{self.label}阀门故障，无法开启", component="supply")
        self._valve = OPEN

    def close_valve(self) -> None:
        """关闭该路阀门。"""

        if self._valve == CLOSED:
            raise InterlockError(f"{self.label}阀门已关", component="supply")
        self._valve = CLOSED

    def draw(self, mass_kg: float) -> float:
        """从该路抽出一段气量，记账并折算压力下降，返回抽出后的压力。"""

        mass = float(mass_kg)
        if mass < 0:
            raise ValueError("抽气量不能为负")
        if self._valve != OPEN:
            raise InterlockError(f"{self.label}阀门未开，不能抽气", component="supply")
        if mass > self.remaining_kg() + 1e-9:
            raise InterlockError(f"{self.label}配额不足，无法抽取 {mass:.3f} kg", component="supply")
        self._used_kg += mass
        drop = mass * self._limits.pressure_coeff_mpa_per_kg
        # 只把压力折算到下限为止；再抽由汇流排触发切换，而不是报"超抽"。
        self._pressure_mpa = max(self._limits.low_mpa, self._pressure_mpa - drop)
        return self._pressure_mpa

    def set_fault(self, faulted: bool) -> None:
        """置位或清除阀门故障（开阀失败的演练注入点）。"""

        flag = bool(faulted)
        if flag and self._valve == OPEN:
            raise InterlockError(f"{self.label}正在供气，不能注入阀门故障", component="supply")
        self._faulted = flag

    def restore(self, state: dict) -> None:
        """从落盘台账恢复用量与最后实测压力（阀门一律回到关闭安全位）。"""

        self._used_kg = float(state.get("used_kg", 0.0))
        pressure = float(state.get("pressure_mpa", 0.0))
        self._pressure_mpa = pressure
        self._measured = bool(state.get("measured", pressure > 0.0))
        self._valve = CLOSED
        self._faulted = False

    def reset(self) -> None:
        """清回未测量、零用量的初始状态。"""

        self._pressure_mpa = 0.0
        self._measured = False
        self._valve = CLOSED
        self._used_kg = 0.0
        self._faulted = False

    def ledger(self) -> dict:
        """导出需要落盘的台账字段。"""

        return {
            "pressure_mpa": round(self._pressure_mpa, 6),
            "measured": self._measured,
            "used_kg": round(self._used_kg, 6),
        }

    def snapshot(self) -> "GasSourceSnapshot":
        from .snapshot import GasSourceSnapshot

        return GasSourceSnapshot(
            key=self._limits.key,
            label=self._limits.label,
            pressure_mpa=self._pressure_mpa,
            measured=self._measured,
            valve_open=self.valve_open,
            low_mpa=self._limits.low_mpa,
            quota_kg=self._limits.quota_kg,
            used_kg=self._used_kg,
            switch_mode=self._limits.switch_mode,
            faulted=self._faulted,
        )
