"""两路取气选择器：压力优先配额 + 分相序切换。

配额策略被定死为 **压力优先**（:data:`PRESSURE_FIRST`）：两路都可用时先抽
压力高的一路；活动路掉到取气下限或配额用尽时，按目标气源配置的方式切换到
另一路。切换有两种时序：

``immediate``  先开新路、两路叠接一个步长再关旧路，一步完成，适合槽车；
``buffered``  先把取气流量降到怠速（``ramp_down``），再开新路叠接顶上来
               （``overlap``），最后旧路关新路满流量（``transfer``），适合
               瓶组，切换期间压缩机入口始终有气，出口压力继续爬升。

切换是一次可回滚的事务：切换期间每个步长的压力下降与配额扣减先记账，提交
失败（现场阀动作失败由 ``arm_switch_failure`` 注入）就整体回滚到切换前，
活动路保持原路，记一次失败切换——这就是"部分失败回到原路"。
"""

from __future__ import annotations

from ..config import INTAKE_SOURCES, IntakeLimits, PRESSURE_FIRST
from ..errors import InterlockError
from .snapshot import IntakeSnapshot

STEADY = "steady"
RAMP_DOWN = "ramp_down"
OVERLAP = "overlap"
MAKE_BEFORE_BREAK = "make_before_break"
TRANSFER = "transfer"

REASON_CUTOFF = "cutoff"
REASON_QUOTA = "quota"
STOP_QUOTA = "allocation_exhausted"
STOP_SOURCES = "sources_exhausted"
STOP_ROLLBACK = "switch_rolled_back"

_EPS = 1e-9


class _Source:
    """单路气源的活动状态。"""

    def __init__(self, limits) -> None:
        self.limits = limits
        self.pressure_mpa = 0.0
        self.drawn_kg = 0.0
        self.quota_kg = limits.quota_kg

    def available(self) -> bool:
        """压力高于下限且配额有余量，才允许被选中。"""

        return self.pressure_mpa > self.limits.cutoff_mpa + _EPS and self.quota_kg > _EPS

    def headroom_kg(self) -> float:
        """按当前压力与折算系数估算还能抽多少公斤才到下限。"""

        pressure_room = max(0.0, self.pressure_mpa - self.limits.cutoff_mpa)
        return min(self.quota_kg, pressure_room / self.limits.depletion_mpa_per_kg)

    def draw_capacity_kg(self) -> float:
        """当前压力与配额下本路最多还能出多少气（不会把压力抽到下限以下）。"""

        pressure_cap = max(0.0, (self.pressure_mpa - self.limits.cutoff_mpa) / self.limits.depletion_mpa_per_kg)
        return min(self.quota_kg, pressure_cap)

    def draw(self, mass_kg: float) -> None:
        """扣减一段气量，压力按折算系数同步下降，配额同步记账。"""

        if mass_kg <= 0.0:
            return
        mass = min(mass_kg, self.draw_capacity_kg())
        self.pressure_mpa = max(self.limits.cutoff_mpa, self.pressure_mpa - mass * self.limits.depletion_mpa_per_kg)
        self.quota_kg -= mass
        self.drawn_kg += mass


