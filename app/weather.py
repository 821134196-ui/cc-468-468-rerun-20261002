"""模拟天气适配器：按时间脚本返回雨量与各入口水位（演示用，无外部服务）。"""
from __future__ import annotations

import math
import threading
import time


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


class WeatherAdapter:
    """一条确定性的暴雨过程线：小雨 → 升级 → 峰值 → 减弱 → 解除。

    时间轴（秒，相对适配器创建）：
      0–15s   小雨，水位贴近地面
      15–45s  雨势增强，水位抬升，部分入口越过橙色阈值
      45–70s  峰值，个别入口逼近红色
      70–110s 雨势减弱，水位回落
      110s+   雨后低位，等待复查解除
    """

    PHASES = (
        (0, 15, 3.0, None),
        (15, 45, None, None),
        (45, 70, 24.0, None),
        (70, 110, None, None),
        (110, 10**9, 1.0, None),
    )

    def __init__(self, speed: float = 1.0, start_elapsed: float = 0.0, base=None):
        self.speed = speed
        base = base if base is not None else time.monotonic()
        self.t0_offset = base - start_elapsed / speed
        self._lock = threading.Lock()
        self._manual = None  # 手动设定的演示档位

    def elapsed(self, mono) -> float:
        return max(0.0, (mono - self.t0_offset) * self.speed)

    def set_manual(self, rain=None, levels=None):
        """演示开关：直接指定雨量/水位；传 None 恢复脚本。"""
        with self._lock:
            if rain is None and levels is None:
                self._manual = None
            else:
                self._manual = (rain, levels)

    def rainfall_mm(self, t: float) -> float:
        with self._lock:
            if self._manual and self._manual[0] is not None:
                return float(self._manual[0])
        if t < 15:
            return round(2.0 + 0.8 * math.sin(t / 3.0), 1)
        if t < 45:
            p = (t - 15) / 30.0
            return round(3.0 + 21.0 * _clamp(p, 0, 1), 1)          # 3 → 24
        if t < 70:
            return round(24.0 + 2.0 * math.sin((t - 45) / 5.0), 1)
        if t < 110:
            p = (t - 70) / 40.0
            return round(24.0 - 22.0 * _clamp(p, 0, 1), 1)         # 24 → 2
        return round(max(0.5, 1.0 + 0.5 * math.sin(t / 7.0)), 1)

    def level_cm(self, entrance, t: float) -> float:
        """每个入口地势不同，汇水速度/峰值不同。"""
        with self._lock:
            if self._manual and self._manual[1] is not None:
                lv = self._manual[1]
                val = lv.get(entrance['id'], lv.get('default', 0.0))
                return round(float(val), 1)
        # 汇水系数与滞后：低地势入口更危险
        gain = entrance['gain']
        lag = entrance['lag']
        tt = max(0.0, t - lag)
        if tt < 10:
            base = 1.0 + 0.4 * math.sin(tt / 2.0)
        elif tt < 55:
            p = (tt - 10) / 45.0
            base = 1.0 + gain * _clamp(p, 0, 1)
        elif tt < 80:
            base = 1.0 + gain
        else:
            p = _clamp((tt - 80) / 35.0, 0, 1)
            base = (1.0 + gain) * (1 - p) + 1.2 * p
        return round(max(0.0, base), 1)

    def phase_name(self, t: float) -> str:
        if t < 15:
            return '小雨巡查'
        if t < 45:
            return '雨势增强'
        if t < 70:
            return '暴雨峰值'
        if t < 110:
            return '雨势减弱'
        return '雨后复查'
