"""配额分配计划。

抽气前先把需求质量按定死的策略切成两路配额：

* ``pressure``（出厂定死）：实测压力高的一路先抽，抽到下限或配额尽再轮到
  下一路；压力相同时按固定位序 ``truck`` → ``cascade`` 打破平局。
* ``remaining``：剩余配额多的一路先抽，余量相同时仍按固定位序打破平局。

计划在抽气开始前一次性算清并落审计，执行期实抽量逐路记账，结算时用
"计划分配值 == 实抽值 + 缺口" 做核对。策略只在装配期由配置选定，本模块不
提供任何运行期改策略的入口。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import PRESSURE_FIRST, SUPPLY_KEYS, SupplyLimits


@dataclass(frozen=True)
class PlanLine:
    """一路气源的配额分配行。"""

    key: str
    order: int
    pressure_mpa: float
    available_kg: float
    allocated_kg: float

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "order": self.order,
            "pressure_mpa": round(self.pressure_mpa, 3),
            "available_kg": round(self.available_kg, 3),
            "allocated_kg": round(self.allocated_kg, 3),
        }


@dataclass(frozen=True)
class QuotaPlan:
    """一次充瓶的完整配额计划。"""

    policy: str
    demand_kg: float
    lines: tuple[PlanLine, ...] = field(default_factory=tuple)

    @property
    def allocated_kg(self) -> float:
        return sum(line.allocated_kg for line in self.lines)

    @property
    def shortfall_kg(self) -> float:
        """两路可分配总量相对需求的缺口。"""

        return max(0.0, self.demand_kg - self.allocated_kg)

    def first(self) -> str | None:
        """返回先抽的一路；两路都不可用时为 ``None``。"""

        ordered = sorted(self.lines, key=lambda line: line.order)
        return ordered[0].key if ordered else None

    def as_dict(self) -> dict:
        return {
            "policy": self.policy,
            "demand_kg": round(self.demand_kg, 3),
            "allocated_kg": round(self.allocated_kg, 3),
            "shortfall_kg": round(self.shortfall_kg, 3),
            "first": self.first(),
            "lines": [line.as_dict() for line in sorted(self.lines, key=lambda item: item.order)],
        }


def build_plan(sources: dict, limits: SupplyLimits, demand_kg: float) -> QuotaPlan:
    """按当前实测压力与剩余配额，把需求确定性地切成两路配额。

    ``sources`` 为 ``key -> GasSource`` 映射；只纳入实测压力高于下限且仍有
    配额的气源。故障标志不影响分配资格——故障要等到真正切换开阀时才暴露，
    这正是"切换失败回退原路"的演练路径。
    """

    demand = float(demand_kg)
    if demand <= 0:
        raise ValueError("配额需求必须为正")

    candidates = [sources[key] for key in SUPPLY_KEYS if key in sources and sources[key].available()]
    if limits.quota_policy == PRESSURE_FIRST:
        # 压力降序；压力相同按固定位序。
        candidates.sort(key=lambda item: (-item.pressure_mpa, SUPPLY_KEYS.index(item.key)))
    else:
        candidates.sort(key=lambda item: (-item.remaining_kg(), SUPPLY_KEYS.index(item.key)))

    remaining = demand
    lines: list[PlanLine] = []
    for order, source in enumerate(candidates):
        available = source.remaining_kg()
        take = min(available, remaining)
        lines.append(
            PlanLine(
                key=source.key,
                order=order,
                pressure_mpa=source.pressure_mpa,
                available_kg=available,
                allocated_kg=max(0.0, take),
            )
        )
        remaining -= take
    return QuotaPlan(policy=limits.quota_policy, demand_kg=demand, lines=tuple(lines))
