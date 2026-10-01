"""两路取气：压力优先配额、分相序切换、失败回滚与审计核对。"""

from __future__ import annotations

from hydrorefuel.config import default_config
from hydrorefuel.intake import IntakeSelector
from hydrorefuel.intake.selector import REASON_CUTOFF, REASON_QUOTA, STOP_QUOTA


def _switch(report):
    records = report["switch_records"]
    assert len(records) == 1
    return records[0]


def test_pressure_first_picks_higher_pressure_source(platform):
    platform.start()
    report = platform.pressurize(inlet_pressure_mpa=20.0, bank_pressure_mpa=8.0, charge_kg=10.0)
    assert report["ok"], report["violations"]
    plan = report["intake_plan"]
    assert plan["policy"] == "pressure_first"
    assert plan["first_source"] == "truck"
    assert plan["order"] == ["truck", "bank"]
    settle = report["intake_settlement"]
    assert settle["drawn_kg"]["bank"] == 0.0
    assert settle["drawn_kg"]["truck"] == 10.0


def test_lower_pressure_source_waits_its_turn(platform):
    platform.start()
    report = platform.pressurize(inlet_pressure_mpa=8.0, bank_pressure_mpa=20.0, charge_kg=10.0)
    assert report["ok"], report["violations"]
    assert report["intake_plan"]["first_source"] == "bank"
    assert report["intake_settlement"]["drawn_kg"]["truck"] == 0.0


def test_policy_is_locked_to_pressure_first():
    cfg = default_config()
    assert cfg.intake.allocation_policy == "pressure_first"
    selector = IntakeSelector(cfg.intake)
    snapshot = selector.snapshot().as_dict()
    assert snapshot["policy"] == "pressure_first"
    selector.begin({"truck": 10.0, "bank": 12.0})
    assert selector.active_source == "bank"


def test_quota_exhaustion_switches_and_does_not_overdraw(platform):
    platform.start()
    platform.set_intake_quota("truck", 4.0)
    report = platform.pressurize(inlet_pressure_mpa=12.0, bank_pressure_mpa=9.0, charge_kg=10.0)
    assert report["ok"], report["violations"]
    record = _switch(report)
    assert record["from"] == "truck" and record["to"] == "bank"
    # 离开槽车：立即（先开后关）切换，配额用尽触发。
    assert record["mode"] == "immediate"
    assert record["reason"] == REASON_QUOTA
    settle = report["intake_settlement"]
    assert settle["drawn_kg"]["truck"] == 4.0
    assert settle["drawn_kg"]["bank"] == 6.0
    assert abs(settle["drawn_total_kg"] - 10.0) < 1e-9


def test_cutoff_switch_from_bank_uses_buffered_phasing(platform):
    platform.start()
    platform.set_intake_quota("bank", 1000.0)
    # bank 8.0 -> 下限 4.0，折算 0.05 MPa/kg，即 80kg 见底；槽车 7.0 可用。
    report = platform.pressurize(inlet_pressure_mpa=7.0, bank_pressure_mpa=8.0, charge_kg=120.0)
    record = _switch(report)
    assert record["from"] == "bank" and record["to"] == "truck"
    assert record["mode"] == "buffered"
    assert record["reason"] == REASON_CUTOFF
    assert record["last_phase"] == "transfer"
    phases = [step["detail"] for step in report["steps"] if step["action"] == "switch_phase"]
    names = [item["phase"] for item in phases]
    assert names[0] == "ramp_down"
    assert "overlap" in names
    # 切换全程入口不断档：每个分相步两路合计供气都为正，且不低于怠速流量。
    idle = platform.config.intake.idle_flow_kg_per_min
    for item in phases:
        assert item["bank_kg"] + item["truck_kg"] > 0.0
        assert item["effective_rate_kg_per_min"] >= idle


