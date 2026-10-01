"""平台装配与控制循环。

``Platform`` 把各组件按真实调用链接起来，并暴露四件事：操作编排（卸氢、增压
充瓶、加注、停止加注）、控制周期、并发仲裁探测和状态快照。控制周期每个周期
做三件事：核对加注确认是否超时、采集瓶组压力趋势、把平台计数落盘；跨模块
动作都由编排一次性走完并写审计。
"""

from __future__ import annotations

import dataclasses
import threading

from .audit.log import AuditLog
from .audit.record import AuditRecord
from .buffer.bank import BufferBank
from .buffer.baseline import BufferBaseline
from .buffer.snapshot import BufferSnapshot
from .clock import Clock, SystemClock
from .comp.machine import Compressor
from .comp.surge import SurgeGuard
from .config import StationConfig, SUPPLY_KEYS, default_config
from .cool.precool import Precooler
from .cool.snapshot import CoolSnapshot
from .cool.temperature import TemperatureModel
from .disp.machine import Dispenser
from .disp.nozzle import Nozzle
from .disp.setpoint import SetpointArbiter
from .errors import HydroError
from .eventbus import Event, EventBus
from .flow.meter import MassFlowMeter
from .flow.snapshot import FlowSnapshot
from .flow.step import FlowStep
from .ids import IdFactory
from .leak.detector import LeakDetector
from .leak.latch import LeakLatch
from .leak.snapshot import LeakSnapshot
from .ns.registry import NamespaceRegistry
from .ns.topology import Topology, build_default_topology
from .queue.confirm import Confirmation
from .queue.queue import FillQueue
from .queue.snapshot import QueueSnapshot
from .sequence import (
    charge_from_supply,
    fill_vehicle,
    pressurize_buffer,
    stop_fill,
    unload_truck,
)
from .store.journal import JsonlJournal
from .store.jsonfile import JsonStore
from .supply.manifold import SupplyManifold
from .telemetry import TrendBuffer
from .truck.machine import TruckMachine
from .vent.latch import VentLatch
from .vent.snapshot import VentSnapshot
from .vent.valve import VentValve

DEFAULT_UNIT = "station1"
DEFAULT_VEHICLE = "vehicle01"