class IntakeSelector:
    """按压力优先在槽车与瓶组两路之间分配取气配额。"""

    def __init__(self, limits: IntakeLimits) -> None:
        self._limits = limits
        self._sources = {source_id: _Source(limits.source(source_id)) for source_id in INTAKE_SOURCES}
        self._active = ""
        self._switching: dict | None = None
        self._drawn_total_kg = 0.0
        self._switch_count = 0
        self._failed_switches = 0
        self._fail_next = False
        self._failed_targets: set[str] = set()
        self._records: list[dict] = []
        self._epoch = 0
        self._running = False
        self._opening_quota = {source_id: self._sources[source_id].quota_kg for source_id in INTAKE_SOURCES}

    @property
    def active_source(self) -> str:
        return self._active

    @property
    def switching(self) -> bool:
        return self._switching is not None

    @property
    def switch_count(self) -> int:
        return self._switch_count

    @property
    def failed_switches(self) -> int:
        return self._failed_switches

    @property
    def epoch(self) -> int:
        return self._epoch

    def pressure_mpa(self, source_id: str | None = None) -> float:
        """返回某路（默认活动路）的当前压力。"""

        return self._sources[source_id or self._active].pressure_mpa

    def remaining_quota_kg(self, source_id: str) -> float:
        """返回某路剩余配额。"""

        return self._sources[source_id].quota_kg

    def drawn_kg(self, source_id: str) -> float:
        """返回某路累计抽取量。"""

        return self._sources[source_id].drawn_kg

    def drawn_total_kg(self) -> float:
        """返回两路累计抽取总量。"""

        return self._drawn_total_kg

    def switch_records(self) -> list[dict]:
        """返回切换台账（含失败回滚记录）。"""

        return [dict(record) for record in self._records]

    def begin(self, pressures: dict[str, float]) -> dict:
        """开始一次增压取气：登记两路压力并按压力优先选出首抽路，返回配额计划。"""

        for source_id in INTAKE_SOURCES:
            if source_id not in pressures:
                raise ValueError(f"缺少气源 {source_id} 的压力")
            pressure = float(pressures[source_id])
            if pressure < 0.0:
                raise ValueError("气源压力不能为负")
            self._sources[source_id].pressure_mpa = pressure
        self._switching = None
        self._failed_targets = set()
        self._running = True
        self._active = self._pick(prefer="")
        self._opening_quota = {source_id: self._sources[source_id].quota_kg for source_id in INTAKE_SOURCES}
        return self.plan()

    def plan(self) -> dict:
        """按压力优先给出配额计划：先抽哪路、各路计划抽多少。"""

        order = self._pressure_order()
        planned = {source_id: round(self._sources[source_id].headroom_kg(), 3) for source_id in INTAKE_SOURCES}
        return {
            "policy": PRESSURE_FIRST,
            "first_source": self._active,
            "order": order,
            "planned_kg": planned,
            "total_planned_kg": round(sum(planned.values()), 3),
            "pressures_mpa": {source_id: round(self._sources[source_id].pressure_mpa, 3) for source_id in INTAKE_SOURCES},
            "available": [source_id for source_id in order if self._sources[source_id].available()],
        }

    def _pressure_order(self) -> list[str]:
        """压力从高到低排序，压力相同按固定路名排序保证结论唯一。"""

        return sorted(INTAKE_SOURCES, key=lambda item: (-self._sources[item].pressure_mpa, item))

    def _pick(self, prefer: str) -> str:
        """在可用气源里挑压力最高的一路；``prefer`` 可用时优先保留原路。"""

        if prefer and self._sources[prefer].available() and prefer not in self._failed_targets:
            return prefer
        candidates = [
            source_id
            for source_id in self._pressure_order()
            if self._sources[source_id].available() and source_id not in self._failed_targets
        ]
        return candidates[0] if candidates else ""

    def step(self, rate_kg_per_min: float, dt: float) -> dict:
        """推进一个取气步长，返回本步长的气源分配与切换相位。"""

        if not self._running:
            raise ValueError("取气计划尚未开始或已结算")
        if dt <= 0:
            raise ValueError("步长必须为正")
        demand_kg = max(0.0, float(rate_kg_per_min)) * dt / 60.0
        if self._switching is not None:
            return self._advance_switch(dt, demand_kg)
        return self._steady_step(dt, demand_kg, float(rate_kg_per_min))

    def _steady_step(self, dt: float, demand_kg: float, rate: float) -> dict:
        draws = {source_id: 0.0 for source_id in INTAKE_SOURCES}
        steady_kg = 0.0
        remaining = demand_kg
        initiated = False
        if self._active and self._sources[self._active].available():
            source = self._sources[self._active]
            capacity = source.draw_capacity_kg()
            if capacity + _EPS >= demand_kg:
                source.draw(demand_kg)
                draws[self._active] += demand_kg
                steady_kg = demand_kg
                remaining = 0.0
            else:
                # 活动路本步长喂不饱：先抽到下限／配额见底，同一步立即发起切换
                # 并推进首个相位（先开新路），入口流量在本步长内就不断档。
                source.draw(capacity)
                draws[self._active] += capacity
                steady_kg = capacity
                remaining = max(0.0, demand_kg - capacity)
                self._start_switch(rate)
                initiated = self.switching
        else:
            self._start_switch(rate)
            initiated = self.switching
        self._drawn_total_kg += steady_kg
        if initiated:
            # 相位推进内部已自行累计抽取量，这里只合并展示口径。
            take = self._advance_switch(dt, remaining)
            for source_id, mass in take["draws"].items():
                draws[source_id] = draws.get(source_id, 0.0) + mass
            take["draws"] = {key: round(value, 6) for key, value in draws.items()}
            return take
        stop_reason = self._terminal_reason()
        return {
            "switching": False,
            "phase": STEADY,
            "step_in_switch": 0,
            "draws": {key: round(value, 6) for key, value in draws.items()},
            "effective_rate_kg_per_min": round(rate if self._active else 0.0, 3),
            "switch_completed": False,
            "switch_record": None,
            "stop_reason": stop_reason,
            "pressures_mpa": self._pressures(),
        }

    def _terminal_reason(self) -> str | None:
        """稳态步长结束后判断是否已无气可抽：配额全尽按配额收口，否则按气源见底。"""

        if any(self._sources[source_id].available() for source_id in INTAKE_SOURCES):
            return None
        return STOP_QUOTA if self._total_allocation_gone() else STOP_SOURCES

    def _total_allocation_gone(self) -> bool:
        return all(self._sources[source_id].quota_kg <= _EPS for source_id in INTAKE_SOURCES)

    def _start_switch(self, rate: float) -> None:
        active = self._active
        target = self._pick(prefer="")
        if not target or target == active:
            self._switching = None
            return
        # 触发原因按活动路见底方式判定：压力先到下限算压力触发，否则算配额用尽。
        # 切换方式按来路（正在离开的活动路）配置：它决定"要不要先降流量缓冲"。
        old = self._sources[active] if active else None
        pressure_gone = bool(old) and old.pressure_mpa <= old.limits.cutoff_mpa + _EPS
        reason = REASON_CUTOFF if pressure_gone else REASON_QUOTA
        mode = self._sources[active].limits.switch_mode if active else self._sources[target].limits.switch_mode
        phases = self._build_phases(mode, rate, reason)
        snapshot = self._snapshot_state()
        self._switching = {
            "from": active,
            "to": target,
            "mode": mode,
            "reason": reason,
            "phases": phases,
            "phase_index": 0,
            "step_index": 0,
            "steps_total": len(phases),
            "snapshot": snapshot,
            "phase_draws": [],
        }

    def _build_phases(self, mode: str, rate: float, reason: str) -> list[tuple[str, float, float]]:
        """构造切换相位序列（每元素一个步长）：(相位名, 旧路计划份额, 新路计划份额)。"""

        n = max(1, self._limits.switch_steps)
        if mode == "immediate":
            # 先开新路再关旧路：单个叠接步长两路各承担一半，入口流量不断档。
            return [(MAKE_BEFORE_BREAK, 0.5, 0.5)]
        # 压力触发：先把流量降到怠速（旧路独自承担怠速量），叠接相新路逐步顶上，
        # 最后旧路关闭新路满流量——正在升的出口压力不被打乱。
        # 配额触发：旧路配额已尽、无气可供，降流量相直接由新路全量顶上；新路自
        # 第一步即已打开（先开后关），入口流量同样不断档，只是没有旧路缓冲段。
        if reason == REASON_QUOTA:
            return [(RAMP_DOWN, 0.0, 1.0)] + [
                (OVERLAP, 0.0, 1.0) for _ in range(n)
            ] + [(TRANSFER, 0.0, 1.0)]
        idle_ratio = self._limits.idle_fraction(rate)
        ramp = [(RAMP_DOWN, idle_ratio, 0.0)]
        overlap = [
            (OVERLAP, idle_ratio, round((1.0 - idle_ratio) * (i + 1) / n, 6))
            for i in range(n)
        ]
        return ramp + overlap + [(TRANSFER, 0.0, 1.0)]

    def _advance_switch(self, dt: float, demand_kg: float) -> dict:
        switch = self._switching
        assert switch is not None
        phase_name, old_share, new_share = switch["phases"][switch["phase_index"]]
        old_id, new_id = switch["from"], switch["to"]
        old, new = self._sources[old_id], self._sources[new_id]
        draws = {source_id: 0.0 for source_id in INTAKE_SOURCES}
        # 旧路先按计划份额出，出不动（已到下限/配额见底）的部分由新路顶上：
        # 新路自第一步就已打开（先开后关），因此入口流量始终不断档。
        old_wanted = demand_kg * old_share
        old_got = min(old_wanted, max(0.0, old.draw_capacity_kg()))
        new_wanted = demand_kg - old_got
        new_got = min(max(0.0, new_wanted), max(0.0, new.draw_capacity_kg()))
        old.draw(old_got)
        new.draw(new_got)
        draws[old_id] += old_got
        draws[new_id] += new_got
        supplied = old_got + new_got
        switch["phase_draws"].append(
            {
                "phase": phase_name,
                "old_share": old_share,
                "new_share": new_share,
                "supplied_kg": round(supplied, 6),
            }
        )
        switch["step_index"] += 1
        switch["phase_index"] += 1
        self._drawn_total_kg += supplied
        completed = switch["phase_index"] >= len(switch["phases"])
        record = None
        stop_reason = None
        if completed:
            record = self._finalize_switch(switch, dt)
            if record["rolled_back"] and not self._sources[record["active_source"]].available():
                # 部分失败回到原路，而原路也已见底：没有可回的路，只能收口。
                stop_reason = STOP_ROLLBACK
        return {
            "switching": not completed,
            "phase": phase_name,
            "step_in_switch": switch["step_index"],
            "from": old_id,
            "to": new_id,
            "mode": switch["mode"],
            "reason": switch["reason"],
            "steps_total": switch["steps_total"],
            "draws": {key: round(value, 6) for key, value in draws.items()},
            "effective_rate_kg_per_min": round(supplied * 60.0 / dt, 3),
            "switch_completed": completed,
            "switch_record": record,
            "rolled_back": bool(record and record["rolled_back"]),
            "stop_reason": stop_reason,
            "pressures_mpa": self._pressures(),
        }

    def _finalize_switch(self, switch: dict, dt: float) -> dict:
        record = {
            "seq": self._switch_count + self._failed_switches + 1,
            "from": switch["from"],
            "to": switch["to"],
            "mode": switch["mode"],
            "reason": switch["reason"],
            "last_phase": switch["phases"][-1][0],
            "steps": switch["steps_total"],
            "elapsed_s": round(switch["steps_total"] * dt, 3),
            "phases": list(switch["phase_draws"]),
            "rolled_back": False,
        }
        if self._fail_next:
            self._fail_next = False
            self._failed_targets.add(switch["to"])
            self._restore_state(switch["snapshot"])
            record["rolled_back"] = True
            record["active_source"] = switch["from"]
            self._failed_switches += 1
            self._records.append(dict(record))
            self._switching = None
            return record
        self._switch_count += 1
        self._active = switch["to"]
        self._records.append(dict(record))
        self._switching = None
        return record

    def settle(self, outcome: str, stop_reason: str | None = None) -> dict:
        """结算一次增压取气，返回两路实抽量与配额余量（落审计用）。"""

        if not self._running:
            raise ValueError("取气计划已结算")
        self._running = False
        self._switching = None
        return {
            "outcome": outcome,
            "stop_reason": stop_reason,
            "active_source": self._active,
            "drawn_kg": {source_id: round(self._sources[source_id].drawn_kg, 3) for source_id in INTAKE_SOURCES},
            "drawn_total_kg": round(self._drawn_total_kg, 3),
            "remaining_quota_kg": {
                source_id: round(self._sources[source_id].quota_kg, 3) for source_id in INTAKE_SOURCES
            },
            "opening_quota_kg": {
                source_id: round(self._opening_quota.get(source_id, self._sources[source_id].quota_kg), 3)
                for source_id in INTAKE_SOURCES
            },
            "pressures_mpa": self._pressures(),
            "switch_count": self._switch_count,
            "failed_switches": self._failed_switches,
            "epoch": self._epoch,
        }

    def arm_switch_failure(self) -> None:
        """安排下一次切换提交失败（现场阀动作失败的演练注入点）。"""

        self._fail_next = True

    def set_quota(self, source_id: str, quota_kg: float) -> dict:
        """调整某路配额，返回调整后的配额计划口径（配额变更单独落审计）。"""

        if source_id not in self._sources:
            raise InterlockError(f"未知气源 {source_id}", component="intake")
        quota = float(quota_kg)
        if quota < 0.0:
            raise InterlockError("配额不能为负", component="intake")
        self._sources[source_id].quota_kg = quota
        return {
            "source": source_id,
            "quota_kg": round(quota, 3),
            "drawn_kg": round(self._sources[source_id].drawn_kg, 3),
        }

    def reset(self) -> int:
        """清空取气台账与配额，配额纪元加一（审计核对用它隔开新旧两代台账）。"""

        self._sources = {source_id: _Source(self._limits.source(source_id)) for source_id in INTAKE_SOURCES}
        self._active = ""
        self._switching = None
        self._drawn_total_kg = 0.0
        self._switch_count = 0
        self._failed_switches = 0
        self._fail_next = False
        self._failed_targets = set()
        self._records = []
        self._running = False
        self._epoch += 1
        return self._epoch

    def restore(
        self,
        epoch: int,
        quotas: dict[str, float],
        drawn: dict[str, float],
        switch_count: int = 0,
        failed_switches: int = 0,
    ) -> None:
        """按审计回放结果重建配额纪元、两路台账与切换累计（压力每次操作重报）。"""

        self.reset()
        self._epoch = int(epoch)
        for source_id in INTAKE_SOURCES:
            self._sources[source_id].quota_kg = float(quotas.get(source_id, 0.0))
            self._sources[source_id].drawn_kg = float(drawn.get(source_id, 0.0))
        self._drawn_total_kg = sum(self._sources[source_id].drawn_kg for source_id in INTAKE_SOURCES)
        self._switch_count = int(switch_count)
        self._failed_switches = int(failed_switches)

    def _snapshot_state(self) -> tuple:
        return (
            self._active,
            self._drawn_total_kg,
            self._switch_count,
            [(s.pressure_mpa, s.drawn_kg, s.quota_kg) for s in (self._sources[i] for i in INTAKE_SOURCES)],
        )

    def _restore_state(self, snapshot: tuple) -> None:
        active, drawn_total, switch_count, source_states = snapshot
        self._active = active
        self._drawn_total_kg = drawn_total
        self._switch_count = switch_count
        for source_id, (pressure, drawn, quota) in zip(INTAKE_SOURCES, source_states):
            source = self._sources[source_id]
            source.pressure_mpa = pressure
            source.drawn_kg = drawn
            source.quota_kg = quota

    def _pressures(self) -> dict:
        return {source_id: round(self._sources[source_id].pressure_mpa, 3) for source_id in INTAKE_SOURCES}

    def source_snapshot(self) -> dict:
        """两路气源的控制台展示口径：压力、配额、实抽与切换方式。"""

        return {
            source_id: {
                "pressure_mpa": round(source.pressure_mpa, 3),
                "cutoff_mpa": source.limits.cutoff_mpa,
                "quota_kg": round(source.quota_kg, 3),
                "drawn_kg": round(source.drawn_kg, 3),
                "remaining_kg": round(source.headroom_kg(), 3),
                "switch_mode": source.limits.switch_mode,
                "available": source.available(),
            }
            for source_id, source in self._sources.items()
        }

    def snapshot(self) -> IntakeSnapshot:
        """返回取气选择器快照。"""

        return IntakeSnapshot(
            policy=PRESSURE_FIRST,
            active_source=self._active,
            switching=self.switching,
            drawn_total_kg=self._drawn_total_kg,
            switch_count=self._switch_count,
            failed_switches=self._failed_switches,
            epoch=self._epoch,
            sources=self.source_snapshot(),
        )