def test_outlet_pressure_keeps_climbing_through_switch(platform):
    platform.start()
    platform.set_intake_quota("truck", 4.0)
    report = platform.pressurize(inlet_pressure_mpa=12.0, bank_pressure_mpa=9.0, charge_kg=10.0)
    assert "intake_switch_outlet_droop" not in report["violations"]
    ramps = [step["detail"]["pressure_mpa"] for step in report["steps"] if step["action"] == "ramp"]
    assert ramps[-1] >= platform.config.compressor.outlet_target_mpa - 1e-6


def test_both_quotas_exhausted_stops_without_hard_drawing(platform):
    platform.start()
    platform.set_intake_quota("truck", 3.0)
    platform.set_intake_quota("bank", 3.0)
    report = platform.pressurize(inlet_pressure_mpa=20.0, bank_pressure_mpa=18.0, charge_kg=20.0)
    assert report["stop_reason"] == STOP_QUOTA
    assert abs(report["intake_settlement"]["drawn_total_kg"] - 6.0) < 1e-9
    assert "intake_quota_exhausted" in report["violations"]


def test_switch_failure_rolls_back_to_original_source(platform):
    platform.start()
    platform.set_intake_quota("truck", 4.0)
    platform.arm_intake_switch_failure()
    report = platform.pressurize(inlet_pressure_mpa=12.0, bank_pressure_mpa=9.0, charge_kg=10.0)
    assert "intake_switch_failed" in report["violations"]
    assert report["failed_switches_this_op"] == 1
    record = _switch(report)
    assert record["rolled_back"] is True
    settle = report["intake_settlement"]
    # 部分失败回到原路：活动路仍是槽车，瓶组没被动过。
    assert settle["active_source"] == "truck"
    assert settle["drawn_kg"]["bank"] == 0.0
    assert settle["drawn_kg"]["truck"] == 4.0
    snapshot = platform.intake_snapshot()
    assert snapshot["failed_switches"] == 1
    assert snapshot["active_source"] == "truck"


def test_buffered_switch_failure_rolls_back_entire_transaction(platform):
    platform.start()
    platform.set_intake_quota("bank", 1000.0)
    platform.arm_intake_switch_failure()
    report = platform.pressurize(inlet_pressure_mpa=7.0, bank_pressure_mpa=8.0, charge_kg=120.0)
    assert "intake_switch_failed" in report["violations"]
    settle = report["intake_settlement"]
    # 切换事务原子回滚：活动路回到瓶组，槽车压力与抽取量完全没被动过。
    assert settle["active_source"] == "bank"
    assert settle["drawn_kg"]["truck"] == 0.0
    assert abs(settle["drawn_kg"]["bank"] - 80.0) < 0.01
    assert abs(settle["drawn_total_kg"] - 80.0) < 0.01
    snapshot = platform.intake_snapshot()
    assert snapshot["sources"]["truck"]["pressure_mpa"] == 7.0


def test_quota_plan_settle_and_switch_land_in_audit(platform):
    platform.start()
    platform.set_intake_quota("truck", 4.0)
    platform.pressurize(inlet_pressure_mpa=12.0, bank_pressure_mpa=9.0, charge_kg=10.0)
    entries = platform.audit.entries()
    actions = [entry["action"] for entry in entries if entry["component"] == "intake"]
    assert "intake_plan" in actions
    assert "intake_switch" in actions
    assert "intake_settle" in actions
    switch = next(entry for entry in entries if entry["action"] == "intake_switch")
    assert switch["payload"]["from_source"] == "truck"
    assert switch["payload"]["to_source"] == "bank"


def test_reconcile_matches_ledger_after_switch(platform):
    platform.start()
    platform.set_intake_quota("truck", 4.0)
    platform.pressurize(inlet_pressure_mpa=12.0, bank_pressure_mpa=9.0, charge_kg=10.0)
    report = platform.intake_reconcile()
    assert report["ok"], report["differences"]
    assert report["switch_records"] == 1
    assert report["rolled_back"] == 0
    assert report["drawn_kg_audit"] == report["drawn_kg_ledger"]


