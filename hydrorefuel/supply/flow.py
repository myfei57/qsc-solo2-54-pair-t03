"""抽气流量缓冲。

槽车一路到下限要求"先降流量缓冲再切"：流量按固定步长压到压缩机喘振流量
下限，让入口压力有时间重新建立，避免阀门重叠期压力断档；切完之后再按同样
步长恢复到原流量。瓶组一路声明的是立即切换，不走本缓冲。
"""

from __future__ import annotations


class FlowRamp:
    """按固定步长升降抽气流量的缓冲模型。"""

    def __init__(self, floor_kg_per_min: float, step_kg_per_min: float = 0.5) -> None:
        self._floor = float(floor_kg_per_min)
        self._rate = max(0.01, float(step_kg_per_min))
        self._flow = 0.0

    @property
    def flow_kg_per_min(self) -> float:
        return self._flow

    @property
    def floor_kg_per_min(self) -> float:
        return self._floor

    def reset(self) -> None:
        """回到零流量。"""

        self._flow = 0.0

    def set(self, flow_kg_per_min: float) -> float:
        """直接置为给定流量（建立活动路时的初值，不做缓冲）。"""

        self._flow = float(flow_kg_per_min)
        return self._flow

    def ramp_to(self, target_kg_per_min: float, dt: float) -> float:
        """按 ``rate * dt`` 的步长逼近目标流量，返回逼近后的流量。"""

        if dt <= 0:
            raise ValueError("步长必须为正")
        target = float(target_kg_per_min)
        step = self._rate * dt
        if abs(target - self._flow) <= step:
            self._flow = target
        elif target > self._flow:
            self._flow += step
        else:
            self._flow -= step
        return self._flow

    def ramp_down(self, dt: float) -> float:
        """降到喘振流量下限，返回当前流量。"""

        return self.ramp_to(self._floor, dt)

    def ramp_up(self, target_kg_per_min: float, dt: float) -> float:
        """从缓冲流量恢复到给定流量，返回当前流量。"""

        return self.ramp_to(target_kg_per_min, dt)
