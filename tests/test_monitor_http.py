"""监控线程与 HTTP 接口集成测试。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.client import HTTPConnection

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import service
from app.db import connect, init_db
from app.monitor import Monitor
from app.weather import WeatherAdapter

PERSONNEL = [
    (1, '周建国', 'guard', ''), (2, '李海涛', 'worker', ''),
    (3, '王大山', 'worker', ''), (4, '陈立群', 'supervisor', ''),
]


class MonitorTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        self.conn = connect(self.path)
        init_db(self.conn)
        for p in PERSONNEL:
            self.conn.execute("INSERT INTO personnel (id,name,role,phone) VALUES (?,?,?,?)", p)
        self.conn.execute(
            "INSERT INTO entrances (id,name,slot_count,warn_cm,orange_cm,red_cm,gain,lag)"
            " VALUES ('E1','东门',2,5,10,20,18,0)")
        for i in range(1, 6):
            self.conn.execute(
                "INSERT INTO equipment (code,kind,name,shelf,status) VALUES (?,?,?,?,'available')",
                (f'B-A{i:02d}', 'panel', '挡板', 'A'))
        for i in range(1, 5):
            self.conn.execute(
                "INSERT INTO equipment (code,kind,name,shelf,status) VALUES (?,?,?,?,'available')",
                (f'P-{i:02d}', 'post', '立柱', 'B'))
        for i in range(1, 4):
            self.conn.execute(
                "INSERT INTO equipment (code,kind,name,shelf,status) VALUES (?,?,?,?,'available')",
                (f'S-{i:02d}', 'seal', '密封条', 'C'))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_escalation_auto_checklist_and_clear(self):
        adapter = WeatherAdapter()
        ents = self.conn.execute("SELECT * FROM entrances").fetchall()
        mon = Monitor(self.path, adapter, ents, interval=1)
        # 手动切到暴雨+高水位：一次 tick 内升级橙色并为 E1 自动生成清单
        adapter.set_manual(rain=20, levels={'default': 12.0})
        tick = mon.tick()
        self.assertEqual(tick['level'], 'orange')
        alert = service.active_alert(self.conn)
        self.assertIsNotNone(alert)
        self.assertEqual(alert['level'], 'orange')
        task = self.conn.execute(
            "SELECT * FROM tasks WHERE entrance_id='E1'").fetchone()
        self.assertIsNotNone(task, '达阈值入口应自动生成部署清单')
        n_items = self.conn.execute(
            "SELECT COUNT(*) c FROM task_items WHERE task_id=?", (task['id'],)).fetchone()['c']
        # 2 槽 = 2 挡板 + 2 密封条 + 3 立柱
        self.assertEqual(n_items, 7)

        # 第二个 tick 不应重复生成清单
        mon.tick()
        n_tasks = self.conn.execute("SELECT COUNT(*) c FROM tasks").fetchone()['c']
        self.assertEqual(n_tasks, 1)

        # 雨停水位回落：连续两次复查后自动解除预警
        adapter.set_manual(rain=0.5, levels={'default': 0.0})
        mon.tick()
        self.assertIsNotNone(service.active_alert(self.conn), '首次回落仍在复查期')
        mon.tick()
        self.assertIsNone(service.active_alert(self.conn), '复查通过应自动解除')
        mon.stop()

    def test_red_escalation(self):
        adapter = WeatherAdapter()
        ents = self.conn.execute("SELECT * FROM entrances").fetchall()
        mon = Monitor(self.path, adapter, ents)
        adapter.set_manual(rain=30, levels={'default': 22.0})
        tick = mon.tick()
        self.assertEqual(tick['level'], 'red')
        self.assertEqual(service.active_alert(self.conn)['level'], 'red')
        mon.stop()


# --------------------------------------------------------------- HTTP layer

class HttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fd, cls.path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        cls.photo_dir = tempfile.mkdtemp()
        from app import server
        conn = connect(cls.path)
        init_db(conn)
        conn.execute("INSERT INTO personnel (id,name,role,phone) VALUES (4,'主管','supervisor','')")
        conn.commit()
        conn.close()
        import app.server as srv
        cls._orig_photos = srv.PHOTOS
        from pathlib import Path
        srv.PHOTOS = Path(cls.photo_dir)
        from app.server import build_server
        cls.httpd = build_server(cls.path, host='127.0.0.1', port=0)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        import app.server as srv
        srv.PHOTOS = cls._orig_photos

    def _post(self, path, obj):
        req = urllib.request.Request(
            f'http://127.0.0.1:{self.port}{path}',
            data=json.dumps(obj).encode(),
            headers={'Content-Type': 'application/json'}, method='POST')
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def _get(self, path):
        with urllib.request.urlopen(f'http://127.0.0.1:{self.port}{path}') as r:
            return r.status, json.loads(r.read())

    def test_index_and_personnel(self):
        with urllib.request.urlopen(f'http://127.0.0.1:{self.port}/') as r:
            html = r.read().decode()
        self.assertIn('防汛挡板', html)
        st, body = self._get('/api/personnel')
        self.assertTrue(body['ok'])

    def test_photo_upload_roundtrip(self):
        import base64, pathlib
        # 1x1 PNG
        png = base64.b64encode(
            bytes.fromhex('89504e470d0a1a0a0000000d4948445200000001000000010806000000'
                          '1f15c4890000000a49444154789c6360000002000154a24f5f00000000'
                          '49454e44ae426082')).decode()
        st, body = self._post('/api/photos', {
            'data': f'data:image/png;base64,{png}', 'stage': 'install',
            'entrance_id': 'E1', 'taken_by': 4})
        self.assertEqual(st, 201)
        url = body['data']['url']
        with urllib.request.urlopen(f'http://127.0.0.1:{self.port}{url}') as r:
            self.assertEqual(r.status, 200)
            self.assertTrue(len(r.read()) > 0)
        self.assertTrue(pathlib.Path(self.photo_dir, body['data']['filename']).is_file())

    def test_role_enforcement_over_http(self):
        # 先准备一个已部署任务
        c = connect(self.path)
        init_db(c)
        for pid, name, role in ((1, '保安', 'guard'), (2, '工', 'worker'),
                                (3, '工2', 'worker')):
            c.execute("INSERT OR IGNORE INTO personnel (id,name,role,phone) VALUES (?,?,?,'')",
                      (pid, name, role))
        c.execute(
            "INSERT OR IGNORE INTO entrances (id,name,slot_count,warn_cm,orange_cm,red_cm,gain,lag)"
            " VALUES ('E1','东门',1,5,10,20,18,0)")
        for code, kind in (('B-A01', 'panel'), ('P-01', 'post'), ('P-02', 'post'),
                           ('S-01', 'seal')):
            c.execute(
                "INSERT OR IGNORE INTO equipment (code,kind,name,shelf,status) "
                "VALUES (?,?,?,?,'available')", (code, kind, kind, 'A'))
        c.commit()
        tid = service.generate_checklist(c, 'E1', None)
        service.claim_plan(c, tid, 2)
        p = c.execute(
            "SELECT locked_code FROM task_items WHERE kind='panel'").fetchone()['locked_code']
        service.install_panel(c, tid, 1, p, 2)
        for it in c.execute("SELECT id FROM task_items WHERE kind!='panel'"):
            service.install_post_or_seal(c, tid, it['id'], 2)
        service.confirm_slot(c, tid, 1, 'connection', 2)
        service.confirm_slot(c, tid, 1, 'seal', 2)
        service.control_channel(c, tid, 1, True)
        service.finish_deploy(c, tid, 2)
        service.ensure_alert(c, 'orange', 15, '暴雨')
        c.close()

        # 工人尝试审批提前解除 → 403
        st, body = self._post(f'/api/tasks/{tid}/decide-early',
                              {'person_id': 2, 'approve': True})
        self.assertEqual(st, 403)
        self.assertFalse(body['ok'])
        # 未申请先拆 → 400 拆除过早
        st, body = self._post(f'/api/tasks/{tid}/dismantle', {'person_id': 2})
        self.assertFalse(body['ok'])
        self.assertEqual(body['code'], 'alert_active')


if __name__ == '__main__':
    unittest.main(verbosity=2)
