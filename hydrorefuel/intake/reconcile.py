"""取气台账与审计流水核对。

控制台展示的两路配额、实抽量与切换次数，必须能和落盘审计逐条对上。核对只
信三类由平台显式发布的事件（全部带 ``epoch`` 配额纪元）：

``intake_plan``    一次增压取气开始：先抽哪路、各路计划抽多少；
``intake_settle``  一次增压取气结算：各路实抽、剩余配额、切换与回滚次数；
``intake_switch``  一次切换提交：成功或失败回滚都要记。

同一个纪元内：切换台账成功／失败条数必须等于各次结算登记的次数之和；各路
实抽量按结算累加必须等于选择器台账；每个操作的开始与结算必须一一对应且先
后有序。纪元复位（``intake_reset``）之后的旧台账不参与本纪元核对。
"""

from __future__ import annotations

from .selector import IntakeSelector

PLAN = "intake_plan"
SETTLE = "intake_settle"
SWITCH = "intake_switch"
RESET = "intake_reset"


def _events(records: list[dict]) -> list[dict]:
    events = []
    for record in records:
        if record.get("component") != "intake":
            continue
        events.append({"action": record.get("action", ""), "station": record.get("station", ""), **record.get("payload", {})})
    return events


def reconcile(selector: IntakeSelector, records: list[dict], station: str = "") -> dict:
    """把审计流水与选择器台账逐项核对，返回差异清单；无差异即一致。"""

    epoch = selector.epoch
    scoped = [
        event
        for event in _events(records)
        if int(event.get("epoch", -1)) == epoch and (not station or event["station"] == station)
    ]

    plans = [event for event in scoped if event["action"] == PLAN]
    settles = [event for event in scoped if event["action"] == SETTLE]
    switches = [event for event in scoped if event["action"] == SWITCH]
    resets = [event for event in scoped if event["action"] == RESET]
    successful = [event for event in switches if not event.get("rolled_back")]
    rolled_back = [event for event in switches if event.get("rolled_back")]

    differences: list[str] = []

    sum_switches = sum(int(event.get("switches_this_op", 0)) for event in settles)
    sum_failures = sum(int(event.get("failures_this_op", 0)) for event in settles)
    if sum_switches != len(successful):
        differences.append(
            f"成功切换次数不一致：结算合计 {sum_switches}，审计切换台账 {len(successful)}"
        )
    if sum_failures != len(rolled_back):
        differences.append(
            f"失败回滚次数不一致：结算合计 {sum_failures}，审计回滚台账 {len(rolled_back)}"
        )

    plan_ids = [event.get("op_id") for event in plans]
    settle_ids = [event.get("op_id") for event in settles]
    if len(plan_ids) != len(set(plan_ids)):
        differences.append("存在重复的取气开始事件")
    if len(settle_ids) != len(set(settle_ids)):
        differences.append("存在重复的取气结算事件")
    if set(settle_ids) - set(plan_ids):
        differences.append("存在没有开始记录的结算事件")
    # 结算顺序必须与开始顺序一致（同 op_id 集合，按出现次序比对）。
    settled_plans = [op_id for op_id in plan_ids if op_id in set(settle_ids)]
    if settled_plans != settle_ids:
        differences.append("取气开始与结算顺序不一致")

    drawn_audit = {source_id: 0.0 for source_id in selector.source_snapshot()}
    for event in settles:
        remaining = event.get("remaining_quota_kg")
        opening = event.get("opening_quota_kg")
        if isinstance(remaining, dict) and isinstance(opening, dict):
            per_remaining, per_opening = remaining, opening
        else:
            # 审计事件按标量字段落盘，避免嵌套结构进流水。
            per_remaining = {
                "truck": float(event.get("truck_remaining_kg", 0.0)),
                "bank": float(event.get("bank_remaining_kg", 0.0)),
            }
            per_opening = {
                "truck": float(event.get("truck_opening_kg", per_remaining["truck"])),
                "bank": float(event.get("bank_opening_kg", per_remaining["bank"])),
            }
        # 每次操作：开始前配额 − 结算后剩余 = 本次实抽；各次求和即纪元总量。
        # 配额若在两次操作之间被控制台调整，下一次操作的"开始前配额"会带着
        # 新值进入，调整量因此不会被错算成实抽。
        for source_id in drawn_audit:
            drawn_audit[source_id] += float(per_opening.get(source_id, 0.0)) - float(
                per_remaining.get(source_id, 0.0)
            )
    for source_id in drawn_audit:
        drawn_audit[source_id] = round(drawn_audit[source_id], 3)

    ledger = selector.snapshot().as_dict()["sources"]
    for source_id, audit_kg in drawn_audit.items():
        audit_kg = round(audit_kg, 3)
        drawn_audit[source_id] = audit_kg
        ledger_kg = round(float(ledger.get(source_id, {}).get("drawn_kg", 0.0)), 3)
        if abs(audit_kg - ledger_kg) > 0.01:
            differences.append(
                f"气源 {source_id} 实抽量不一致：审计结算合计 {audit_kg} kg，台账 {ledger_kg} kg"
            )

    if settles:
        last = settles[-1]
        if int(last.get("switch_count", -1)) != selector.switch_count:
            differences.append("最近结算的累计切换次数与选择器台账不符")
        if int(last.get("failed_switches", -1)) != selector.failed_switches:
            differences.append("最近结算的累计回滚次数与选择器台账不符")

    return {
        "ok": not differences,
        "epoch": epoch,
        "station": station,
        "plans": len(plans),
        "settles": len(settles),
        "switch_records": len(switches),
        "rolled_back": len(rolled_back),
        "resets": len(resets),
        "drawn_kg_audit": drawn_audit,
        "drawn_kg_ledger": {source_id: ledger.get(source_id, {}).get("drawn_kg", 0.0) for source_id in drawn_audit},
        "differences": differences,
    }
