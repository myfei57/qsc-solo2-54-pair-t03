"""双路气源汇流排。

:class:`SupplyManifold` 持有两路气源与当前活动路，提供四组原语：

1. **测量与台账**：``measure`` 接收入口实测压力，``used_kg`` / ``remaining``
   逐路记账，台账可落盘、可恢复、可重置；
2. **计划**：把当前压力与剩余配额交给 :func:`build_plan` 计算确定性配额；
3. **抽气**：只允许从活动路抽，抽出量同时折算该路压力下降；
4. **切换时序**：``switch_to`` 执行"重叠期先开新路 → 稳定期核对 → 再关旧
   路"，全程记录出口压力不断档；新路开阀失败即关闭新路、旧路保持开启，记
   一条 ``rolled_back`` 记录，不改变活动路。

缓冲流量的升降由编排按离开侧气源的切换方式决定，结果通过参数传入切换记录。
"""

from __future__ import annotations

from ..config import SUPPLY_KEYS, SupplyLimits
from ..errors import InterlockError
from .flow import FlowRamp
from .plan import QuotaPlan, build_plan
from .snapshot import SupplySnapshot
from .source import GasSource
from .switch_record import SWITCH_OK, SWITCH_ROLLED_BACK, SwitchRecord


class SupplyManifold:
    """两路气源的选择、记账与切换时序。"""

    def __init__(
        self,
        limits: SupplyLimits,
        min_flow_kg_per_min: float,
        ramp_step_kg_per_min: float = 1.0,
    ) -> None:
        self._limits = limits
        self._sources = {key: GasSource(limits.sources[key]) for key in SUPPLY_KEYS}
        self._active: str | None = None
        self._ramp = FlowRamp(min_flow_kg_per_min, ramp_step_kg_per_min)
        self._switches: list[SwitchRecord] = []
        self._switch_seq = 0
        self._last: str | None = None

    @property
    def limits(self) -> SupplyLimits:
        return self._limits

    @property
    def policy(self) -> str:
        return self._limits.quota_policy

    @property
    def active(self) -> str | None:
        return self._active

    @property
    def ramp(self) -> FlowRamp:
        return self._ramp

    def source(self, key: str) -> GasSource:
        """按固定名字取一路气源。"""

        if key not in self._sources:
            raise InterlockError(f"未知气源 {key!r}", component="supply")
        return self._sources[key]

    def sources(self) -> dict[str, GasSource]:
        """返回两路气源（固定位序）。"""

        return dict(self._sources)

    def switches(self) -> list[SwitchRecord]:
        """返回全部切换记录（含回退）。"""

        return list(self._switches)

    def measure(self, key: str, pressure_mpa: float) -> GasSource:
        """上报一路气源的入口实测压力。"""

        source = self.source(key)
        source.measure(pressure_mpa)
        return source

    def set_fault(self, key: str, faulted: bool) -> None:
        """注入或清除一路阀门故障（开阀失败演练用）。"""

        self.source(key).set_fault(faulted)

    def plan(self, demand_kg: float) -> QuotaPlan:
        """按当前实测压力与剩余配额生成确定性配额计划。"""

        return build_plan(self._sources, self._limits, demand_kg)

    def engage(self, key: str) -> None:
        """把活动路切到指定气源：只有尚无活动路时允许直接建立。"""

        if self._active is not None:
            raise InterlockError("已有活动气源，切换必须走 switch_to 时序", component="supply")
        source = self.source(key)
        if not source.valve_open:
            source.open_valve()
        self._active = key
        self._last = key

    def release(self) -> None:
        """关闭活动路并清空活动位（充瓶结束的正常收尾）。"""

        if self._active is None:
            return
        self._sources[self._active].close_valve()
        self._ramp.reset()
        self._active = None

    def draw(self, mass_kg: float) -> float:
        """从活动路抽出一段气量，返回抽出后的该路压力。"""

        if self._active is None:
            raise InterlockError("没有活动气源，不能抽气", component="supply")
        return self._sources[self._active].draw(mass_kg)

    def switch_to(
        self,
        target: str,
        now: float,
        *,
        outlet_before_mpa: float,
        outlet_during_mpa: float,
        outlet_after_mpa: float,
        buffered: bool,
        pre_switch_rate_kg_per_min: float,
        reason: str,
    ) -> SwitchRecord:
        """执行一次重叠切换；失败时回退原路并返回回退记录。

        时序：记录离开侧切换方式 → 开新路（重叠期两路同开）→ 关旧路（稳定期
        之后）。新路开阀抛错时，把新路留在关闭位、旧路保持开启，活动路不变。
        """

        if self._active is None:
            raise InterlockError("没有活动气源，无从切换", component="supply")
        if target == self._active:
            raise InterlockError("目标气源与当前活动路相同", component="supply")
        source_from = self._sources[self._active]
        source_to = self.source(target)
        self._switch_seq += 1
        seq = self._switch_seq

        try:
            source_to.open_valve()  # 重叠期：先开新路，旧路仍在供气，压力不断档。
        except InterlockError:
            record = SwitchRecord(
                seq=seq,
                ts=now,
                source_from=source_from.key,
                source_to=source_to.key,
                switch_mode=source_from.limits.switch_mode,
                buffered=buffered,
                pre_switch_rate_kg_per_min=pre_switch_rate_kg_per_min,
                overlap_s=self._limits.overlap_s,
                settle_s=self._limits.settle_s,
                outlet_before_mpa=outlet_before_mpa,
                outlet_during_mpa=outlet_during_mpa,
                outlet_after_mpa=outlet_before_mpa,
                result=SWITCH_ROLLED_BACK,
                reason=reason,
            )
            self._switches.append(record)
            return record

        source_from.close_valve()  # 稳定期之后再关旧路。
        self._active = target
        self._last = target
        record = SwitchRecord(
            seq=seq,
            ts=now,
            source_from=source_from.key,
            source_to=source_to.key,
            switch_mode=source_from.limits.switch_mode,
            buffered=buffered,
            pre_switch_rate_kg_per_min=pre_switch_rate_kg_per_min,
            overlap_s=self._limits.overlap_s,
            settle_s=self._limits.settle_s,
            outlet_before_mpa=outlet_before_mpa,
            outlet_during_mpa=outlet_during_mpa,
            outlet_after_mpa=outlet_after_mpa,
            result=SWITCH_OK,
            reason=reason,
        )
        self._switches.append(record)
        return record

    def used_kg(self, key: str | None = None) -> float:
        """返回单路或两路合计的已抽气量。"""

        if key is not None:
            return self.source(key).used_kg
        return sum(source.used_kg for source in self._sources.values())

    def remaining_kg(self, key: str | None = None) -> float:
        """返回单路或两路合计的剩余配额。"""

        if key is not None:
            return self.source(key).remaining_kg()
        return sum(source.remaining_kg() for source in self._sources.values())

    def set_rate(self, rate_kg_per_min: float) -> None:
        """直接设置当前抽气流量（建立活动路时的初值，不做缓冲）。"""

        self._ramp.set(rate_kg_per_min)

    def rate(self) -> float:
        """返回当前抽气流量。"""

        return self._ramp.flow_kg_per_min

    def ledger(self) -> dict:
        """导出需要落盘的台账。"""

        return {
            "active": self._active,
            "last": self._last,
            "sources": {key: self._sources[key].ledger() for key in SUPPLY_KEYS},
            "switches": [record.as_dict() for record in self._switches],
        }

    def restore(self, state: dict) -> None:
        """从落盘台账恢复用量、最后压力与切换记录（阀门回关闭安全位）。"""

        if not isinstance(state, dict):
            return
        sources_state = state.get("sources", {})
        for key in SUPPLY_KEYS:
            if isinstance(sources_state.get(key), dict):
                self._sources[key].restore(sources_state[key])
        self._active = None
        last = state.get("last")
        self._last = last if last in self._sources else None
        self._ramp.reset()
        self._switches = [_record_from_dict(item) for item in state.get("switches", [])]
        # 重放记录的序号紧接落盘末尾，保证重启后切换记录连续可核对。
        self._switch_seq = max((record.seq for record in self._switches), default=0)

    def reset(self) -> None:
        """清回初始状态：零用量、零切换记录、无活动路。"""

        for source in self._sources.values():
            source.reset()
        self._active = None
        self._last = None
        self._ramp.reset()
        self._switches.clear()
        self._switch_seq = 0

    def snapshot(self) -> SupplySnapshot:
        """返回双路气源快照。"""

        return SupplySnapshot(
            active=self._active,
            rate_kg_per_min=self._ramp.flow_kg_per_min,
            policy=self._limits.quota_policy,
            sources={key: self._sources[key].snapshot() for key in SUPPLY_KEYS},
        )


def _record_from_dict(raw: dict) -> SwitchRecord:
    """从落盘字段重建一条不可变切换记录。"""

    return SwitchRecord(
        seq=int(raw.get("seq", 0)),
        ts=float(raw.get("ts", 0.0)),
        source_from=str(raw.get("source_from", "")),
        source_to=str(raw.get("source_to", "")),
        switch_mode=str(raw.get("switch_mode", "")),
        buffered=bool(raw.get("buffered", False)),
        pre_switch_rate_kg_per_min=float(raw.get("pre_switch_rate_kg_per_min", 0.0)),
        overlap_s=float(raw.get("overlap_s", 0.0)),
        settle_s=float(raw.get("settle_s", 0.0)),
        outlet_before_mpa=float(raw.get("outlet_before_mpa", 0.0)),
        outlet_during_mpa=float(raw.get("outlet_during_mpa", 0.0)),
        outlet_after_mpa=float(raw.get("outlet_after_mpa", 0.0)),
        result=str(raw.get("result", SWITCH_OK)),
        reason=str(raw.get("reason", "")),
    )
