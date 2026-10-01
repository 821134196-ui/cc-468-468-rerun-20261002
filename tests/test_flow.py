"""业务规则测试：器材排他、分段完整、预警状态、提前审批、归架差异、并发一次。"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import service
from app.db import connect, init_db
from app.seed import PERSONNEL

GUARD, WORKER, WORKER2, SUP = 1, 2, 3, 4


def expect_err(code):
    return unittest.TestCase().assertRaisesRegex(service.ServiceError, code)


class FlowTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        self.conn = connect(self.path)
        init_db(self.conn)
        for pid, name, role, phone in PERSONNEL:
            self.conn.execute(
                "INSERT INTO personnel (id,name,role,phone) VALUES (?,?,?,?)",
                (pid, name, role, phone))
        self.conn.execute(
            "INSERT INTO entrances (id,name,slot_count,warn_cm,orange_cm,red_cm,gain,lag)"
            " VALUES ('E1','东门',3,5,10,20,18,0)")
        self.conn.execute(
            "INSERT INTO entrances (id,name,slot_count,warn_cm,orange_cm,red_cm,gain,lag)"
            " VALUES ('E2','北门',2,5,10,20,20,0)")
        for i in range(1, 10):
            self.conn.execute(
                "INSERT INTO equipment (code,kind,name,shelf,status) VALUES (?,?,?,?,'available')",
                (f'B-A{i:02d}', 'panel', f'挡板{i}', 'A'))
        for i in range(1, 8):
            self.conn.execute(
                "INSERT INTO equipment (code,kind,name,shelf,status) VALUES (?,?,?,?,'available')",
                (f'P-{i:02d}', 'post', f'立柱{i}', 'B'))
        for i in range(1, 8):
            self.conn.execute(
                "INSERT INTO equipment (code,kind,name,shelf,status) VALUES (?,?,?,?,'available')",
                (f'S-{i:02d}', 'seal', f'密封条{i}', 'C'))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        for suffix in ('', '-wal', '-shm'):
            p = self.path + suffix
            if os.path.exists(p):
                os.unlink(p)

    # ---------------------------------------------------------- helpers

    def _task(self, ent='E1', alert_id=None):
        return service.generate_checklist(self.conn, ent, alert_id)

    def _claim(self, tid, who=WORKER):
        return service.claim_plan(self.conn, tid, who)

    def _install_all(self, tid, slots, who=WORKER):
        for s in range(1, slots + 1):
            p = self.conn.execute(
                "SELECT locked_code FROM task_items WHERE task_id=? AND slot_index=? AND kind='panel'",
                (tid, s)).fetchone()
            service.install_panel(self.conn, tid, s, p['locked_code'], who)
            seal = self.conn.execute(
                "SELECT id FROM task_items WHERE task_id=? AND slot_index=? AND kind='seal'",
                (tid, s)).fetchone()
            service.install_post_or_seal(self.conn, tid, seal['id'], who)
        posts = self.conn.execute(
            "SELECT id FROM task_items WHERE task_id=? AND kind='post'", (tid,)).fetchall()
        for p in posts:
            service.install_post_or_seal(self.conn, tid, p['id'], who)

    def _confirm_all(self, tid, slots, who=WORKER):
        for s in range(1, slots + 1):
            service.confirm_slot(self.conn, tid, s, 'connection', who)
            service.confirm_slot(self.conn, tid, s, 'seal', who)

    def _full_deploy(self, tid, slots, who=WORKER):
        self._install_all(tid, slots, who)
        self._confirm_all(tid, slots, who)
        service.control_channel(self.conn, tid, GUARD, True)
        service.finish_deploy(self.conn, tid, who)

    # ---------------------------------------------------------- 1. exclusivity

    def test_equipment_exclusivity_across_entrances(self):
        t1 = self._task('E1')
        locked = self._claim(t1)['locked']
        t2 = self._task('E2')
        # 第二个入口试图把已被 E1 领取的同一件挡板再领一次 → 拒绝
        item = self.conn.execute(
            "SELECT id FROM task_items WHERE task_id=? AND kind='panel' ORDER BY slot_index LIMIT 1",
            (t2,)).fetchone()
        code = [c for c in locked if c.startswith('B-')][0]
        with self.assertRaises(service.ServiceError) as cm:
            service.claim_one(self.conn, t2, item['id'], code, WORKER2)
        self.assertEqual(cm.exception.code, 'equipment_busy')
        # 器材仍只挂在 t1
        row = self.conn.execute("SELECT status,current_task_id FROM equipment WHERE code=?",
                                (code,)).fetchone()
        self.assertEqual(row['status'], 'locked')
        self.assertEqual(row['current_task_id'], t1)
        # 同入口重复生成清单也被拒绝
        with self.assertRaises(service.ServiceError):
            self._task('E1')

    def test_wrong_segment_blocked_and_fixable(self):
        t = self._task('E1')
        items = {f"{it['slot_index']}:{it['kind']}": it for it in
                 self.conn.execute("SELECT * FROM task_items WHERE task_id=?", (t,)).fetchall()}
        # 槽1、槽2 的挡板故意反领（槽1 领 B-A02、槽2 领 B-A01），其余按建议领
        planned = {(it['slot_index'], it['kind']): it['planned_code']
                   for it in items.values() if it['planned_code']}
        service.claim_one(self.conn, t, items['1:panel']['id'], 'B-A02', WORKER)
        service.claim_one(self.conn, t, items['2:panel']['id'], 'B-A01', WORKER)
        for key, it in items.items():
            if key in ('1:panel', '2:panel'):
                continue
            service.claim_one(self.conn, t, it['id'],
                              planned[(it['slot_index'], it['kind'])], WORKER)
        # 把槽2 锁定的 B-A01 扫到槽1 → 错段拦截
        with self.assertRaises(service.ServiceError) as cm:
            service.install_panel(self.conn, t, 1, 'B-A01', WORKER)
        self.assertEqual(cm.exception.code, 'wrong_segment')
        self.assertTrue(service.open_incident_exists(self.conn, t, 'wrong_segment'))
        # 纠正两槽扫反：自动交换两件的槽位锁定，槽1 装入 B-A01，异常关闭
        service.fix_wrong_segment(self.conn, t, 1, 'B-A01', WORKER)
        self.assertEqual(
            self.conn.execute(
                "SELECT locked_code FROM task_items WHERE id=?",
                (items['1:panel']['id'],)).fetchone()[0], 'B-A01')
        self.assertEqual(
            self.conn.execute(
                "SELECT locked_code FROM task_items WHERE id=?",
                (items['2:panel']['id'],)).fetchone()[0], 'B-A02')
        self.assertFalse(service.open_incident_exists(self.conn, t, 'wrong_segment'))

    # ---------------------------------------------------------- 2. segment completeness

    def test_deploy_requires_all_segments_and_control(self):
        t = self._task('E1')
        self._claim(t)
        # 未安装 → 不能完成
        with self.assertRaisesRegex(service.ServiceError, 'incomplete|分段'):
            service.finish_deploy(self.conn, t, WORKER)
        self._install_all(t, 3)
        # 只确认部分槽位 → 分段不完整
        service.confirm_slot(self.conn, t, 1, 'connection', WORKER)
        with self.assertRaisesRegex(service.ServiceError, '分段不完整'):
            service.finish_deploy(self.conn, t, WORKER)
        self._confirm_all(t, 3)
        # 齐了但没封控 → 仍不能完成
        with self.assertRaisesRegex(service.ServiceError, '封控'):
            service.finish_deploy(self.conn, t, WORKER)
        # 工人无权封控
        with self.assertRaises(service.ServiceError) as cm:
            service.control_channel(self.conn, t, WORKER, True)
        self.assertEqual(cm.exception.code, 'forbidden')
        service.control_channel(self.conn, t, GUARD, True)
        service.finish_deploy(self.conn, t, WORKER)
        self.assertEqual(
            self.conn.execute("SELECT status FROM tasks WHERE id=?", (t,)).fetchone()['status'],
            'deployed')

    # ---------------------------------------------------------- 3. alert gating

    def test_dismantle_blocked_while_alert_active(self):
        t = self._task('E1')
        self._claim(t)
        self._full_deploy(t, 3)
        service.ensure_alert(self.conn, 'orange', 15.0, '暴雨')
        with self.assertRaises(service.ServiceError) as cm:
            service.dismantle(self.conn, t, WORKER)
        self.assertEqual(cm.exception.code, 'alert_active')
        # 已登记拆除过早异常
        self.assertTrue(service.open_incident_exists(self.conn, t, 'early_dismantle'))

    def test_water_ingress_blocks_dismantle(self):
        t = self._task('E1')
        self._claim(t)
        self._full_deploy(t, 3)
        service.observe_water(self.conn, 'E1', 12.0, WORKER, '积水漫入', t)
        with self.assertRaises(service.ServiceError) as cm:
            service.dismantle(self.conn, t, WORKER)
        self.assertEqual(cm.exception.code, 'water_ingress')

    # ---------------------------------------------------------- 4. early approval

    def test_early_removal_requires_supervisor(self):
        t = self._task('E1')
        self._claim(t)
        self._full_deploy(t, 3)
        service.ensure_alert(self.conn, 'orange', 15.0, '暴雨')
        service.request_early_removal(self.conn, t, WORKER, '业主车辆急需离场')
        # 保安不能审批
        with self.assertRaises(service.ServiceError) as cm:
            service.decide_early_removal(self.conn, t, GUARD, True)
        self.assertEqual(cm.exception.code, 'forbidden')
        # 主管批准后预警未解除也可拆除
        service.decide_early_removal(self.conn, t, SUP, True)
        service.dismantle(self.conn, t, WORKER)
        self.assertEqual(
            self.conn.execute("SELECT status FROM tasks WHERE id=?", (t,)).fetchone()['status'],
            'dismantled')

    def test_early_removal_rejected(self):
        t = self._task('E1')
        self._claim(t)
        self._full_deploy(t, 3)
        service.ensure_alert(self.conn, 'orange', 15.0, '暴雨')
        service.request_early_removal(self.conn, t, WORKER, '理由不充分')
        service.decide_early_removal(self.conn, t, SUP, False)
        with self.assertRaises(service.ServiceError) as cm:
            service.dismantle(self.conn, t, WORKER)
        self.assertEqual(cm.exception.code, 'alert_active')

    def test_normal_dismantle_after_clear(self):
        t = self._task('E1')
        self._claim(t)
        self._full_deploy(t, 3)
        service.ensure_alert(self.conn, 'orange', 15.0, '暴雨')
        service.clear_alert(self.conn, '雨停复查')
        service.dismantle(self.conn, t, WORKER)  # 不抛异常即通过

    # ---------------------------------------------------------- 5. return differences

    def _to_return_check(self, tid, slots):
        self._claim(tid)
        self._full_deploy(tid, slots)
        service.dismantle(self.conn, tid, WORKER)
        service.wash(self.conn, tid, WORKER)
        service.dry_finish(self.conn, tid, WORKER)

    def test_return_shortage_blocks_completion(self):
        t = self._task('E2')
        self._to_return_check(t, 2)
        items = self.conn.execute(
            "SELECT * FROM task_items WHERE task_id=? ORDER BY id", (t,)).fetchall()
        for it in items[:-1]:
            service.return_item(self.conn, t, it['id'], 'ok', WORKER)
        # 最后一件密封条缺件
        service.return_item(self.conn, t, items[-1]['id'], 'missing', WORKER, '清点缺失')
        with self.assertRaisesRegex(service.ServiceError, '差异'):
            service.complete_return(self.conn, t, WORKER)
        # 主管审批 + 补充入库后闭环
        req = self.conn.execute(
            "SELECT id FROM supply_requests WHERE task_id=? AND status='requested'",
            (t,)).fetchone()
        service.approve_supply(self.conn, req['id'], SUP)
        new_code = service.restock(self.conn, req['id'], WORKER)
        service.complete_return(self.conn, t, WORKER)
        # 归架完好的器材解除占用、重新可用
        ok_codes = [it['locked_code'] for it in items[:-1]]
        placeholders = ','.join('?' * len(ok_codes))
        avail = self.conn.execute(
            f"SELECT COUNT(*) c FROM equipment WHERE code IN ({placeholders}) "
            "AND status='available' AND current_task_id IS NULL", ok_codes).fetchone()['c']
        self.assertEqual(avail, len(items) - 1)
        self.assertTrue(
            self.conn.execute("SELECT 1 FROM equipment WHERE code=? AND status='available'",
                              (new_code,)).fetchone())

    def test_damaged_seal_writeoff_flow(self):
        t = self._task('E2')
        self._to_return_check(t, 2)
        items = self.conn.execute(
            "SELECT * FROM task_items WHERE task_id=? AND kind='seal' ORDER BY slot_index",
            (t,)).fetchall()
        code = items[0]['locked_code']
        service.return_item(self.conn, t, items[0]['id'], 'damaged', WORKER, '老化开裂')
        req = self.conn.execute(
            "SELECT id FROM supply_requests WHERE equipment_code=? AND reason='damage_writeoff'",
            (code,)).fetchone()
        # 非主管不能审批报损
        with self.assertRaises(service.ServiceError) as cm:
            service.approve_supply(self.conn, req['id'], WORKER)
        self.assertEqual(cm.exception.code, 'forbidden')
        service.approve_supply(self.conn, req['id'], SUP)
        self.assertEqual(
            self.conn.execute("SELECT status FROM equipment WHERE code=?", (code,)).fetchone()['status'],
            'written_off')

    def test_install_time_seal_damage_auto_request(self):
        t = self._task('E1')
        self._claim(t)
        r = service.damage_seal(self.conn, t, 1, WORKER)
        self.assertEqual(
            self.conn.execute("SELECT status FROM equipment WHERE code=?", (r['old'],)).fetchone()['status'],
            'damaged')
        req = self.conn.execute(
            "SELECT id,status FROM supply_requests WHERE equipment_code=? AND reason='damage_writeoff'",
            (r['old'],)).fetchone()
        self.assertIsNotNone(req)
        self.assertEqual(
            self.conn.execute("SELECT status FROM equipment WHERE code=?",
                              (r['replacement'],)).fetchone()['status'], 'installed')

    # ---------------------------------------------------------- 6. concurrent finish once

    def test_concurrent_completion_only_once(self):
        t = self._task('E2')
        self._to_return_check(t, 2)
        items = self.conn.execute(
            "SELECT id FROM task_items WHERE task_id=?", (t,)).fetchall()
        for it in items:
            service.return_item(self.conn, t, it['id'], 'ok', WORKER)
        results = []

        def finish():
            c = connect(self.path)
            try:
                service.complete_return(c, t, WORKER)
                results.append('ok')
            except service.ServiceError as e:
                results.append(e.code)
            finally:
                c.close()

        threads = [threading.Thread(target=finish) for _ in range(2)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        self.assertEqual(sorted(results), sorted(['ok', 'already_completed']))
        self.assertEqual(
            self.conn.execute("SELECT status FROM tasks WHERE id=?", (t,)).fetchone()['status'],
            'completed')

    # ---------------------------------------------------------- temp pass & crew

    def test_temp_pass_opens_and_restores(self):
        t = self._task('E1')
        self._claim(t)
        self._install_all(t, 3)
        self._confirm_all(t, 3)
        service.control_channel(self.conn, t, GUARD, True)
        pid = service.open_temp_pass(self.conn, t, GUARD, '救护车通行')
        self.assertEqual(
            self.conn.execute("SELECT channel_controlled FROM tasks WHERE id=?", (t,)).fetchone()[0], 0)
        # 放行未恢复不能重新封控
        with self.assertRaises(service.ServiceError) as cm:
            service.control_channel(self.conn, t, GUARD, True)
        self.assertEqual(cm.exception.code, 'temp_pass_open')
        service.restore_pass(self.conn, pid, GUARD)
        service.control_channel(self.conn, t, GUARD, True)

    def test_late_arrival_incident(self):
        t = self._task('E1')
        self.conn.execute(
            "INSERT INTO task_personnel (task_id,person_id,duty,due_at) VALUES (?,?, 'install', ?)",
            (t, WORKER2, '2020-01-01 08:00:00'))
        self.conn.commit()
        r = service.mark_arrival(self.conn, t, WORKER2, '2020-01-01 08:12:00')
        self.assertTrue(r['late'])
        self.assertTrue(service.open_incident_exists(self.conn, t, 'personnel_late'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
