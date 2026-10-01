"""演示种子：人员、入口、器材，以及部署 / 缺件 / 提前解除三类样例。"""
from __future__ import annotations

from . import service
from .db import connect, init_db

PERSONNEL = [
    (1, '周建国', 'guard', '138-0000-0001'),
    (2, '李海涛', 'worker', '138-0000-0002'),
    (3, '王大山', 'worker', '138-0000-0003'),
    (4, '陈立群', 'supervisor', '138-0000-0004'),
]

ENTRANCES = [
    # id, 名称, 槽数, 黄/橙/红阈值, 汇水增益, 滞后
    ('E1', '东门（主入口）', 4, 5, 10, 20, 20, 0, 1),
    ('E2', '北门（坡道入口）', 3, 5, 10, 20, 22, 4, 2),
    ('E3', '西门（人行口）', 2, 4, 8, 16, 12, 2, 3),
]


def seed(conn, scenarios=True):
    init_db(conn)
    for pid, name, role, phone in PERSONNEL:
        conn.execute("INSERT OR IGNORE INTO personnel (id,name,role,phone) VALUES (?,?,?,?)",
                     (pid, name, role, phone))
    existing = conn.execute("SELECT COUNT(*) c FROM entrances").fetchone()['c']
    if existing:
        return
    for e in ENTRANCES:
        conn.execute(
            "INSERT INTO entrances (id,name,slot_count,warn_cm,orange_cm,red_cm,gain,lag,"
            "sort_order) VALUES (?,?,?,?,?,?,?,?,?)", e)

    # 器材：挡板 14、立柱 14、密封条 12，预留错段/更换演示余量
    for i in range(1, 15):
        conn.execute(
            "INSERT INTO equipment (code,kind,name,shelf,status) VALUES (?,?,?,?,'available')",
            (f'B-A{i:02d}', 'panel', f'挡板 {i:02d}', f'A架-{i:02d}'))
    for i in range(1, 15):
        conn.execute(
            "INSERT INTO equipment (code,kind,name,shelf,status) VALUES (?,?,?,?,'available')",
            (f'P-{i:02d}', 'post', f'立柱 {i:02d}', f'B架-{i:02d}'))
    for i in range(1, 13):
        conn.execute(
            "INSERT INTO equipment (code,kind,name,shelf,status) VALUES (?,?,?,?,'available')",
            (f'S-{i:02d}', 'seal', f'密封条 {i:02d}', f'C架-{i:02d}'))
    conn.commit()

    if scenarios:
        # 顺序重要：缺件样例先走完拆除（此时尚无预警）；
        # 随后建立橙色预警，部署样例与提前解除样例都挂在该预警下
        _scenario_shortage(conn)
        _scenario_deploy(conn)
        _scenario_early(conn)


def _scenario_deploy(conn):
    """样例1：东门已领取并装到槽1的在途部署任务，等值班人员继续。"""
    ts = service.now()
    alert_id, _ = service.ensure_alert(conn, 'orange', 14.0,
                                       '样例：暴雨橙色预警', ts)
    tid = service.generate_checklist(conn, 'E1', alert_id, ts)
    service.claim_plan(conn, tid, 2, ts)
    p1 = conn.execute(
        "SELECT id, locked_code FROM task_items WHERE task_id=? AND slot_index=1 AND kind='panel'",
        (tid,)).fetchone()
    service.install_panel(conn, tid, 1, p1['locked_code'], 2, ts)
    seal1 = conn.execute(
        "SELECT id FROM task_items WHERE task_id=? AND slot_index=1 AND kind='seal'",
        (tid,)).fetchone()
    service.install_post_or_seal(conn, tid, seal1['id'], 2, ts)
    post0 = conn.execute(
        "SELECT id FROM task_items WHERE task_id=? AND slot_index=0 AND kind='post'",
        (tid,)).fetchone()
    service.install_post_or_seal(conn, tid, post0['id'], 2, ts)
    service.confirm_slot(conn, tid, 1, 'connection', 2, ts)
    service.confirm_slot(conn, tid, 1, 'seal', 2, ts)
    conn.execute(
        "INSERT INTO task_personnel (task_id,person_id,duty,due_at,arrived_at) "
        "VALUES (?,?, 'install', ?, ?)", (tid, 3, ts, ts))


def _scenario_shortage(conn):
    """样例2：西门任务走完部署→拆除→清洗晾干→归架核对，缺 1 条密封条待主管审批补充。"""
    ts = service.now()
    tid = service.generate_checklist(conn, 'E3', None, ts)
    service.claim_plan(conn, tid, 3, ts)
    items = conn.execute(
        "SELECT * FROM task_items WHERE task_id=? ORDER BY slot_index, kind", (tid,)).fetchall()
    for it in items:
        if it['kind'] == 'panel':
            service.install_panel(conn, tid, it['slot_index'], it['locked_code'], 3, ts)
        else:
            service.install_post_or_seal(conn, tid, it['id'], 3, ts)
    n = conn.execute("SELECT slot_count FROM entrances WHERE id='E3'").fetchone()[0]
    for slot in range(1, n + 1):
        service.confirm_slot(conn, tid, slot, 'connection', 3, ts)
        service.confirm_slot(conn, tid, slot, 'seal', 3, ts)
    service.control_channel(conn, tid, 1, True, ts)
    service.finish_deploy(conn, tid, 3, ts)
    service.dismantle(conn, tid, 3, ts)
    service.wash(conn, tid, 3, ts)
    service.dry_finish(conn, tid, 3, ts)
    for it in items[:-1]:
        service.return_item(conn, tid, it['id'], 'ok', 3, '', ts)
    last = items[-1]
    service.return_item(conn, tid, last['id'], 'missing', 3, '样例：归架清点缺 1 条密封条', ts)


def _scenario_early(conn):
    """样例3：北门已部署完毕、通道封控，预警仍在，提前拆除申请等待主管审批。"""
    ts = service.now()
    tid = service.generate_checklist(conn, 'E2', None, ts)
    service.claim_plan(conn, tid, 2, ts)
    items = conn.execute(
        "SELECT * FROM task_items WHERE task_id=? ORDER BY slot_index, kind", (tid,)).fetchall()
    for it in items:
        if it['kind'] == 'panel':
            service.install_panel(conn, tid, it['slot_index'], it['locked_code'], 2, ts)
        else:
            service.install_post_or_seal(conn, tid, it['id'], 2, ts)
    n = conn.execute("SELECT slot_count FROM entrances WHERE id='E2'").fetchone()[0]
    for slot in range(1, n + 1):
        service.confirm_slot(conn, tid, slot, 'connection', 2, ts)
        service.confirm_slot(conn, tid, slot, 'seal', 2, ts)
    service.control_channel(conn, tid, 1, True, ts)
    service.finish_deploy(conn, tid, 2, ts)
    service.request_early_removal(conn, tid, 2,
                                  '样例：雨已停、业主车辆急需离场，申请提前拆除', ts)


if __name__ == '__main__':
    import sys
    db = sys.argv[1] if len(sys.argv) > 1 else 'data/flood.db'
    c = connect(db)
    seed(c)
    print('seeded ->', db)