def test_reconcile_counts_rolled_back_switch(platform):
    platform.start()
    platform.set_intake_quota("truck", 4.0)
    platform.arm_intake_switch_failure()
    platform.pressurize(inlet_pressure_mpa=12.0, bank_pressure_mpa=9.0, charge_kg=10.0)
    report = platform.intake_reconcile()
    assert report["ok"], report["differences"]
    assert report["rolled_back"] == 1
    assert report["switch_records"] == 1


def test_quota_update_is_separately_audited(platform):
    platform.start()
    platform.set_intake_quota("bank", 50.0)
    entries = platform.audit.entries()
    event = next(entry for entry in entries if entry["action"] == "intake_quota_set")
    assert event["payload"]["source"] == "bank"
    assert event["payload"]["quota_kg"] == 50.0
    assert platform.intake_snapshot()["sources"]["bank"]["quota_kg"] == 50.0


def test_intake_reset_advances_epoch_and_reconcile_ignores_old(platform):
    platform.start()
    platform.set_intake_quota("truck", 4.0)
    platform.pressurize(inlet_pressure_mpa=12.0, bank_pressure_mpa=9.0, charge_kg=6.0)
    reset = platform.reset_intake()
    assert reset["epoch"] == 1
    # 新纪元没有任何取气操作，核对应当干净（旧纪元流水不参与）。
    report = platform.intake_reconcile()
    assert report["ok"], report["differences"]
    assert report["epoch"] == 1
    assert report["settles"] == 0


def test_quota_ledger_restores_after_rebuild(tmp_path):
    from hydrorefuel.app import build_platform
    from hydrorefuel.clock import ManualClock

    clock = ManualClock()
    first = build_platform(default_config(), clock=clock, storage_root=str(tmp_path / "state"))
    first.start()
    first.set_intake_quota("truck", 4.0)
    first.pressurize(inlet_pressure_mpa=12.0, bank_pressure_mpa=9.0, charge_kg=10.0)

    rebuilt = build_platform(default_config(), clock=clock, storage_root=str(tmp_path / "state"))
    snapshot = rebuilt.intake_snapshot()
    assert snapshot["sources"]["truck"]["drawn_kg"] == 4.0
    assert snapshot["sources"]["truck"]["quota_kg"] == 0.0
    assert snapshot["sources"]["bank"]["drawn_kg"] == 6.0
    assert rebuilt.intake_reconcile()["ok"]


def test_no_source_available_rejects_start(platform):
    platform.start()
    # 两路压力都低于各自下限：无法选出首抽路。
    report = platform.pressurize(inlet_pressure_mpa=1.0, bank_pressure_mpa=1.0)
    assert not report["ok"]
    assert "intake_no_source_available" in report["violations"]


def test_selector_unit_buffered_switch_does_not_gap():
    selector = IntakeSelector(default_config().intake)
    selector.set_quota("truck", 1000.0)
    selector.begin({"truck": 7.0, "bank": 8.0})
    last_record = None
    gap = False
    for _ in range(2000):
        take = selector.step(4.0, 1.0)
        if take["switching"] and sum(take["draws"].values()) <= 0.0:
            gap = True
        if take["switch_record"]:
            last_record = take["switch_record"]
        if take["stop_reason"]:
            break
        if last_record and not take["switching"]:
            break
    assert not gap
    assert last_record is not None
    assert last_record["mode"] == "buffered"
    assert selector.active_source == "truck"


def test_reconcile_flags_tampered_ledger(platform):
    platform.start()
    platform.set_intake_quota("truck", 4.0)
    platform.pressurize(inlet_pressure_mpa=12.0, bank_pressure_mpa=9.0, charge_kg=10.0)
    # 篡改台账实抽量（模拟账本与流水脱节），核对必须报差异。
    platform.intake._sources["truck"].drawn_kg += 5.0
    report = platform.intake_reconcile()
    assert not report["ok"]
    assert any("truck" in item for item in report["differences"])
