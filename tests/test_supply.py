"""双路气源选择、配额分配、切换时序、回退与审计一致性。"""

from __future__ import annotations

import dataclasses

import pytest

from hydrorefuel.app import Platform, build_platform
from hydrorefuel.config import (
    IMMEDIATE_SWITCH,
    PRESSURE_FIRST,
    RAMP_SWITCH,
    REMAINING_FIRST,
    SupplyLimits,
    default_config,
)
from hydrorefuel.errors import ConfigError, InterlockError
from hydrorefuel.supply.manifold import SupplyManifold
from hydrorefuel.supply.switch_record import SWITCH_OK, SWITCH_ROLLED_BACK


def _cfg(policy: str = PRESSURE_FIRST, **overrides) -> object:
    """构造带双路气源约束的配置；可覆盖单路配额与策略。"""

    cfg = default_config()
    supply = cfg.supply
    sources = dict(supply.sources)
    for key, values in overrides.items():
        sources[key] = dataclasses.replace(sources[key], **values)
    return dataclasses.replace(cfg, supply=dataclasses.replace(supply, quota_policy=policy, sources=sources))


def _manifold(policy: str = PRESSURE_FIRST, min_flow: float = 1.5) -> SupplyManifold:
    cfg = _cfg(policy)
    return SupplyManifold(cfg.supply, min_flow)


def _measure_both(manifold: SupplyManifold, truck: float, cascade: float) -> None:
    manifold.measure("truck", truck)
    manifold.measure("cascade", cascade)


# ---- 配额策略：定死压力优先 ---------------------------------------------


def test_policy_is_pinned_to_pressure_in_shipped_config():
    cfg = default_config()
    assert cfg.supply.quota_policy == PRESSURE_FIRST
    # 运行期对象不提供任何改策略的入口。
    assert not hasattr(cfg.supply, "set_quota_policy")
    manifold = _manifold()
    assert manifold.policy == PRESSURE_FIRST


def test_config_rejects_unknown_policy():
    from hydrorefuel.config import config_to_mapping

    section = config_to_mapping(default_config())["limits"]["supply"]
    section["quota_policy"] = "round_robin"
    with pytest.raises(ConfigError):
        # 走一遍映射装配，非法策略必须在装配阶段被拦下。
        SupplyLimits.from_mapping(section)


def _supply_section(cfg, **patch) -> dict:
    from hydrorefuel.config import config_to_mapping

    section = config_to_mapping(cfg)["limits"]["supply"]
    section.update(patch)
    return section


def test_source_low_pressure_must_meet_compressor_inlet():
    cfg = default_config()
    section = _supply_section(cfg)
    section["sources"]["truck"]["low_mpa"] = cfg.compressor.inlet_min_mpa - 0.5
    limits = SupplyLimits.from_mapping(section)
    with pytest.raises(ConfigError):
        limits.validate_compressor(cfg.compressor)


# ---- 配额计划：先抽哪路、各抽多少算得清 ---------------------------------


def test_plan_prefers_higher_pressure():
    manifold = _manifold()
    _measure_both(manifold, truck=20.0, cascade=17.0)
    plan = manifold.plan(10.0)
    assert plan.first() == "truck"
    assert [line.key for line in plan.lines] == ["truck", "cascade"]
    assert plan.lines[0].allocated_kg == 10.0
    assert plan.lines[1].allocated_kg == 0.0


def test_plan_breaks_pressure_tie_by_fixed_order():
    manifold = _manifold()
    _measure_both(manifold, truck=18.0, cascade=18.0)
    plan = manifold.plan(5.0)
    assert plan.first() == "truck"


def test_plan_remaining_policy_prefers_more_quota():
    cfg = _cfg(
        REMAINING_FIRST,
        truck={"quota_kg": 10.0},
        cascade={"quota_kg": 40.0},
    )
    manifold = SupplyManifold(cfg.supply, 1.5)
    _measure_both(manifold, truck=20.0, cascade=17.0)
    plan = manifold.plan(10.0)
    assert plan.policy == REMAINING_FIRST
    assert plan.first() == "cascade"


def test_plan_splits_demand_when_first_source_cannot_cover():
    manifold = _manifold()
    _measure_both(manifold, truck=20.0, cascade=17.0)
    plan = manifold.plan(80.0)
    lines = {line.key: line.allocated_kg for line in plan.lines}
    assert lines["truck"] == pytest.approx(60.0)
    assert lines["cascade"] == pytest.approx(20.0)
    assert plan.shortfall_kg == pytest.approx(0.0)


def test_plan_exhausts_quota_and_reports_shortfall():
    manifold = _manifold()
    _measure_both(manifold, truck=20.0, cascade=17.0)
    plan = manifold.plan(200.0)
    assert plan.allocated_kg == pytest.approx(95.0)
    assert plan.shortfall_kg == pytest.approx(105.0)


