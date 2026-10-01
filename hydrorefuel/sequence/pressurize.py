"""增压充瓶顺序：目标瓶组核对 → 取气配额计划 → 压缩机启动 → 双路取气升压 → 缓存。

压缩机启动前必须先核对目标瓶组压力基线：基线过期或瓶组已越过目标带上限时
直接拒绝启动。启动后取气不再只认一路：:class:`IntakeSelector` 按 **压力
优先** 在槽车与瓶组两路间分配配额，活动路掉到取气下限或配额用尽即按目标
气源配置的时序切换（槽车一步叠接、瓶组分相序缓冲）。切换期间压缩机不停机、
出口压力继续爬升，入口流量靠"先开新路再关旧路"保持不断档。

切换失败属于部分失败：选择器把压力与配额回滚到切换前、活动路回到原路，编排
记 ``intake_switch_failed`` 违约并结束本次取气，已抽气量照常落账。两路配额
都用尽即按 ``allocation_exhausted`` 正常收口，不超抽、不硬抽。
"""

from __future__ import annotations

from ..buffer.bank import BufferBank
from ..buffer.baseline import BufferBaseline
from ..comp.machine import Compressor
from ..comp.surge import SurgeGuard
from ..errors import HydroError
from ..intake import (
    MAKE_BEFORE_BREAK,
    OVERLAP,
    RAMP_DOWN,
    REASON_CUTOFF,
    REASON_QUOTA,
    STOP_QUOTA,
    STOP_ROLLBACK,
    STOP_SOURCES,
    TRANSFER,
    IntakeSelector,
)

PHASE_LABELS = {
    "steady": "稳态取气",
    RAMP_DOWN: "降流量缓冲",
    OVERLAP: "双路叠接",
    TRANSFER: "新路升流量",
    MAKE_BEFORE_BREAK: "先开后关叠接",
}


def pressurize_buffer(
    compressor: Compressor,
    bank: BufferBank,
    baseline: BufferBaseline,
    surge: SurgeGuard,
    intake: IntakeSelector,
    station: str,
    now: float = 0.0,
    inlet_pressure_mpa: float = 12.0,
    bank_pressure_mpa: float | None = None,
    flow_kg_per_min: float = 4.0,
    charge_kg: float = 5.0,
    dt: float = 1.0,
    max_steps: int = 4000,
) -> dict:
    """执行一次按两路配额分配的增压充瓶，返回带步骤与违约项的报告。"""

    steps: list[dict] = []
    violations: list[str] = []
    plan: dict = {}
    settlement: dict = {}
    switch_records: list[dict] = []
    switches_this_op = 0
    failures_this_op = 0
    stop_reason = None

    def add_step(component: str, action: str, detail: dict) -> None:
        steps.append({"component": component, "action": action, "detail": detail})

    check = baseline.check(bank.pressure_mpa, now)
    add_step("buffer", "baseline_check", check)
    if not check["ok"]:
        violations.append("buffer_baseline_expired" if check["expired"] else "buffer_over_target")
        return _report(station, steps, violations, plan, settlement, switch_records, 0, 0, None)

    bank_inlet = inlet_pressure_mpa - 3.0 if bank_pressure_mpa is None else bank_pressure_mpa
    try:
        plan = intake.begin({"truck": inlet_pressure_mpa, "bank": bank_inlet})
        add_step("intake", "quota_plan", _scalar_plan(plan))
        if not plan["first_source"]:
            violations.append("intake_no_source_available")
            settlement = intake.settle("rejected", STOP_SOURCES)
            return _report(station, steps, violations, plan, settlement, switch_records, 0, 0, STOP_SOURCES)

        compressor.reset()
        inlet = compressor.start(plan["pressures_mpa"][plan["first_source"]])
        add_step(
            "comp",
            "start",
            {"inlet_mpa": round(inlet, 3), "source": plan["first_source"], "policy": plan["policy"]},
        )

        delivered = 0.0
        for _ in range(max_steps):
            if delivered + 1e-9 >= charge_kg:
                break
            # 每步只按剩余配额提需求：切换发起当步也要封顶，避免叠接相位多抽。
            remaining_charge = charge_kg - delivered
            capped_rate = min(flow_kg_per_min, remaining_charge * 60.0 / dt)
            take = intake.step(capped_rate, dt)
            step_mass = round(sum(take["draws"].values()), 6)
            step_mass = min(step_mass, round(remaining_charge, 6))
            if take["switch_record"]:
                record = take["switch_record"]
                switch_records.append(record)
                add_step(
                    "intake",
                    "source_switch",
                    _scalar_switch(record, take),
                )
                if record["rolled_back"]:
                    failures_this_op += 1
                    violations.append("intake_switch_failed")
                else:
                    switches_this_op += 1
            elif take["switching"]:
                add_step(
                    "intake",
                    "switch_phase",
                    {
                        "phase": take["phase"],
                        "phase_label": PHASE_LABELS.get(take["phase"], take["phase"]),
                        "step_in_switch": take["step_in_switch"],
                        "steps_total": take["steps_total"],
                        "truck_kg": take["draws"].get("truck", 0.0),
                        "bank_kg": take["draws"].get("bank", 0.0),
                        "effective_rate_kg_per_min": take["effective_rate_kg_per_min"],
                    },
                )
            # 切换期间压缩机照常升压：出口压力单调不降，入口流量靠叠接不断档。
            outlet_before = compressor.pressure_mpa
            compressor.ramp(dt)
            if compressor.pressure_mpa + 1e-9 < outlet_before:
                violations.append("intake_switch_outlet_droop")
            delivered += step_mass
            # 切换期间按怠速流量核对喘振；稳态按配置流量核对。
            eval_rate = take["effective_rate_kg_per_min"] if take["switching"] else flow_kg_per_min
            if eval_rate > 0:
                surge_result = surge.evaluate(eval_rate)
                if surge_result["surge"]:
                    surge.trip()
                    violations.append("compressor_surge")
            if take["stop_reason"]:
                stop_reason = take["stop_reason"]
                break
            if not take["switching"] and step_mass <= 0.0:
                stop_reason = STOP_SOURCES
                break
            if not take["switching"] and not intake.active_source:
                stop_reason = STOP_SOURCES
                break

        add_step(
            "comp",
            "ramp",
            {
                "pressure_mpa": round(compressor.pressure_mpa, 3),
                "target": round(compressor.limits.outlet_target_mpa, 3),
                "intake_kg": round(delivered, 3),
            },
        )
        if not compressor.at_target():
            violations.append("compressor_ramp_incomplete")

        # 结算取气配额，再缓存到瓶组；配额用尽是正常收口，欠量只告警不硬抽。
        outcome = "completed" if not violations else "stopped"
        settlement = intake.settle(outcome, stop_reason)
        add_step("intake", "quota_settle", _scalar_settle(settlement, switches_this_op, failures_this_op))
        if stop_reason in (STOP_QUOTA, STOP_SOURCES, STOP_ROLLBACK) and delivered + 1e-9 < charge_kg:
            violations.append("intake_quota_exhausted")

        cached = min(delivered, max(0.0, charge_kg))
        try:
            bank.fill(compressor.pressure_mpa, cached)
        except HydroError as error:
            payload = error.as_record()
            violations.append(f"{payload['component']}:{payload['type']}")
            add_step("buffer", "rejected", {"message": payload["message"]})
        add_step(
            "buffer",
            "cache",
            {"pressure_mpa": round(bank.pressure_mpa, 3), "stored_kg": round(bank.stored_kg, 3)},
        )
        compressor.stop()
        add_step("comp", "stop", {"pressure_mpa": round(compressor.pressure_mpa, 3)})
    except HydroError as error:
        payload = error.as_record()
        violations.append(f"{payload['component']}:{payload['type']}")
        add_step(payload["component"], "rejected", {"message": payload["message"]})
        try:
            settlement = intake.settle("aborted", stop_reason)
        except (HydroError, ValueError):
            settlement = {}

    return _report(
        station,
        steps,
        violations,
        plan,
        settlement,
        switch_records,
        switches_this_op,
        failures_this_op,
        stop_reason,
    )


