"""双路气源增压充瓶顺序。

在原有"基线核对 → 压缩机启动 → 升压 → 瓶组缓存"之前，先按两路气源的实测
压力与剩余配额算一份确定性配额计划，再按计划逐路抽气：

* 先抽计划 ``first`` 指向的一路，各抽多少按计划分配值封顶；
* 一路掉到自身下限（或配额用尽）即按**离开侧**声明的切换方式切到另一路：
  ``ramp`` 先把流量缓冲到喘振下限，``immediate`` 立即切；
* 切换走"先开新路再关旧路"的重叠时序，重叠期与稳定期照常推进压缩机升压，
  出口压力只升不降，不断档；
* 新路开阀失败属于部分失败：回退原路、恢复缓冲流量，本次充瓶记违约停机，
  已抽气量全部保留入账；
* 两路配额用尽即正常停机，报告给出缺口与停机原因，不算违约。
"""

from __future__ import annotations

from ..buffer.baseline import BufferBaseline
from ..clock import Clock, ManualClock
from ..comp.surge import SurgeGuard
from ..config import RAMP_SWITCH, SUPPLY_KEYS, SupplyLimits
from ..errors import HydroError
from ..supply.manifold import SupplyManifold
from ..supply.plan import QuotaPlan
from ..supply.switch_record import SWITCH_ROLLED_BACK

#: 正常结束：需求气量已抽够。
STOP_FULFILLED = "fulfilled"
#: 正常结束：两路配额用尽仍有缺口。
STOP_QUOTA_EXHAUSTED = "quota_exhausted"
#: 异常结束：切换失败回退原路，本次充瓶终止。
STOP_SWITCH_FAILED = "switch_failed"

_EPS = 1e-9


def _exhausted(manifold: SupplyManifold, key: str) -> bool:
    source = manifold.source(key)
    return (not source.above_low()) or source.remaining_kg() <= _EPS


def _next_target(manifold: SupplyManifold, current: str | None) -> str | None:
    """挑另一路仍可抽气的气源；都不可用时返回 None。"""

    for key in SUPPLY_KEYS:
        if key != current and manifold.source(key).available():
            return key
    return None


def _buffer_down(manifold: SupplyManifold, limits: SupplyLimits, clock: Clock) -> list[dict]:
    """离开侧要求缓冲时，把流量压到喘振下限。"""

    steps: list[dict] = []
    floor = manifold.ramp.floor_kg_per_min
    while manifold.ramp.flow_kg_per_min > floor + _EPS:
        flow = manifold.ramp.ramp_down(limits.ramp_dt_s)
        clock.sleep(limits.ramp_dt_s)
        steps.append(
            {
                "component": "supply",
                "action": "flow_buffer",
                "detail": {"rate_kg_per_min": round(flow, 3), "phase": "down"},
            }
        )
    return steps


def _buffer_up(manifold: SupplyManifold, limits: SupplyLimits, clock: Clock, target_flow: float) -> list[dict]:
    """切换成功后把缓冲流量恢复回原流量。"""

    steps: list[dict] = []
    while manifold.ramp.flow_kg_per_min < target_flow - _EPS:
        flow = manifold.ramp.ramp_up(target_flow, limits.ramp_dt_s)
        clock.sleep(limits.ramp_dt_s)
        steps.append(
            {
                "component": "supply",
                "action": "flow_buffer",
                "detail": {"rate_kg_per_min": round(flow, 3), "phase": "up"},
            }
        )
    manifold.set_rate(target_flow)
    return steps


