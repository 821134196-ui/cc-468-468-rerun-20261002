"""进程内监控任务：定时取模拟读数，推动预警升级、清单生成与回落复查解除。"""
from __future__ import annotations

import logging
import threading
import time

from . import service
from .db import connect

log = logging.getLogger('monitor')


class Monitor(threading.Thread):
    def __init__(self, db_path, adapter, entrances, interval=3.0):
        super().__init__(daemon=True, name='weather-monitor')
        self.db_path = db_path
        self.adapter = adapter
        self.entrance_ids = [e['id'] for e in entrances]
        self.interval = interval
        self._stop = threading.Event()
        self._blue_streak = 0
        self.last_tick = None

    def stop(self):
        self._stop.set()

    def run(self):
        log.info('监控任务启动，间隔 %ss', self.interval)
        while not self._stop.wait(self.interval):
            try:
                self.tick()
            except Exception:
                log.exception('监控周期异常')

    def tick(self):
        conn = connect(self.db_path)
        try:
            return self._tick(conn)
        finally:
            conn.close()

    def _tick(self, conn):
        ts = service.now()
        t = self.adapter.elapsed(time.monotonic())
        rain = self.adapter.rainfall_mm(t)
        conn.execute("INSERT INTO rainfall_readings (ts, value_mm) VALUES (?,?)",
                     (ts, rain))

        ents = conn.execute(
            f"SELECT * FROM entrances WHERE id IN ({','.join('?'*len(self.entrance_ids))})",
            self.entrance_ids).fetchall()
        pairs = []
        max_cm = 0.0
        for ent in ents:
            cm = self.adapter.level_cm(ent, t)
            max_cm = max(max_cm, cm)
            conn.execute(
                "INSERT INTO level_readings (ts, entrance_id, level_cm) VALUES (?,?,?)",
                (ts, ent['id'], cm))
            pairs.append((ent, cm))

        level = service.level_from_readings(rain, pairs)
        active = service.active_alert(conn)
        phase = self.adapter.phase_name(t)

        if service.LEVEL_RANK[level] >= service.LEVEL_RANK['orange']:
            alert_id, created = service.ensure_alert(
                conn, level, rain, f'{phase}：雨量{rain}mm，最高水位{max_cm}cm', ts)
            # 达部署阈值的入口自动生成部署清单（在途任务不重复）
            for ent, cm in pairs:
                if cm >= ent['orange_cm']:
                    exists = conn.execute(
                        "SELECT 1 FROM tasks WHERE entrance_id=? AND status IN (%s)"
                        % ','.join('?' * len(service.ACTIVE_TASK_STATUSES)),
                        (ent['id'], *service.ACTIVE_TASK_STATUSES)).fetchone()
                    if not exists:
                        tid = service.generate_checklist(conn, ent['id'], alert_id, ts)
                        log.warning('入口 %s 水位 %.1fcm 达阈值，自动生成部署任务 #%s',
                                    ent['id'], cm, tid)
            self._blue_streak = 0
        elif active is not None:
            # 回落复查：连续 2 个周期雨量与各入口水位都低于黄色阈值才解除
            if level == 'blue':
                self._blue_streak += 1
            else:
                self._blue_streak = 0
            if self._blue_streak >= 2:
                service.clear_alert(conn, f'{phase}：连续复查水位回落、雨量减弱', ts)
                log.info('预警解除（复查通过）')
                self._blue_streak = 0
        else:
            self._blue_streak = 0

        self.last_tick = {
            'ts': ts, 'elapsed': round(t, 1), 'phase': phase,
            'rainfall_mm': rain, 'level': level,
            'entrances': [{'entrance_id': ent['id'], 'level_cm': cm}
                          for ent, cm in pairs],
        }
        conn.commit()
        return self.last_tick