def test_plan_excludes_source_below_low_pressure():
    manifold = _manifold()
    # 瓶组压力低于自身下限，不具备抽气资格。
    _measure_both(manifold, truck=20.0, cascade=4.0)
    plan = manifold.plan(10.0)
    assert [line.key for line in plan.lines] == ["truck"]
    assert plan.first() == "truck"


# ---- 整路增压：自动切换、不断档、配额尽停 -------------------------------


def test_charge_draws_single_source_when_it_covers_demand(platform):
    platform.start()
    report = platform.supply_charge(demand_kg=10.0, truck_mpa=20.0, cascade_mpa=17.0)
    assert report["ok"], report["violations"]
    assert report["first_source"] == "truck"
    assert report["switches"] == []
    assert platform.manifold.used_kg("truck") == pytest.approx(10.0, abs=1e-6)
    assert platform.compressor.state == "stopped"


def test_charge_switches_on_low_pressure_with_ramp_buffer(platform):
    platform.start()
    # 槽车高压先抽，抽到下限；瓶组压力较低但仍高于压缩机入口下限，可接续。
    report = platform.supply_charge(
        demand_kg=40.0, flow_kg_per_min=4.0, dt=1.0, truck_mpa=8.0, cascade_mpa=7.5
    )
    assert report["ok"], report["violations"]
    switch = report["switches"][0]
    assert switch["source_from"] == "truck"
    assert switch["source_to"] == "cascade"
    assert switch["switch_mode"] == RAMP_SWITCH
    assert switch["buffered"] is True
    assert switch["reason"] == "low_pressure"
    # 切换全程出口压力不断档：重叠期/稳定期都不低于切换前。
    assert switch["pressure_continuous"] is True
    assert switch["outlet_during_mpa"] >= switch["outlet_before_mpa"]
    assert switch["outlet_after_mpa"] >= switch["outlet_during_mpa"]
    # 缓冲流量先压到喘振下限再切，且切换后恢复。
    buffer_phases = [
        step["detail"]["phase"]
        for step in report["steps"]
        if step["action"] == "flow_buffer"
    ]
    assert buffer_phases[0] == "down"
    assert buffer_phases[-1] == "up"
    assert platform.manifold.used_kg("truck") > 0
    assert platform.manifold.used_kg("cascade") > 0


def test_charge_switches_immediately_for_cascade_source(platform):
    platform.start()
    # 瓶组压力略高先抽（瓶组声明 immediate），槽车在入口下限之上接续。
    report = platform.supply_charge(
        demand_kg=50.0, flow_kg_per_min=4.0, dt=1.0, truck_mpa=8.0, cascade_mpa=8.5
    )
    assert report["ok"], report["violations"]
    switch = report["switches"][0]
    assert switch["source_from"] == "cascade"
    assert switch["source_to"] == "truck"
    assert switch["switch_mode"] == IMMEDIATE_SWITCH
    assert switch["immediate"] is True
    assert switch["buffered"] is False
    assert not any(step["action"] == "flow_buffer" for step in report["steps"])
    assert switch["pressure_continuous"] is True


def test_charge_stops_when_both_quotas_exhausted(platform):
    platform.start()
    report = platform.supply_charge(demand_kg=200.0, truck_mpa=20.0, cascade_mpa=17.0)
    assert report["ok"] is True  # 配额尽正常停机，不是违约
    assert report["stop_reason"] == "quota_exhausted"
    assert report["delivered_kg"] == pytest.approx(95.0)
    assert report["shortfall_kg"] == pytest.approx(105.0)
    assert any(step["action"] == "switch_unavailable" for step in report["steps"])


def test_charge_requires_measured_sources(platform):
    platform.start()
    # 全新台账、不测量：计划拿不到任何可用气源，拒绝启动。
    from hydrorefuel.sequence.supply import charge_from_supply

    report = charge_from_supply(
        platform.manifold,
        platform.compressor,
        platform.bank,
        platform.baseline,
        platform.surge,
        platform.unit,
        now=platform.clock.now(),
        demand_kg=5.0,
        clock=platform.clock,
    )
    assert report["ok"] is False
    assert "supply_no_source_available" in report["violations"]
    assert platform.compressor.state == "stopped"


# ---- 切换失败：部分失败，回到原路 ---------------------------------------


def test_failed_switch_rolls_back_to_original_source(platform):
    platform.start()
    platform.set_supply_fault("cascade", True)
    report = platform.supply_charge(
        demand_kg=40.0, flow_kg_per_min=4.0, dt=1.0, truck_mpa=8.0, cascade_mpa=7.5
    )
    assert report["ok"] is False
    assert "supply_switch_failed" in report["violations"]
    switch = report["switches"][0]
    assert switch["result"] == SWITCH_ROLLED_BACK
    assert switch["rolled_back"] is True
    # 回退：旧路（槽车）仍是最后使用的一路，瓶组阀门没打开，已抽气量保留。
    assert platform.manifold.source("truck").used_kg > 0
    assert platform.manifold.source("cascade").used_kg == 0.0
    assert platform.manifold.source("cascade").valve_open is False
    assert report["stop_reason"] == "switch_failed"
    assert platform.compressor.state == "stopped"