class Platform:
    """加氢站卸氢增压与加注顺序控制平台。"""

    def __init__(
        self,
        config: StationConfig | None = None,
        clock: Clock | None = None,
        unit: str = DEFAULT_UNIT,
    ) -> None:
        self.config = config or default_config()
        self.clock = clock or SystemClock()
        self.bus = EventBus()
        self.unit = unit

        store = JsonStore(self.config.storage_root)
        self.store = store
        self.audit = AuditLog(self.clock, self.bus, JsonlJournal(store.root, "audit", segment_limit=512))
        self.namespaces = NamespaceRegistry(unit)
        self.topology: Topology = build_default_topology()
        self.trend = TrendBuffer(period_s=5.0)
        self.ids = IdFactory("op")

        self.truck = TruckMachine(self.config.truck)
        self.vent = VentValve(self.config.vent)
        self.vent_latch = VentLatch(self.config.vent.latch_travel_mm)
        self.compressor = Compressor(self.config.compressor)
        self.surge = SurgeGuard(self.config.compressor)
        self.manifold = SupplyManifold(self.config.supply, self.config.compressor.min_flow_kg_per_min)
        self.bank = BufferBank(self.config.buffer)
        self.baseline = BufferBaseline(
            self.config.buffer.target_pressure_mpa,
            self.config.buffer.pressure_tolerance_mpa,
            self.config.buffer.baseline_ttl_s,
        )
        # 平台启动即以当前时刻建立一次压力基线，避免首轮操作被判过期。
        self.baseline.calibrate(self.clock.now(), self.config.buffer.target_pressure_mpa)
        self.temperature = TemperatureModel(self.config.cool)
        self.precooler = Precooler(self.config.cool)
        self.meter = MassFlowMeter(self.config.flow)
        self.flow_step = FlowStep(self.config.flow)
        self.nozzle = Nozzle(self.config.dispenser.nozzle_seat_travel_mm)
        self.dispenser = Dispenser(self.config.dispenser, self.nozzle)
        self.setpoint = SetpointArbiter(
            self.config.buffer.target_pressure_mpa - self.config.buffer.pressure_tolerance_mpa,
            self.config.buffer.target_pressure_mpa + self.config.buffer.pressure_tolerance_mpa,
            self.config.buffer.target_pressure_mpa,
        )
        self.leak = LeakDetector(self.config.leak)
        self.leak_latch = LeakLatch(self.config.leak.hold_s)
        self.queue = FillQueue(self.config.queue)
        self.confirm = Confirmation(self.config.queue.confirm_timeout_s)

        self._cycles = 0
        self._operations = 0
        self._running = False
        self._last_audit_ts = 0.0
        self._supply_epoch = 0
        self._lock = threading.RLock()
        self._register_namespaces()
        self._restore()

    def _register_namespaces(self) -> None:
        """登记现场设备命名空间（控制台与审计共用同一套名字）。"""

        for area, device, kind in (
            ("station", "truck", "tube-trailer"),
            ("station", "vent", "vent-valve"),
            ("station", "compressor", "booster-compressor"),
            ("station", "supply-truck", "supply-source"),
            ("station", "supply-cascade", "supply-source"),
            ("station", "buffer", "storage-bank"),
            ("station", "precooler", "precool-unit"),
            ("station", "dispenser", "fill-dispenser"),
            ("station", "nozzle", "fill-nozzle"),
            ("station", "flowmeter", "mass-flow-meter"),
            ("station", "leak", "leak-detector"),
            ("station", "queue", "fill-queue"),
        ):
            self.namespaces.register(area, device, kind)

    def _restore(self) -> None:
        """从落盘状态恢复平台计数，保证重启后计数连续。"""

        tail = self.audit.replay_from_journal(limit=1)
        if tail:
            self._last_audit_ts = AuditRecord.from_dict(tail[-1]).ts
        state = self.store.read("platform_state")
        if isinstance(state, dict):
            self._operations = int(state.get("operations", 0))
            self._cycles = int(state.get("cycles", 0))
        supply_state = self.store.read("supply_state")
        if isinstance(supply_state, dict):
            self.manifold.restore(supply_state.get("ledger", {}))
            self._supply_epoch = int(supply_state.get("epoch", 0))

    def _persist(self) -> None:
        """落盘平台计数，供重启后对照。"""

        self.store.write(
            "platform_state",
            {"operations": self._operations, "cycles": self._cycles, "unit": self.unit},
        )
        self.store.write(
            "supply_state",
            {"epoch": self._supply_epoch, "ledger": self.manifold.ledger()},
        )

    def _record_operation(self, op_id: str, operation: str, report: dict) -> dict:
        """把编排步骤逐条写审计，并记一条汇总事件。"""

        station = str(report.get("station", ""))
        reserved = ("station", "op_id", "severity", "message", "ts", "component", "action")
        for step in report.get("steps", []):
            detail = {
                key: value
                for key, value in step.get("detail", {}).items()
                if not isinstance(value, (dict, list)) and key not in reserved
            }
            self.bus.publish(
                Event.of(
                    step["component"],
                    step["component"],
                    step["action"],
                    station=station,
                    op_id=op_id,
                    severity="notice",
                    **detail,
                )
            )
        for violation in report.get("violations", []):
            self.bus.publish(
                Event.of(
                    "audit",
                    "audit",
                    "interlock_rejected",
                    station=station,
                    op_id=op_id,
                    severity="alarm",
                    message=f"{operation} 被拒绝：{violation}",
                    violation=violation,
                )
            )
        summary = {
            "op_id": op_id,
            "operation": operation,
            "station": station,
            "ok": bool(report.get("ok")),
            "violations": list(report.get("violations", [])),
        }
        self.bus.publish(
            Event.of(
                "audit",
                "audit",
                f"{operation}_complete",
                severity="notice" if report.get("ok") else "warning",
                message=f"{operation} 完成" if report.get("ok") else f"{operation} 未完成",
                **summary,
            )
        )
        return summary

    def start(self) -> dict:
        """启动平台：核对拓扑与命名空间。"""

        with self._lock:
            self._running = True
            topology = self.topology.verify(set())
            self.bus.publish(Event.of("station", "station", "started", severity="notice", unit=self.unit))
            return {
                "running": self._running,
                "unit": self.unit,
                "topology_order": self.topology.order(),
                "namespaces": self.namespaces.addresses(),
                "pending_stages": sorted(topology["missing"]),
            }

    def stop(self) -> dict:
        """停运平台。"""

        with self._lock:
            self._running = False
            self.bus.publish(Event.of("station", "station", "stopped", severity="notice", unit=self.unit))
            return {"running": self._running, "operations": self._operations}

    def unload(
        self,
        hose_pressure_mpa: float = 20.0,
        target_kg: float = 20.0,
        dt: float = 1.0,
    ) -> dict:
        """卸氢编排：连接软管 → 卸氢 → 泄压放散 → 拔管。"""

        with self._lock:
            op_id = self.ids.next()
            report = unload_truck(self.truck, self.vent, self.vent_latch, self.unit, hose_pressure_mpa, target_kg, dt)
            summary = self._record_operation(op_id, "unload", report)
            self._operations += 1
            self._persist()
            return {**report, **summary}

    def pressurize(
        self,
        inlet_pressure_mpa: float = 12.0,
        flow_kg_per_min: float = 4.0,
        charge_kg: float = 8.0,
        dt: float = 1.0,
    ) -> dict:
        """增压充瓶编排：目标瓶组核对 → 压缩机启动 → 升压 → 瓶组缓存。"""

        with self._lock:
            op_id = self.ids.next()
            report = pressurize_buffer(
                self.compressor,
                self.bank,
                self.baseline,
                self.surge,
                self.unit,
                now=self.clock.now(),
                inlet_pressure_mpa=inlet_pressure_mpa,
                flow_kg_per_min=flow_kg_per_min,
                charge_kg=charge_kg,
                dt=dt,
            )
            summary = self._record_operation(op_id, "pressurize", report)
            self._operations += 1
            self._persist()
            return {**report, **summary}

    def measure_supply(self, truck_mpa: float | None = None, cascade_mpa: float | None = None) -> dict:
        """上报两路气源的入口实测压力（变送器），缺省采用配置初始压力。"""

        with self._lock:
            defaults = {
                key: self.config.supply.sources[key].initial_pressure_mpa for key in SUPPLY_KEYS
            }
            readings = {"truck": truck_mpa, "cascade": cascade_mpa}
            measured = {}
            for key in SUPPLY_KEYS:
                value = defaults[key] if readings[key] is None else float(readings[key])
                self.manifold.measure(key, value)
                measured[key] = round(value, 3)
                self.bus.publish(
                    Event.of(
                        "supply",
                        "supply",
                        "supply_measure",
                        severity="notice",
                        station=self.unit,
                        source=key,
                        pressure_mpa=round(value, 3),
                        epoch=self._supply_epoch,
                    )
                )
            self._persist()
            return {"measured": measured, "epoch": self._supply_epoch, "supply": self.supply_snapshot()}

    def supply_charge(
        self,
        demand_kg: float = 10.0,
        flow_kg_per_min: float = 4.0,
        dt: float = 1.0,
        truck_mpa: float | None = None,
        cascade_mpa: float | None = None,
    ) -> dict:
        """双路气源增压充瓶：测量 → 配额计划 → 逐路抽气与切换 → 缓存停机。"""

        with self._lock:
            # 未显式测量时按当前台账压力续抽；台账也是冷的才采用配置初始压力。
            for key, override in (("truck", truck_mpa), ("cascade", cascade_mpa)):
                if override is not None:
                    self.manifold.measure(key, float(override))
                elif not self.manifold.source(key).measured:
                    self.manifold.measure(key, self.config.supply.sources[key].initial_pressure_mpa)

            op_id = self.ids.next()
            report = charge_from_supply(
                self.manifold,
                self.compressor,
                self.bank,
                self.baseline,
                self.surge,
                self.unit,
                now=self.clock.now(),
                demand_kg=demand_kg,
                flow_kg_per_min=flow_kg_per_min,
                dt=dt,
                clock=self.clock,
            )
            plan = report.get("plan") or {}
            self.bus.publish(
                Event.of(
                    "supply",
                    "supply",
                    "quota_allocated",
                    severity="notice",
                    station=self.unit,
                    op_id=op_id,
                    epoch=self._supply_epoch,
                    policy=plan.get("policy"),
                    first=plan.get("first"),
                    demand_kg=report.get("demand_kg"),
                    allocated_kg=plan.get("allocated_kg"),
                )
            )
            for record in report.get("switches", []):
                switch_payload = {
                    k: v
                    for k, v in record.items()
                    if not isinstance(v, (dict, list)) and k not in ("ts",)
                }
                self.bus.publish(
                    Event.of(
                        "supply",
                        "supply",
                        "supply_switch",
                        severity="warning" if record.get("rolled_back") else "notice",
                        station=self.unit,
                        op_id=op_id,
                        epoch=self._supply_epoch,
                        switch_ts=record.get("ts"),
                        **switch_payload,
                    )
                )
            used = {key: round(self.manifold.used_kg(key), 3) for key in SUPPLY_KEYS}
            self.bus.publish(
                Event.of(
                    "supply",
                    "supply",
                    "supply_settled",
                    severity="notice" if report.get("ok") else "warning",
                    station=self.unit,
                    op_id=op_id,
                    epoch=self._supply_epoch,
                    delivered_kg=report.get("delivered_kg"),
                    shortfall_kg=report.get("shortfall_kg"),
                    stop_reason=report.get("stop_reason"),
                    truck_used_kg=used["truck"],
                    cascade_used_kg=used["cascade"],
                )
            )
            summary = self._record_operation(op_id, "supply_charge", report)
            self._operations += 1
            self._persist()
            return {**report, **summary}

    def set_supply_fault(self, key: str, faulted: bool) -> dict:
        """注入或清除一路气源阀门故障，用于切换失败回退演练。"""

        with self._lock:
            self.manifold.set_fault(key, faulted)
            self.bus.publish(
                Event.of(
                    "supply",
                    "supply",
                    "supply_fault",
                    severity="alarm" if faulted else "notice",
                    station=self.unit,
                    source=key,
                    faulted=faulted,
                    epoch=self._supply_epoch,
                )
            )
            return {"source": key, "faulted": faulted, "supply": self.supply_snapshot()}

    def fill(
        self,
        vehicle: str = DEFAULT_VEHICLE,
        rate_kg_per_min: float = 3.0,
        steps: int = 3,
        dt: float = 36.0,
    ) -> dict:
        """加注编排：队列确认 → 枪落座 → 预冷 → 加注 → 隔离停机 → 枪回位。"""

        with self._lock:
            op_id = self.ids.next()
            try:
                self.dispenser.check_line_pressure(self.bank.pressure_mpa)
            except HydroError as error:
                self.note_error(error)
                return {"station": self.unit, "vehicle": vehicle, "ok": False, "violations": ["line_pressure_high"]}
            before = self.meter.total_kg()
            report = fill_vehicle(
                self.dispenser,
                self.nozzle,
                self.precooler,
                self.temperature,
                self.meter,
                self.flow_step,
                self.confirm,
                self.unit,
                vehicle=vehicle,
                rate_kg_per_min=rate_kg_per_min,
                steps=steps,
                now=self.clock.now(),
                dt=dt,
            )
            if report["ok"]:
                try:
                    self.bank.draw(self.meter.total_kg() - before)
                except HydroError as error:
                    self.note_error(error)
            summary = self._record_operation(op_id, "fill", report)
            self._operations += 1
            self._persist()
            return {**report, **summary}

    def stop_fill(self) -> dict:
        """停止加注编排：先隔离，再停机。"""

        with self._lock:
            op_id = self.ids.next()
            report = stop_fill(self.dispenser, self.unit)
            if report["ok"]:
                self.nozzle.unseat()
                report["steps"].append(
                    {
                        "component": "disp",
                        "action": "unseat_nozzle",
                        "detail": {"seated": self.nozzle.is_seated(), "travel_mm": self.nozzle.travel_mm()},
                    }
                )
            summary = self._record_operation(op_id, "stop_fill", report)
            self._operations += 1
            self._persist()
            return {**report, **summary}

    def sample_leak(self, concentration_ppm: float) -> dict:
        """记一次泄漏采样；达到阀值即触发并保持闩锁。"""

        with self._lock:
            result = self.leak.sample(concentration_ppm)
            if result["tripped"] and not self.leak_latch.is_engaged(self.clock.now()):
                self.leak_latch.engage(self.clock.now())
            self.bus.publish(Event.of("leak", "leak", "sample", severity="alarm" if result["tripped"] else "notice", ppm=result["ppm"]))
            result["latch_engaged"] = self.leak_latch.is_engaged(self.clock.now())
            result["remaining_s"] = round(self.leak_latch.remaining_s(self.clock.now()), 1)
            return result

    def release_leak(self) -> dict:
        """保持期结束后复位泄漏闩锁。"""

        with self._lock:
            released = self.leak_latch.reset(self.clock.now())
            return {"released": released, "cycles": self.leak_latch.cycles()}

    def calibrate_baseline(self, target_mpa: float) -> dict:
        """重新标定瓶组压力基线，返回标定前后的差值。"""

        with self._lock:
            before = self.baseline.target_mpa
            after = self.baseline.calibrate(self.clock.now(), target_mpa)
            self.bus.publish(
                Event.of(
                    "buffer",
                    "buffer",
                    "baseline_recalibrated",
                    severity="notice",
                    target_before_mpa=before,
                    target_after_mpa=after,
                    epoch=self.baseline.epoch,
                )
            )
            return {"before_mpa": before, "after_mpa": after, "epoch": self.baseline.epoch}

    def disturb_buffer(self, delta_mpa: float) -> dict:
        """模拟一次瓶组压力扰动，返回扰动后的压力。"""

        before = self.bank.pressure_mpa
        after = self.bank.disturb(delta_mpa)
        return {
            "before_mpa": round(before, 3),
            "after_mpa": round(after, 3),
            "within_band": self.bank.within_band(),
        }

    def enqueue_vehicle(self, vehicle: str) -> dict:
        """把车辆加入加注队列，返回排队位次。"""

        with self._lock:
            position = self.queue.enqueue(vehicle, self.clock.now())
            self.bus.publish(Event.of("queue", "queue", "enqueue", severity="notice", vehicle=vehicle, position=position))
            return {"vehicle": vehicle, "position": position, "size": self.queue.size()}

    def dequeue_vehicle(self) -> dict:
        """取出队首车辆并为其发起确认。"""

        with self._lock:
            vehicle = self.queue.dequeue()
            self.confirm.open(vehicle, self.clock.now())
            return {"vehicle": vehicle, "size": self.queue.size(), "position": self.queue.position(vehicle)}

    def namespace(self, address: str) -> dict:
        """按地址解析现场设备。"""

        return self.namespaces.resolve(address).as_dict()

    def readiness(self) -> dict:
        """汇总加注就绪判据，供控制台与巡检使用。"""

        fill_stage = self.topology.stage("fill")
        checks = {
            "running": self._running,
            "precool_ready": self.precooler.is_ready(),
            "temperature_in_band": self.temperature.reached(),
            "nozzle_latched": self.nozzle.is_latched(),
            "leak_latch_clear": not self.leak_latch.is_engaged(self.clock.now()),
            "confirm_idle": not self.confirm.pending(DEFAULT_VEHICLE),
        }
        recent = self.audit.tail(1)
        return {
            "ready": all(checks.values()),
            "checks": checks,
            "stage": {"name": fill_stage.name, "label": fill_stage.label, "requires": list(self.topology.predecessors("fill"))},
            "outstanding_confirms": self.confirm.outstanding(),
            "last_audit_ts": recent[-1]["ts"] if recent else None,
        }

    def cycle(self, dt: float = 1.0) -> dict:
        """推进一个控制周期，返回本周期摘要。"""

        if dt <= 0:
            raise ValueError("步长必须为正")
        with self._lock:
            self._cycles += 1
            moment = self.clock.now()
            blocked = self.leak_latch.is_engaged(moment)
            for vehicle in self.confirm.outstanding():
                if self.confirm.expired(vehicle, moment):
                    self.confirm.clear(vehicle)
                    self.bus.publish(
                        Event.of("queue", "queue", "confirm_expired", severity="warning", vehicle=vehicle)
                    )
            previous = self.trend.latest("buffer_pressure")
            pressure = self.bank.pressure_mpa
            self.trend.append("buffer_pressure", moment, pressure)
            self.trend.append("precool_temp", moment, self.temperature.value_c)
            self._persist()
            delta = 0.0 if previous is None else pressure - previous.value
            return {
                "cycle": self._cycles,
                "blocked": blocked,
                "buffer_pressure_mpa": round(pressure, 3),
                "buffer_delta_mpa": round(delta, 3),
                "headroom_kg": round(self.bank.headroom_kg(), 3),
                "queue_size": self.queue.size(),
                "outstanding_confirms": self.confirm.outstanding(),
                "leak_tripped": self.leak.tripped(),
            }

    def concurrency_probe(self, rounds: int = 24) -> dict:
        """用真实并行调用探测设定点仲裁与确认并发的结果收敛。

        两个线程分别代表自动循环与手动控制台，同时向加注机提交压力设定点；
        另一组线程同时对同一辆车发起确认。探测只读取最终提交值与确认结果，
        用来确认共享对象始终收敛到唯一结论。
        """

        if rounds < 1:
            raise ValueError("探测轮数必须为正")

        def worker(writer: str, value_mpa: float, priority: int) -> None:
            for _ in range(rounds):
                self.setpoint.request(writer, value_mpa, priority)

        threads = [
            threading.Thread(target=worker, args=("auto_cycle", self.setpoint.limits[1], 50)),
            threading.Thread(target=worker, args=("manual_console", self.setpoint.limits[0], 80)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        outcome = self.setpoint.resolve()

        self.confirm.open("probe-vehicle", self.clock.now())
        winners: list[bool] = []
        guard = threading.Lock()

        def confirm_worker() -> None:
            accepted = self.confirm.confirm("probe-vehicle", self.clock.now())
            with guard:
                winners.append(accepted)

        confirm_threads = [threading.Thread(target=confirm_worker) for _ in range(2)]
        for thread in confirm_threads:
            thread.start()
        for thread in confirm_threads:
            thread.join()
        self.confirm.clear("probe-vehicle")

        return {
            "rounds": rounds,
            "setpoint": outcome.as_dict(),
            "committed_mpa": round(self.setpoint.committed(), 3),
            "writers": self.setpoint.writers(),
            "confirm_attempts": len(winners),
            "confirm_accepted": sum(1 for item in winners if item),
        }

    def note_error(self, error: HydroError) -> dict:
        """把平台异常写入审计，返回落盘记录。"""

        payload = error.as_record()
        return self.audit.record(
            AuditRecord.create(
                self.clock.now(),
                "",
                payload["component"],
                "error",
                severity="warning",
                message=payload["message"],
            )
        )

    def reset_state(self) -> dict:
        """清掉落盘计数与各组件运行态（交付前重新标定用）。"""

        with self._lock:
            self.truck.reset()
            self.compressor.reset()
            self.manifold.reset()
            self._supply_epoch += 1
            self.bank.reset()
            self.temperature.reset()
            self.precooler.reset()
            self.meter.reset()
            self.leak.reset()
            self.dispenser.reset()
            self.queue.reset()
            removed = self.store.remove("platform_state") if self.store.exists("platform_state") else False
            # 新 epoch 写一份零台账，重启后恢复到复位后的状态，不与旧审计混算。
            self._persist()
            self._operations = 0
            self._cycles = 0
            return {"platform_state_removed": removed, "store_root": self.store.root, "supply_epoch": self._supply_epoch}

    def truck_snapshot(self) -> dict:
        """返回槽车状态快照。"""

        return self.truck.snapshot().as_dict()

    def vent_snapshot(self) -> dict:
        """返回放散状态快照。"""

        return VentSnapshot(
            open=self.vent.is_open(),
            latch_engaged=self.vent_latch.is_engaged(),
            latch_travel_mm=self.vent_latch.travel(),
            latch_cycles=self.vent_latch.cycles(),
            latch_resets=self.vent_latch.resets(),
            bursts=self.vent.bursts(),
            vented_mpa=self.vent.vented_mpa(),
        ).as_dict()

    def compressor_snapshot(self) -> dict:
        """返回压缩机状态快照。"""

        return {
            **self.compressor.snapshot().as_dict(),
            "surge_trips": self.surge.trips(),
        }

    def supply_snapshot(self) -> dict:
        """返回双路气源快照（控制台展示配额与切换状态用）。"""

        return {
            **self.manifold.snapshot().as_dict(),
            "switches": [record.as_dict() for record in self.manifold.switches()],
            "epoch": self._supply_epoch,
        }

    def supply_reconcile(self) -> dict:
        """核对两路配额台账、切换记录与审计流水三方一致。

        台账给出每路配额用量；切换记录条数与审计里 ``supply_switch`` 事件逐条
        对照；结算事件里的分路用量之和对照台账。只核对当前 epoch（reset 之后的
        记录与旧台账不混算）。
        """

        with self._lock:
            ledger = self.manifold.snapshot().as_dict()
            records = self.audit.records(limit=4096)
            epoch = self._supply_epoch

            def in_epoch(payload: dict) -> bool:
                return payload.get("epoch") == epoch

            switch_events = [
                entry for entry in records
                if entry.component == "supply" and entry.action == "supply_switch" and in_epoch(entry.payload)
            ]
            settled_events = [
                entry for entry in records
                if entry.component == "supply" and entry.action == "supply_settled" and in_epoch(entry.payload)
            ]
            switch_records = self.manifold.switches()

            switch_consistent = len(switch_events) == len(switch_records)
            switch_pairs: list[dict] = []
            for record, event in zip(switch_records, switch_events):
                pair_ok = (
                    event.payload.get("source_from") == record.source_from
                    and event.payload.get("source_to") == record.source_to
                    and event.payload.get("result") == record.result
                )
                switch_consistent = switch_consistent and pair_ok
                switch_pairs.append(
                    {"seq": record.seq, "result": record.result, "audit_ts": round(event.ts, 3), "ok": pair_ok}
                )

            # 结算事件记的是当次操作后的累计用量，以最近一次结算对照当前台账。
            audited_truck = (
                float(settled_events[-1].payload.get("truck_used_kg", 0.0)) if settled_events else 0.0
            )
            audited_cascade = (
                float(settled_events[-1].payload.get("cascade_used_kg", 0.0)) if settled_events else 0.0
            )

            ledger_truck = self.manifold.used_kg("truck")
            ledger_cascade = self.manifold.used_kg("cascade")
            # 审计值按 3 位小数落盘，核对容差按半档 0.0005 再留一档余量。
            used_consistent = (
                abs(audited_truck - ledger_truck) <= 5e-3 and abs(audited_cascade - ledger_cascade) <= 5e-3
            )

            allocated_events = [
                entry for entry in records
                if entry.component == "supply" and entry.action == "quota_allocated" and in_epoch(entry.payload)
            ]
            policy_consistent = all(entry.payload.get("policy") == self.manifold.policy for entry in allocated_events)

            return {
                "epoch": epoch,
                "policy": self.manifold.policy,
                "ledger": ledger["sources"],
                "ledger_total": ledger["total"],
                "switch_count": len(switch_records),
                "audit_switch_count": len(switch_events),
                "switch_pairs": switch_pairs,
                "audit_last_used_kg": {"truck": round(audited_truck, 3), "cascade": round(audited_cascade, 3)},
                "switches_consistent": switch_consistent,
                "used_consistent": used_consistent,
                "policy_consistent": policy_consistent,
                "settled_ops": len(settled_events),
                "consistent": switch_consistent and used_consistent and policy_consistent,
            }

    def buffer_snapshot(self) -> dict:
        """返回瓶组状态快照。"""

        now = self.clock.now()
        return BufferSnapshot(
            pressure_mpa=self.bank.pressure_mpa,
            stored_kg=self.bank.stored_kg,
            capacity_kg=self.config.buffer.capacity_kg,
            target_mpa=self.config.buffer.target_pressure_mpa,
            within_band=self.bank.within_band(),
            baseline_epoch=self.baseline.epoch,
            baseline_age_s=self.baseline.age_s(now),
            baseline_expired=self.baseline.expired(now),
        ).as_dict()

    def cool_snapshot(self) -> dict:
        """返回预冷状态快照。"""

        return CoolSnapshot(
            state=self.precooler.state,
            temperature_c=self.temperature.value_c,
            target_c=self.config.cool.target_temp_c,
            held_s=self.precooler.held_s,
            ready=self.precooler.is_ready() and self.temperature.reached(),
        ).as_dict()

    def flow_snapshot(self) -> dict:
        """返回流量状态快照。"""

        return FlowSnapshot(
            rate_kg_per_min=self.meter.rate_kg_per_min(),
            total_kg=self.meter.total_kg(),
            step_kg=self.flow_step.current_kg(),
            step_target_kg=self.flow_step.target_kg,
            steps_completed=self.flow_step.completed(),
            within_step=self.flow_step.within(),
        ).as_dict()

    def disp_snapshot(self) -> dict:
        """返回加注机状态快照。"""

        return self.dispenser.snapshot().as_dict()

    def leak_snapshot(self) -> dict:
        """返回泄漏检测状态快照。"""

        now = self.clock.now()
        return LeakSnapshot(
            peak_ppm=self.leak.peak_ppm(),
            threshold_ppm=self.config.leak.threshold_ppm,
            tripped=self.leak.tripped(),
            latch_engaged=self.leak_latch.is_engaged(now),
            remaining_s=self.leak_latch.remaining_s(now),
            cycles=self.leak_latch.cycles(),
        ).as_dict()

    def queue_snapshot(self) -> dict:
        """返回加注队列状态快照。"""

        return QueueSnapshot(
            size=self.queue.size(),
            capacity=self.queue.limited.max_waiting,
            waiting=self.queue.waiting(),
            outstanding=self.confirm.outstanding(),
        ).as_dict()

    def topology_summary(self) -> dict:
        """返回工艺链拓扑摘要。"""

        return self.topology.summary()

    def namespaces_describe(self) -> dict:
        """返回命名空间摘要。"""

        return self.namespaces.describe()

    def status(self) -> dict:
        """汇总状态快照，供控制台与命令行读取。"""

        return {
            "platform": self.config.name,
            "unit": self.unit,
            "running": self._running,
            "operations": self._operations,
            "cycles": self._cycles,
            "now": round(self.clock.now(), 3),
            "monotonic": round(self.clock.monotonic(), 3),
            "utc": self.clock.utc_iso(),
            "id_prefix": self.ids.prefix,
            "ids_issued": self.ids.issued(),
            "truck": self.truck_snapshot(),
            "vent": self.vent_snapshot(),
            "compressor": self.compressor_snapshot(),
            "supply": self.supply_snapshot(),
            "buffer": self.buffer_snapshot(),
            "cool": self.cool_snapshot(),
            "flow": self.flow_snapshot(),
            "disp": self.disp_snapshot(),
            "leak": self.leak_snapshot(),
            "queue": self.queue_snapshot(),
            "setpoint": {
                "committed_mpa": round(self.setpoint.committed(), 3),
                "writers": self.setpoint.writers(),
            },
            "confirm_timeout_s": self.confirm.timeout_s,
            "leak_samples": self.leak.samples(),
            "last_audit_ts": self._last_audit_ts,
            "audit": self.audit.summary(),
        }

    def snapshot(self) -> dict:
        """返回只读的整体快照（状态 + 拓扑 + 命名空间 + 趋势）。"""

        return {
            "status": self.status(),
            "topology": self.topology_summary(),
            "namespaces": self.namespaces_describe(),
            "trend": self.trend.snapshot(),
            "readiness": self.readiness(),
        }


def build_platform(
    config: StationConfig | None = None,
    clock: Clock | None = None,
    storage_root: str | None = None,
    unit: str = DEFAULT_UNIT,
) -> Platform:
    """构造一个平台；``storage_root`` 可覆盖配置里的存储目录。"""

    base = config or default_config()
    if storage_root is not None:
        base = dataclasses.replace(base, storage_root=storage_root)
    return Platform(base, clock=clock, unit=unit)