def charge_from_supply(
    manifold: SupplyManifold,
    compressor: Compressor,
    bank: BufferBank,
    baseline: BufferBaseline,
    surge: SurgeGuard,
    station: str,
    now: float = 0.0,
    demand_kg: float = 10.0,
    flow_kg_per_min: float = 4.0,
    dt: float = 1.0,
    clock: Clock | None = None,
    max_steps: int = 100_000,
) -> dict:
    """执行一次双路气源增压充瓶，返回带计划、切换时序与违约项的报告。"""

    clock = clock or ManualClock(start=float(now))
    steps: list[dict] = []
    violations: list[str] = []
    switches: list[dict] = []
    plan: QuotaPlan | None = None
    stop_reason = STOP_FULFILLED
    # 台账是累计的，本次交付以"操作后用量 − 操作前用量"为准，异常路径也对得上。
    used_before = {key: manifold.used_kg(key) for key in SUPPLY_KEYS}

    def delivered_total() -> float:
        return sum(manifold.used_kg(key) - used_before[key] for key in SUPPLY_KEYS)

    check = baseline.check(bank.pressure_mpa, now)
    steps.append({"component": "buffer", "action": "baseline_check", "detail": check})
    if not check["ok"]:
        violations.append("buffer_baseline_expired" if check["expired"] else "buffer_over_target")
        return _report(station, steps, violations, switches, plan, delivered_total(), demand_kg, STOP_QUOTA_EXHAUSTED)

    try:
        plan = manifold.plan(demand_kg)
        steps.append({"component": "supply", "action": "quota_plan", "detail": {"plan": plan.as_dict()}})
        first = plan.first()
        if first is None:
            violations.append("supply_no_source_available")
            return _report(
                station, steps, violations, switches, plan, delivered_total(), demand_kg, STOP_QUOTA_EXHAUSTED
            )

        inlet = compressor.start(manifold.source(first).pressure_mpa)
        steps.append(
            {"component": "comp", "action": "start", "detail": {"inlet_mpa": round(inlet, 3), "source": first}}
        )
        manifold.engage(first)
        manifold.set_rate(flow_kg_per_min)
        steps.append({"component": "supply", "action": "engage", "detail": {"source": first}})

        delivered = 0.0
        current = first
        guard = 0
        while delivered < demand_kg - _EPS and guard < max_steps:
            guard += 1
            # 计划决定抽气顺序；逐路实际上限取该路剩余配额与剩余需求的较小值。
            # 计划分配行按"前序理想情况"估算，前序提前到下限时，后序需要补足。
            cap = min(manifold.source(current).remaining_kg(), demand_kg - delivered)
            batch = min(manifold.ramp.flow_kg_per_min * dt / 60.0, cap)
            if batch > _EPS:
                source_pressure = manifold.draw(batch)
                delivered += batch
                if not compressor.at_target():
                    compressor.ramp(dt)
                clock.sleep(dt)
                steps.append(
                    {
                        "component": "supply",
                        "action": "draw",
                        "detail": {
                            "source": current,
                            "batch_kg": round(batch, 3),
                            "source_pressure_mpa": round(source_pressure, 3),
                            "delivered_kg": round(delivered, 3),
                        },
                    }
                )

            if delivered >= demand_kg - _EPS:
                break
            if not _exhausted(manifold, current):
                continue

            # 当前路到下限或配额尽，准备切到另一路。
            reason = "low_pressure" if not manifold.source(current).above_low() else "quota_exhausted"
            mode = manifold.source(current).limits.switch_mode
            buffered = False
            if mode == RAMP_SWITCH:
                steps.extend(_buffer_down(manifold, manifold.limits, clock))
                buffered = manifold.ramp.flow_kg_per_min <= manifold.ramp.floor_kg_per_min + _EPS

            target = _next_target(manifold, current)
            outlet_before = compressor.pressure_mpa
            if target is None:
                # 无机可切也要把重叠+稳定时序走完，压缩机照常升压，压力不断档。
                span = manifold.limits.overlap_s + manifold.limits.settle_s
                clock.sleep(span)
                if not compressor.at_target():
                    compressor.ramp(span)
                stop_reason = STOP_QUOTA_EXHAUSTED
                steps.append(
                    {
                        "component": "supply",
                        "action": "switch_unavailable",
                        "detail": {"source_from": current, "reason": reason},
                    }
                )
                break

            # 重叠期：新路开启、旧路仍在供气；压缩机继续升压。
            clock.sleep(manifold.limits.overlap_s)
            if not compressor.at_target():
                compressor.ramp(manifold.limits.overlap_s)
            outlet_during = compressor.pressure_mpa
            # 稳定期：新路压力建立后再关旧路。
            clock.sleep(manifold.limits.settle_s)
            if not compressor.at_target():
                compressor.ramp(manifold.limits.settle_s)
            outlet_after = compressor.pressure_mpa

            # switch_to 内"先开新路再关旧路"；开阀失败即回退，活动路不变。
            record = manifold.switch_to(
                target,
                clock.now(),
                outlet_before_mpa=outlet_before,
                outlet_during_mpa=outlet_during,
                outlet_after_mpa=outlet_after,
                buffered=buffered,
                pre_switch_rate_kg_per_min=flow_kg_per_min,
                reason=reason,
            )
            switches.append(record.as_dict())
            steps.append({"component": "supply", "action": "supply_switch", "detail": record.as_dict()})

            if record.result == SWITCH_ROLLED_BACK:
                # 部分失败：新路没打开，旧路保持开启；恢复缓冲流量后终止本次充瓶。
                if mode == RAMP_SWITCH:
                    steps.extend(_buffer_up(manifold, manifold.limits, clock, flow_kg_per_min))
                violations.append("supply_switch_failed")
                stop_reason = STOP_SWITCH_FAILED
                break

            if mode == RAMP_SWITCH:
                steps.extend(_buffer_up(manifold, manifold.limits, clock, flow_kg_per_min))
            else:
                manifold.set_rate(flow_kg_per_min)
            current = target

        surge_result = surge.evaluate(flow_kg_per_min)
        if surge_result["surge"]:
            surge.trip()
            violations.append("compressor_surge")
        steps.append({"component": "comp", "action": "surge_check", "detail": surge_result})

        bank.fill(compressor.pressure_mpa, delivered_total())
        steps.append(
            {
                "component": "buffer",
                "action": "cache",
                "detail": {
                    "pressure_mpa": round(bank.pressure_mpa, 3),
                    "stored_kg": round(bank.stored_kg, 3),
                    "charged_kg": round(delivered_total(), 3),
                },
            }
        )
        manifold.release()
        pressure = compressor.stop()
        steps.append({"component": "comp", "action": "stop", "detail": {"pressure_mpa": round(pressure, 3)}})
    except HydroError as error:
        payload = error.as_record()
        violations.append(f"{payload['component']}:{payload['type']}")
        steps.append(
            {"component": payload["component"], "action": "rejected", "detail": {"message": payload["message"]}}
        )

    return _report(
        station,
        steps,
        violations,
        switches,
        plan,
        delivered_total(),
        demand_kg,
        stop_reason,
    )


def _report(
    station: str,
    steps: list[dict],
    violations: list[str],
    switches: list[dict],
    plan: QuotaPlan | None,
    delivered_kg: float,
    demand_kg: float,
    stop_reason: str,
) -> dict:
    shortfall = max(0.0, demand_kg - delivered_kg)
    return {
        "station": station,
        "ok": not violations,
        "steps": steps,
        "violations": violations,
        "plan": plan.as_dict() if plan is not None else None,
        "switches": switches,
        "delivered_kg": round(delivered_kg, 3),
        "demand_kg": round(demand_kg, 3),
        "shortfall_kg": round(shortfall, 3),
        "stop_reason": stop_reason,
        "first_source": plan.first() if plan is not None else None,
    }