def _scalar_plan(plan: dict) -> dict:
    """把配额计划摊平成标量字段，便于审计逐条落盘与核对。"""

    return {
        "policy": plan["policy"],
        "first_source": plan["first_source"],
        "order": ">".join(plan["order"]),
        "truck_planned_kg": plan["planned_kg"].get("truck", 0.0),
        "bank_planned_kg": plan["planned_kg"].get("bank", 0.0),
        "total_planned_kg": plan["total_planned_kg"],
        "truck_pressure_mpa": plan["pressures_mpa"].get("truck", 0.0),
        "bank_pressure_mpa": plan["pressures_mpa"].get("bank", 0.0),
    }


def _scalar_switch(record: dict, take: dict) -> dict:
    """把一次切换提交（含失败回滚）摊平成标量字段。"""

    return {
        "seq": record["seq"],
        "from_source": record["from"],
        "to_source": record["to"],
        "mode": record["mode"],
        "reason": record["reason"],
        "last_phase": record.get("last_phase", ""),
        "steps": record["steps"],
        "elapsed_s": record["elapsed_s"],
        "rolled_back": record["rolled_back"],
        "active_source": record.get("active_source", record["to"]),
        "truck_pressure_mpa": take["pressures_mpa"].get("truck", 0.0),
        "bank_pressure_mpa": take["pressures_mpa"].get("bank", 0.0),
    }


def _scalar_settle(settlement: dict, switches: int, failures: int) -> dict:
    return {
        "outcome": settlement["outcome"],
        "stop_reason": settlement.get("stop_reason") or "",
        "active_source": settlement.get("active_source", ""),
        "truck_drawn_kg": settlement["drawn_kg"].get("truck", 0.0),
        "bank_drawn_kg": settlement["drawn_kg"].get("bank", 0.0),
        "drawn_total_kg": settlement["drawn_total_kg"],
        "truck_remaining_kg": settlement["remaining_quota_kg"].get("truck", 0.0),
        "bank_remaining_kg": settlement["remaining_quota_kg"].get("bank", 0.0),
        "truck_opening_kg": settlement["opening_quota_kg"].get("truck", 0.0),
        "bank_opening_kg": settlement["opening_quota_kg"].get("bank", 0.0),
        "switches_this_op": switches,
        "failures_this_op": failures,
        "switch_count": settlement["switch_count"],
        "failed_switches": settlement["failed_switches"],
        "epoch": settlement["epoch"],
    }


def _report(
    station: str,
    steps: list[dict],
    violations: list[str],
    plan: dict,
    settlement: dict,
    switch_records: list[dict],
    switches: int,
    failures: int,
    stop_reason: str | None,
) -> dict:
    return {
        "station": station,
        "ok": not violations,
        "steps": steps,
        "violations": violations,
        "intake_plan": plan,
        "intake_settlement": settlement,
        "switch_records": [dict(record) for record in switch_records],
        "switches_this_op": switches,
        "failed_switches_this_op": failures,
        "stop_reason": stop_reason,
        "trigger_reasons": [REASON_CUTOFF, REASON_QUOTA],
    }