def test_cannot_inject_fault_while_source_is_active(platform):
    platform.start()
    platform.supply_charge(demand_kg=1.0, truck_mpa=20.0, cascade_mpa=17.0)
    # 充瓶结束后活动路释放，阀门关闭；构造一个活动路再注入故障应被拒绝。
    manifold = platform.manifold
    manifold.measure("truck", 20.0)
    manifold.engage("truck")
    with pytest.raises(InterlockError):
        manifold.set_fault("truck", True)


# ---- 配额、切换记录与审计一致性 ------------------------------------------


def test_quota_plan_and_settlement_are_audited(platform):
    platform.start()
    platform.supply_charge(demand_kg=10.0, truck_mpa=20.0, cascade_mpa=17.0)
    actions = [entry["action"] for entry in platform.audit.entries(limit=200)]
    assert "quota_allocated" in actions
    assert "supply_settled" in actions
    entries = {entry["action"]: entry for entry in platform.audit.entries(limit=200)}
    assert entries["quota_allocated"]["payload"]["policy"] == PRESSURE_FIRST
    assert entries["quota_allocated"]["payload"]["first"] == "truck"


def test_switch_records_reconcile_with_audit(platform):
    platform.start()
    platform.supply_charge(
        demand_kg=40.0, flow_kg_per_min=4.0, dt=1.0, truck_mpa=8.0, cascade_mpa=7.5
    )
    recon = platform.supply_reconcile()
    assert recon["consistent"] is True
    assert recon["switches_consistent"] is True
    assert recon["used_consistent"] is True
    assert recon["policy_consistent"] is True
    assert recon["switch_count"] == 1
    assert recon["audit_switch_count"] == 1


def test_reconcile_isolates_records_after_reset(platform):
    platform.start()
    platform.supply_charge(demand_kg=10.0, truck_mpa=20.0, cascade_mpa=17.0)
    assert platform.supply_reconcile()["used_consistent"] is True
    platform.reset_state()
    recon = platform.supply_reconcile()
    # 新 epoch：旧审计不参与核对，台账归零且一致。
    assert recon["epoch"] == 1
    assert recon["switch_count"] == 0
    assert recon["used_consistent"] is True
    assert recon["consistent"] is True


# ---- 落盘恢复 -------------------------------------------------------------


def test_supply_ledger_and_switches_persist_across_restart(platform, tmp_path):
    platform.start()
    platform.supply_charge(
        demand_kg=40.0, flow_kg_per_min=4.0, dt=1.0, truck_mpa=8.0, cascade_mpa=7.5
    )
    truck_used = platform.manifold.used_kg("truck")
    cascade_used = platform.manifold.used_kg("cascade")

    rebuilt = Platform(platform.config, clock=platform.clock)
    assert rebuilt.manifold.used_kg("truck") == pytest.approx(truck_used, abs=1e-6)
    assert rebuilt.manifold.used_kg("cascade") == pytest.approx(cascade_used, abs=1e-6)
    assert len(rebuilt.manifold.switches()) == 1
    record = rebuilt.manifold.switches()[0]
    assert record.result == SWITCH_OK
    # 重启后阀门一律回到关闭安全位，不自动恢复供气。
    assert rebuilt.manifold.active is None


# ---- 控制台展示与接口 -----------------------------------------------------


def test_console_exposes_supply_quota_and_reconcile(platform):
    from hydrorefuel.console.server import ConsoleServer

    platform.start()
    platform.supply_charge(demand_kg=10.0, truck_mpa=20.0, cascade_mpa=17.0)
    server = ConsoleServer(platform, port=0)
    base = server.start()
    try:
        import json
        import urllib.request

        with urllib.request.urlopen(f"{base}/api/supply", timeout=5) as response:
            payload = json.loads(response.read().decode())
        sources = payload["supply"]["sources"]
        assert sources["truck"]["quota_kg"] == 60.0
        assert sources["truck"]["used_kg"] > 0
        assert payload["supply"]["policy"] == PRESSURE_FIRST
        with urllib.request.urlopen(f"{base}/api/supply/reconcile", timeout=5) as response:
            recon = json.loads(response.read().decode())
        assert recon["consistent"] is True
    finally:
        server.stop()


def test_console_supply_fault_rejects_unknown_source(platform):
    from hydrorefuel.console.server import ConsoleServer

    server = ConsoleServer(platform, port=0)
    base = server.start()
    try:
        import json
        import urllib.error
        import urllib.request

        data = json.dumps({"source": "bogus", "faulted": True}).encode()
        request = urllib.request.Request(
            f"{base}/api/supply/fault", data=data, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(request, timeout=5)
        assert exc.value.code == 409
    finally:
        server.stop()
