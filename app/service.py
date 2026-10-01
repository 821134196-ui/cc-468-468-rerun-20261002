"""Domain services: locking, staged install, dismantle approval, return reconcile."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

ACTIVE_TASK_STATUSES = (
    'created', 'claimed', 'installing', 'deployed',
    'dismantled', 'cleaning', 'drying', 'return_check',
)
INSTALLABLE = ('created', 'claimed', 'installing')
POST_STATUSES = ('dismantled', 'cleaning', 'drying', 'return_check')


class ServiceError(Exception):
    def __init__(self, message: str, code: str = 'error'):
        super().__init__(message)
        self.code = code


def now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


# ---------------------------------------------------------------- personnel

ROLE_NAMES = {'guard': '保安', 'worker': '安装工', 'supervisor': '物业主管'}


def get_person(conn: sqlite3.Connection, person_id: int) -> sqlite3.Row:
    p = conn.execute("SELECT * FROM personnel WHERE id=?", (person_id,)).fetchone()
    if not p:
        raise ServiceError('人员不存在', 'no_person')
    return p


def require_role(conn: sqlite3.Connection, person_id: int, *roles: str) -> sqlite3.Row:
    p = get_person(conn, person_id)
    if roles and p['role'] not in roles:
        names = '、'.join(ROLE_NAMES[r] for r in roles)
        raise ServiceError(f"该操作需要{names}权限（{p['name']}是{ROLE_NAMES[p['role']]}）",
                           'forbidden')
    return p


def mark_arrival(conn, task_id, person_id, ts=None):
    """人员到场；晚于约定时间记迟到并开异常单。"""
    ts = ts or now()
    link = conn.execute(
        "SELECT * FROM task_personnel WHERE task_id=? AND person_id=?",
        (task_id, person_id)).fetchone()
    if not link:
        raise ServiceError('该人员未被派工到此任务', 'not_assigned')
    if link['arrived_at']:
        raise ServiceError('已登记到场', 'already_arrived')
    late = bool(link['due_at'] and ts > link['due_at'])
    conn.execute("UPDATE task_personnel SET arrived_at=?, late=? WHERE id=?",
                 (ts, int(late), link['id']))
    incident_id = None
    if late:
        person = get_person(conn, person_id)
        incident_id = add_incident(
            conn, task_id, 'personnel_late',
            detail=f"{person['name']} 迟到（约定 {link['due_at']}，实际 {ts}）",
            raised_by=person_id, ts=ts)
    conn.commit()
    return {'late': late, 'incident_id': incident_id}


# ---------------------------------------------------------------- incidents

def add_incident(conn, task_id, itype, detail='', raised_by=None, ts=None,
                 entrance_id=None):
    ts = ts or now()
    if task_id and not entrance_id:
        entrance_id = conn.execute("SELECT entrance_id FROM tasks WHERE id=?",
                                   (task_id,)).fetchone()[0]
    cur = conn.execute(
        """INSERT INTO incidents (task_id, entrance_id, type, detail, raised_by, created_at)
           VALUES (?,?,?,?,?,?)""",
        (task_id, entrance_id, itype, detail, raised_by, ts))
    return cur.lastrowid


def open_incident_exists(conn, task_id, itype):
    row = conn.execute(
        "SELECT COUNT(*) c FROM incidents WHERE task_id=? AND type=? AND status='open'",
        (task_id, itype)).fetchone()
    return row['c'] > 0


def resolve_incident(conn, incident_id, person_id, resolution, ts=None):
    ts = ts or now()
    require_role(conn, person_id, 'guard', 'worker', 'supervisor')
    inc = conn.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
    if not inc:
        raise ServiceError('异常单不存在', 'no_incident')
    if inc['status'] == 'handled':
        raise ServiceError('异常单已处理', 'already_handled')
    conn.execute(
        "UPDATE incidents SET status='handled', handled_by=?, resolution=?, handled_at=? WHERE id=?",
        (person_id, resolution, ts, incident_id))
    conn.commit()


# ---------------------------------------------------------------- equipment

def _lock_equipment(conn, code, task_id):
    """条件更新：只有 available 才能锁定，保证跨入口排他。"""
    cur = conn.execute(
        """UPDATE equipment SET status='locked', current_task_id=?, version=version+1
           WHERE code=? AND status='available'""",
        (task_id, code))
    if cur.rowcount == 0:
        row = conn.execute("SELECT status, current_task_id FROM equipment WHERE code=?",
                           (code,)).fetchone()
        if not row:
            raise ServiceError(f'器材 {code} 不存在', 'no_equipment')
        if row['status'] in ('locked', 'installed'):
            other = conn.execute(
                "SELECT entrance_id FROM tasks WHERE id=?",
                (row['current_task_id'],)).fetchone()
            where = f"（已被入口 {other[0]} 的任务领取）" if other else ''
            raise ServiceError(f'器材 {code} 已被锁定{where}，不能重复领取', 'equipment_busy')
        raise ServiceError(f"器材 {code} 当前状态 {row['status']}，不可领取",
                           'equipment_busy')


def _unlock(conn, code, status='available'):
    conn.execute(
        """UPDATE equipment SET status=?, current_task_id=NULL, version=version+1
           WHERE code=?""", (status, code))


def _pick_available(conn, kind, exclude):
    row = conn.execute(
        """SELECT code FROM equipment
           WHERE kind=? AND status='available' AND code NOT IN ({})
           ORDER BY code LIMIT 1""".format(','.join('?' * len(exclude)) if exclude else "''"),
        (kind, *exclude) if exclude else (kind,)).fetchone()
    return row['code'] if row else None


# ---------------------------------------------------------------- checklist

def generate_checklist(conn, entrance_id, alert_id, ts=None):
    """预警达阈值后按入口生成部署清单；同入口已有在途任务则不重复生成。"""
    ts = ts or now()
    ent = conn.execute("SELECT * FROM entrances WHERE id=?", (entrance_id,)).fetchone()
    if not ent:
        raise ServiceError('入口不存在', 'no_entrance')
    dup = conn.execute(
        "SELECT id FROM tasks WHERE entrance_id=? AND status IN (%s)"
        % ','.join('?' * len(ACTIVE_TASK_STATUSES)),
        (entrance_id, *ACTIVE_TASK_STATUSES)).fetchone()
    if dup:
        raise ServiceError(f'入口 {ent["name"]} 已存在在途任务 #{dup["id"]}', 'task_exists')

    cur = conn.execute(
        """INSERT INTO tasks (entrance_id, alert_id, status, created_at)
           VALUES (?,?, 'created', ?)""", (entrance_id, alert_id, ts))
    task_id = cur.lastrowid

    n = ent['slot_count']
    used: set[str] = set()
    for slot in range(1, n + 1):
        panel = _pick_available(conn, 'panel', used)
        if panel:
            used.add(panel)
        seal = _pick_available(conn, 'seal', used)
        if seal:
            used.add(seal)
        conn.execute(
            """INSERT INTO task_items (task_id, slot_index, pos_label, kind, planned_code)
               VALUES (?,?,?,?,?)""",
            (task_id, slot, f'槽{slot}', 'panel', panel))
        conn.execute(
            """INSERT INTO task_items (task_id, slot_index, pos_label, kind, planned_code)
               VALUES (?,?,?,?,?)""",
            (task_id, slot, f'槽{slot}密封条', 'seal', seal))
    for post_idx in range(0, n + 1):
        post = _pick_available(conn, 'post', used)
        if post:
            used.add(post)
        conn.execute(
            """INSERT INTO task_items (task_id, slot_index, pos_label, kind, planned_code)
               VALUES (?,?,?,?,?)""",
            (task_id, post_idx, f'立柱{post_idx}', 'post', post))
    conn.commit()
    return task_id


def _get_task(conn, task_id):
    t = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not t:
        raise ServiceError('任务不存在', 'no_task')
    return t


def claim_plan(conn, task_id, person_id, ts=None):
    """按清单建议整单领取：原子锁定全部挡板/立柱/密封条。"""
    ts = ts or now()
    require_role(conn, person_id, 'worker', 'supervisor')
    task = _get_task(conn, task_id)
    if task['status'] not in ('created', 'claimed'):
        raise ServiceError('任务已离开待领取阶段', 'bad_status')
    items = conn.execute(
        "SELECT * FROM task_items WHERE task_id=? AND locked_code IS NULL ORDER BY id",
        (task_id,)).fetchall()
    missing = [it['pos_label'] for it in items if not it['planned_code']]
    if missing:
        raise ServiceError('库存不足，无法配齐：' + '、'.join(missing), 'out_of_stock')
    try:
        for it in items:
            _lock_equipment(conn, it['planned_code'], task_id)
            conn.execute("UPDATE task_items SET locked_code=? WHERE id=?",
                         (it['planned_code'], it['id']))
    except ServiceError:
        conn.rollback()
        raise
    if task['status'] == 'created':
        conn.execute("UPDATE tasks SET status='claimed' WHERE id=?", (task_id,))
    conn.commit()
    return {'locked': [it['planned_code'] for it in items]}


def claim_one(conn, task_id, item_id, code, person_id, ts=None):
    """单独领一件（用于演示把别的挡板领到某槽 → 错段）。"""
    ts = ts or now()
    require_role(conn, person_id, 'worker', 'supervisor')
    task = _get_task(conn, task_id)
    if task['status'] not in INSTALLABLE:
        raise ServiceError('当前任务阶段不能领件', 'bad_status')
    it = conn.execute("SELECT * FROM task_items WHERE id=? AND task_id=?",
                      (item_id, task_id)).fetchone()
    if not it:
        raise ServiceError('清单明细不存在', 'no_item')
    if it['locked_code']:
        raise ServiceError(f"{it['pos_label']} 已领取 {it['locked_code']}", 'already_locked')
    eq = conn.execute("SELECT * FROM equipment WHERE code=?", (code,)).fetchone()
    if not eq or eq['kind'] != it['kind']:
        raise ServiceError(f'{code} 不是需要的{it["kind"]}', 'wrong_kind')
    _lock_equipment(conn, code, task_id)
    conn.execute("UPDATE task_items SET locked_code=? WHERE id=?", (code, it['id']))
    if task['status'] == 'created':
        conn.execute("UPDATE tasks SET status='claimed' WHERE id=?", (task_id,))
    conn.commit()


# ---------------------------------------------------------------- install

def _slot_item(conn, task_id, slot, kind):
    it = conn.execute(
        "SELECT * FROM task_items WHERE task_id=? AND slot_index=? AND kind=?",
        (task_id, slot, kind)).fetchone()
    if not it:
        raise ServiceError(f'槽{slot}的{kind}明细不存在', 'no_item')
    return it


def install_panel(conn, task_id, slot, code, person_id, ts=None):
    """安装扫码：扫到的挡板必须是该槽锁定件；错段开异常单，需纠正后才能安装。"""
    ts = ts or now()
    require_role(conn, person_id, 'worker', 'supervisor')
    task = _get_task(conn, task_id)
    if task['status'] not in INSTALLABLE:
        raise ServiceError('任务不在安装阶段', 'bad_status')
    it = _slot_item(conn, task_id, slot, 'panel')
    if not it['locked_code']:
        raise ServiceError(f'槽{slot}挡板尚未领取', 'not_locked')
    if code == it['locked_code']:
        conn.execute(
            "UPDATE task_items SET installed_at=COALESCE(installed_at, ?) WHERE id=?",
            (ts, it['id']))
        conn.execute("UPDATE equipment SET status='installed' WHERE code=?", (code,))
        conn.execute("UPDATE tasks SET status='installing' WHERE id=? AND status='claimed'",
                     (task_id,))
        conn.execute(
            "UPDATE incidents SET status='handled', handled_at=?, resolution=? "
            "WHERE task_id=? AND type='wrong_segment' AND status='open'",
            (ts, f'已纠正：槽{slot} 装回 {code}', task_id))
        conn.commit()
        return {'matched': True}
    # 错段
    eq = conn.execute("SELECT * FROM equipment WHERE code=?", (code,)).fetchone()
    if not eq:
        raise ServiceError(f'器材 {code} 不存在', 'no_equipment')
    if eq['kind'] != 'panel':
        raise ServiceError(f'{code} 不是挡板', 'wrong_kind')
    add_incident(conn, task_id, 'wrong_segment',
                 detail=f"槽{slot} 扫码为 {code}，应装 {it['locked_code']}（挡板错段）",
                 raised_by=person_id, ts=ts)
    conn.commit()
    raise ServiceError(
        f"挡板错段：{code} 不属于槽{slot}（应为 {it['locked_code']}），已登记异常，"
        '请核对拼接顺序后纠正', 'wrong_segment')


def fix_wrong_segment(conn, task_id, slot, code, person_id, ts=None):
    """纠正错段：把扫到的挡板换锁到该槽，原锁定件退回货架（仅限可用器材）。"""
    ts = ts or now()
    require_role(conn, person_id, 'worker', 'supervisor')
    task = _get_task(conn, task_id)
    if task['status'] not in INSTALLABLE:
        raise ServiceError('任务不在安装阶段', 'bad_status')
    it = _slot_item(conn, task_id, slot, 'panel')
    old = it['locked_code']
    eq = conn.execute("SELECT * FROM equipment WHERE code=?", (code,)).fetchone()
    if not eq or eq['kind'] != 'panel':
        raise ServiceError(f'{code} 不是挡板', 'wrong_kind')
    if code == old:
        raise ServiceError('该挡板本就属于此槽，无需纠正', 'no_change')
    if eq['current_task_id'] not in (None, task_id):
        raise ServiceError(f'{code} 属于其他任务，不能换入', 'equipment_busy')
    if eq['current_task_id'] == task_id:
        # 同任务两槽挡板扫反（无论是否已装）：交换两件的槽位锁定。
        # 挡板身份改变，另一槽的就位与逐段确认标记作废，须重新安装、重新确认。
        other = conn.execute(
            "SELECT * FROM task_items WHERE kind='panel' AND locked_code=? AND task_id=?",
            (code, task_id)).fetchone()
        if not other or other['id'] == it['id']:
            raise ServiceError(f'{code} 未被本任务其他槽位持有，无法交换', 'equipment_busy')
        conn.execute(
            """UPDATE task_items SET locked_code=?, installed_at=NULL,
               connection_confirmed_at=NULL, connection_confirmed_by=NULL,
               seal_confirmed_at=NULL, seal_confirmed_by=NULL WHERE id=?""",
            (old, other['id']))
        # 换出到另一槽的旧件身份变了，该槽需重新安装：旧件回到 locked
        if old:
            conn.execute("UPDATE equipment SET status='locked' WHERE code=?", (old,))
    elif eq['status'] == 'available':
        _lock_equipment(conn, code, task_id)
        if old:
            _unlock(conn, old)
    else:
        raise ServiceError(f'{code} 状态为 {eq["status"]}，不能换入', 'equipment_busy')
    conn.execute("UPDATE task_items SET locked_code=? WHERE id=?", (code, it['id']))
    conn.execute(
        "UPDATE task_items SET installed_at=? WHERE id=?", (ts, it['id']))
    conn.execute("UPDATE equipment SET status='installed' WHERE code=?", (code,))
    inc = conn.execute(
        """UPDATE incidents SET status='handled', handled_by=?, handled_at=?,
           resolution=? WHERE type='wrong_segment' AND task_id=? AND status='open'""",
        (person_id, ts, f'已纠正：槽{slot} 换装 {code}', task_id))
    conn.execute("UPDATE tasks SET status='installing' WHERE id=? AND status='claimed'",
                 (task_id,))
    conn.commit()
    return {'locked_code': code, 'incidents_closed': inc.rowcount}


def install_post_or_seal(conn, task_id, item_id, person_id, ts=None):
    """立柱/密封条就位（密封条可能在安装时发现破损 → damage_seal 更换）。"""
    ts = ts or now()
    require_role(conn, person_id, 'worker', 'supervisor')
    task = _get_task(conn, task_id)
    if task['status'] not in INSTALLABLE:
        raise ServiceError('任务不在安装阶段', 'bad_status')
    it = conn.execute("SELECT * FROM task_items WHERE id=? AND task_id=?",
                      (item_id, task_id)).fetchone()
    if not it:
        raise ServiceError('明细不存在', 'no_item')
    if it['kind'] not in ('post', 'seal'):
        raise ServiceError('该接口只用于立柱/密封条就位', 'wrong_kind')
    if not it['locked_code']:
        raise ServiceError(f"{it['pos_label']}尚未领取", 'not_locked')
    eq = conn.execute("SELECT * FROM equipment WHERE code=?", (it['locked_code'],)).fetchone()
    if eq['status'] == 'damaged':
        raise ServiceError(f"{it['locked_code']} 已报破损，请先更换", 'damaged')
    conn.execute("UPDATE task_items SET installed_at=COALESCE(installed_at,?) WHERE id=?",
                 (ts, it['id']))
    conn.execute("UPDATE equipment SET status='installed' WHERE code=?", (it['locked_code'],))
    conn.commit()


def damage_seal(conn, task_id, slot, person_id, replacement_code=None, ts=None):
    """安装中发现密封条破损：旧件转破损并自动报损，从库存换一条同槽继续安装。"""
    ts = ts or now()
    require_role(conn, person_id, 'worker', 'supervisor')
    task = _get_task(conn, task_id)
    if task['status'] not in INSTALLABLE and task['status'] not in POST_STATUSES:
        raise ServiceError('当前阶段不能处理密封条', 'bad_status')
    it = _slot_item(conn, task_id, slot, 'seal')
    old = it['locked_code']
    if old:
        conn.execute(
            """UPDATE equipment SET status='damaged', version=version+1 WHERE code=?""",
            (old,))
        conn.execute(
            "INSERT INTO supply_requests (task_id, kind, equipment_code, reason, quantity, "
            "requested_by, note, created_at) VALUES (?,?,?,'damage_writeoff',1,?,?,?)",
            (task_id, 'seal', old, person_id, f'槽{slot}密封条安装时发现破损，待主管报损', ts))
    add_incident(conn, task_id, 'seal_damaged',
                 detail=f"槽{slot}密封条 {old or '(未领)'} 破损，已更换",
                 raised_by=person_id, ts=ts)
    exclude = {r['locked_code'] for r in conn.execute(
        "SELECT locked_code FROM task_items WHERE task_id=? AND locked_code IS NOT NULL",
        (task_id,))}
    new_code = replacement_code or _pick_available(conn, 'seal', exclude)
    if not new_code:
        conn.commit()
        raise ServiceError('库存已无可用密封条，缺件待补充', 'out_of_stock')
    _lock_equipment(conn, new_code, task_id)
    conn.execute(
        "UPDATE task_items SET locked_code=?, installed_at=? WHERE id=?",
        (new_code, ts, it['id']))
    conn.execute("UPDATE equipment SET status='installed' WHERE code=?", (new_code,))
    conn.commit()
    return {'old': old, 'replacement': new_code}


def confirm_slot(conn, task_id, slot, check, person_id, ts=None):
    """逐段确认 connection（连接）/ seal（密封）。错段或未安装不得确认。"""
    ts = ts or now()
    require_role(conn, person_id, 'worker', 'supervisor')
    task = _get_task(conn, task_id)
    if task['status'] not in INSTALLABLE:
        raise ServiceError('任务不在安装阶段', 'bad_status')
    panel = _slot_item(conn, task_id, slot, 'panel')
    seal = _slot_item(conn, task_id, slot, 'seal')
    if not panel['installed_at']:
        raise ServiceError(f'槽{slot}挡板未安装，不能确认', 'not_installed')
    if not seal['installed_at']:
        raise ServiceError(f'槽{slot}密封条未就位，不能确认', 'not_installed')
    if check == 'connection':
        conn.execute(
            "UPDATE task_items SET connection_confirmed_at=?, connection_confirmed_by=? WHERE id=?",
            (ts, person_id, panel['id']))
    elif check == 'seal':
        conn.execute(
            "UPDATE task_items SET seal_confirmed_at=?, seal_confirmed_by=? WHERE id=?",
            (ts, person_id, panel['id']))
    else:
        raise ServiceError('确认项只能是 connection / seal', 'bad_check')
    conn.commit()


def control_channel(conn, task_id, person_id, controlled, ts=None):
    """通道封控/解封；保安或主管可执行。临时放行期间不得保持封控状态。"""
    ts = ts or now()
    require_role(conn, person_id, 'guard', 'supervisor')
    task = _get_task(conn, task_id)
    if task['status'] not in ('installing', 'deployed') and controlled:
        raise ServiceError('只有安装中/已部署任务可以封控通道', 'bad_status')
    if controlled:
        open_pass = conn.execute(
            "SELECT COUNT(*) c FROM temp_passes WHERE task_id=? AND restored_at IS NULL",
            (task_id,)).fetchone()['c']
        if open_pass:
            raise ServiceError('存在未恢复的临时放行，不能封控', 'temp_pass_open')
        conn.execute(
            "UPDATE tasks SET channel_controlled=1, controlled_at=COALESCE(controlled_at,?), "
            "controlled_by=? WHERE id=?", (ts, person_id, task_id))
    else:
        conn.execute(
            "UPDATE tasks SET channel_controlled=0 WHERE id=?", (task_id,))
    conn.commit()


def _all_slots_ready(conn, task_id):
    """逐段完整性：每块挡板已装、连接与密封均确认；立柱/密封条均就位。"""
    problems = []
    for it in conn.execute("SELECT * FROM task_items WHERE task_id=?", (task_id,)):
        if not it['locked_code']:
            problems.append(f"{it['pos_label']}未领取")
        elif not it['installed_at']:
            problems.append(f"{it['pos_label']}未就位")
        if it['kind'] == 'panel':
            if not it['connection_confirmed_at']:
                problems.append(f"槽{it['slot_index']}连接未确认")
            if not it['seal_confirmed_at']:
                problems.append(f"槽{it['slot_index']}密封未确认")
    return problems


def finish_deploy(conn, task_id, person_id, ts=None):
    """安装完成：全部槽位连接/密封逐段确认 + 通道封控，方可置为 deployed。"""
    ts = ts or now()
    require_role(conn, person_id, 'worker', 'supervisor')
    task = _get_task(conn, task_id)
    if task['status'] == 'deployed':
        raise ServiceError('任务已部署完成', 'already_deployed')
    if task['status'] not in INSTALLABLE:
        raise ServiceError('任务不在安装阶段', 'bad_status')
    problems = _all_slots_ready(conn, task_id)
    if problems:
        raise ServiceError('分段不完整，不能完成部署：' + '、'.join(problems[:6]),
                           'incomplete')
    if not task['channel_controlled']:
        raise ServiceError('通道尚未封控（请保安执行封控），不能完成部署',
                           'not_controlled')
    conn.execute("UPDATE tasks SET status='deployed', deployed_at=? WHERE id=?",
                 (ts, task_id))
    conn.commit()


# ---------------------------------------------------------------- temp pass

def open_temp_pass(conn, task_id, person_id, reason, ts=None):
    ts = ts or now()
    require_role(conn, person_id, 'guard', 'supervisor')
    task = _get_task(conn, task_id)
    if task['status'] not in ('installing', 'deployed'):
        raise ServiceError('只有部署中的入口可以办理临时放行', 'bad_status')
    opened = conn.execute(
        "SELECT COUNT(*) c FROM temp_passes WHERE task_id=? AND restored_at IS NULL",
        (task_id,)).fetchone()['c']
    if opened:
        raise ServiceError('已有一笔未恢复的临时放行', 'pass_open')
    cur = conn.execute(
        "INSERT INTO temp_passes (task_id, reason, opened_by, opened_at) VALUES (?,?,?,?)",
        (task_id, reason, person_id, ts))
    conn.execute("UPDATE tasks SET channel_controlled=0 WHERE id=?", (task_id,))
    add_incident(conn, task_id, 'temp_vehicle_pass',
                 detail=f'临时车辆放行：{reason}', raised_by=person_id, ts=ts)
    conn.commit()
    return cur.lastrowid


def restore_pass(conn, pass_id, person_id, ts=None):
    ts = ts or now()
    require_role(conn, person_id, 'guard', 'supervisor')
    p = conn.execute("SELECT * FROM temp_passes WHERE id=?", (pass_id,)).fetchone()
    if not p:
        raise ServiceError('放行记录不存在', 'no_pass')
    if p['restored_at']:
        raise ServiceError('该放行已恢复', 'already_restored')
    conn.execute("UPDATE temp_passes SET restored_by=?, restored_at=? WHERE id=?",
                 (person_id, ts, pass_id))
    conn.execute(
        """UPDATE incidents SET status='handled', handled_by=?, handled_at=?,
           resolution='车辆已通过，封控恢复'
           WHERE task_id=? AND type='temp_vehicle_pass' AND status='open'""",
        (person_id, ts, p['task_id']))
    conn.commit()


# ---------------------------------------------------------------- water

def observe_water(conn, entrance_id, level_cm, person_id, note='', task_id=None, ts=None):
    ts = ts or now()
    p = get_person(conn, person_id)
    ent = conn.execute("SELECT * FROM entrances WHERE id=?", (entrance_id,)).fetchone()
    if not ent:
        raise ServiceError('入口不存在', 'no_entrance')
    conn.execute(
        """INSERT INTO water_observations (entrance_id, task_id, level_cm, note,
           observed_by, created_at) VALUES (?,?,?,?,?,?)""",
        (entrance_id, task_id, level_cm, note, person_id, ts))
    incident_id = None
    if level_cm >= ent['orange_cm']:
        # 入口积水：开异常单并阻止拆除，直至水位回落并人工复查关闭
        task_id = task_id or conn.execute(
            "SELECT id FROM tasks WHERE entrance_id=? AND status IN (%s) ORDER BY id DESC LIMIT 1"
            % ','.join('?' * len(ACTIVE_TASK_STATUSES)),
            (entrance_id, *ACTIVE_TASK_STATUSES)).fetchone()
        task_id = task_id[0] if isinstance(task_id, sqlite3.Row) else task_id
        if task_id and not open_incident_exists(conn, task_id, 'water_ingress'):
            incident_id = add_incident(
                conn, task_id, 'water_ingress',
                detail=f'入口积水 {level_cm}cm ≥ 阈值 {ent["orange_cm"]}cm，禁止拆除',
                raised_by=person_id, ts=ts, entrance_id=entrance_id)
    conn.commit()
    return {'incident_id': incident_id, 'observer': p['name']}


# ------------------------------------------------------- early dismantle flow

def request_early_removal(conn, task_id, person_id, reason, ts=None):
    """申请提前解除（预警未解除时拆除）。"""
    ts = ts or now()
    require_role(conn, person_id, 'worker', 'guard', 'supervisor')
    task = _get_task(conn, task_id)
    if task['status'] != 'deployed':
        raise ServiceError('只有已部署任务可以申请提前解除', 'bad_status')
    alert = _active_alert(conn)
    if not alert:
        raise ServiceError('预警已解除，按正常流程拆除即可', 'alert_cleared')
    if task['early_removal_status'] == 'requested':
        raise ServiceError('已提交提前解除申请，等待主管审批', 'pending_approval')
    conn.execute(
        "UPDATE tasks SET early_removal_status='requested', early_removal_reason=?, "
        "early_removal_by=?, early_removal_at=? WHERE id=?",
        (reason, person_id, ts, task_id))
    add_incident(conn, task_id, 'early_dismantle',
                 detail=f'申请预警未解除提前拆除：{reason}', raised_by=person_id, ts=ts)
    conn.commit()


def decide_early_removal(conn, task_id, supervisor_id, approve, ts=None):
    """物业主管决定提前拆除；保安无权。"""
    ts = ts or now()
    require_role(conn, supervisor_id, 'supervisor')
    task = _get_task(conn, task_id)
    if task['early_removal_status'] != 'requested':
        raise ServiceError('没有待审批的提前解除申请', 'no_request')
    state = 'approved' if approve else 'rejected'
    conn.execute(
        "UPDATE tasks SET early_removal_status=?, early_approval_by=?, early_approval_at=? WHERE id=?",
        (state, supervisor_id, ts, task_id))
    conn.execute(
        """UPDATE incidents SET status='handled', handled_by=?, handled_at=?,
           resolution=? WHERE task_id=? AND type='early_dismantle' AND status='open'""",
        (supervisor_id, ts, f'主管{"批准" if approve else "驳回"}提前拆除', task_id))
    conn.commit()


def dismantle(conn, task_id, person_id, ts=None):
    """拆除：必须预警已解除，或持主管批准的提前解除；积水异常未关闭同样禁止。"""
    ts = ts or now()
    require_role(conn, person_id, 'worker', 'supervisor')
    task = _get_task(conn, task_id)
    if task['status'] != 'deployed':
        raise ServiceError('只有已部署任务可以拆除', 'bad_status')
    alert = _active_alert(conn)
    if alert is not None:
        if task['early_removal_status'] != 'approved':
            add_incident(conn, task_id, 'early_dismantle',
                         detail='预警未解除即尝试拆除，已制止（拆除过早）',
                         raised_by=person_id, ts=ts)
            conn.commit()
            raise ServiceError(
                '预警尚未解除，禁止拆除（拆除过早）。如确需拆除，请提交提前解除申请'
                '并由物业主管批准', 'alert_active')
    if open_incident_exists(conn, task_id, 'water_ingress'):
        raise ServiceError('入口积水异常未关闭，不能拆除；请先排水并复查', 'water_ingress')
    conn.execute(
        "UPDATE tasks SET status='dismantled', dismantled_at=? WHERE id=?", (ts, task_id))
    conn.execute(
        "UPDATE equipment SET status='cleaning', version=version+1 "
        "WHERE current_task_id=? AND status='installed'", (task_id,))
    conn.commit()


# ---------------------------------------------------------------- wash & dry

def _set_wash_stage(conn, task_id, from_status, to_status, column, ts):
    cur = conn.execute(
        f"UPDATE tasks SET status=?, {column}=? WHERE id=? AND status=?",
        (to_status, ts, task_id, from_status))
    if cur.rowcount == 0:
        raise ServiceError(f'任务当前不是 {from_status} 阶段', 'bad_status')


def wash(conn, task_id, person_id, ts=None):
    """清洗：本任务全部器材进入晾干。"""
    ts = ts or now()
    require_role(conn, person_id, 'worker', 'supervisor')
    _get_task(conn, task_id)
    _set_wash_stage(conn, task_id, 'dismantled', 'drying', 'cleaned_at', ts)
    conn.execute(
        "UPDATE equipment SET status='drying', version=version+1 "
        "WHERE current_task_id=? AND status='cleaning'", (task_id,))
    conn.commit()


def dry_finish(conn, task_id, person_id, ts=None):
    """晾干完成，进入归架核对。"""
    ts = ts or now()
    require_role(conn, person_id, 'worker', 'supervisor')
    _get_task(conn, task_id)
    _set_wash_stage(conn, task_id, 'drying', 'return_check', 'dried_at', ts)
    conn.execute(
        "UPDATE equipment SET status='shelved', version=version+1 "
        "WHERE current_task_id=? AND status='drying'", (task_id,))
    conn.commit()


# ---------------------------------------------------------------- return check

def return_item(conn, task_id, item_id, condition, person_id, note='', ts=None):
    """逐件归架核对：ok / damaged（破损报损）/ missing（缺件补充）。"""
    ts = ts or now()
    require_role(conn, person_id, 'worker', 'supervisor')
    task = _get_task(conn, task_id)
    if task['status'] != 'return_check':
        raise ServiceError('只有归架核对阶段可以登记归架', 'bad_status')
    it = conn.execute("SELECT * FROM task_items WHERE id=? AND task_id=?",
                      (item_id, task_id)).fetchone()
    if not it:
        raise ServiceError('明细不存在', 'no_item')
    if it['returned_at']:
        raise ServiceError(f"{it['pos_label']}已核对过", 'already_returned')
    code = it['locked_code']
    if condition == 'ok':
        conn.execute(
            "UPDATE task_items SET returned_at=?, return_condition='ok', note=? WHERE id=?",
            (ts, note, it['id']))
        if code:
            conn.execute(
                "UPDATE equipment SET status='shelved' WHERE code=?", (code,))
    elif condition == 'damaged':
        if not code:
            raise ServiceError('破损件必须有器材编号', 'no_code')
        conn.execute(
            "UPDATE equipment SET status='damaged', version=version+1 WHERE code=?", (code,))
        conn.execute(
            "UPDATE task_items SET returned_at=?, return_condition='damaged', note=? WHERE id=?",
            (ts, note, it['id']))
        conn.execute(
            "INSERT INTO supply_requests (task_id, kind, equipment_code, reason, quantity, "
            "requested_by, note, created_at) VALUES (?,?,?,'damage_writeoff',1,?,?,?)",
            (task_id, it['kind'], code, person_id,
             f"{it['pos_label']}归架发现破损：{note}", ts))
        add_incident(conn, task_id, 'return_shortage',
                     detail=f"{it['pos_label']} {code} 归架破损，待主管报损",
                     raised_by=person_id, ts=ts)
    elif condition == 'missing':
        conn.execute(
            "UPDATE task_items SET returned_at=?, return_condition='missing', note=? WHERE id=?",
            (ts, note, it['id']))
        if code:
            conn.execute(
                "UPDATE equipment SET status='missing', current_task_id=NULL, version=version+1 "
                "WHERE code=?", (code,))
        conn.execute(
            "INSERT INTO supply_requests (task_id, kind, equipment_code, reason, quantity, "
            "requested_by, note, created_at) VALUES (?,?,?,'shortage',1,?,?,?)",
            (task_id, it['kind'], code, person_id,
             f"{it['pos_label']}归架缺件{('(' + code + ')') if code else ''}：{note}", ts))
        add_incident(conn, task_id, 'return_shortage',
                     detail=f"{it['pos_label']} {code or ''} 归架缺件，已开补充单",
                     raised_by=person_id, ts=ts)
    else:
        raise ServiceError('condition 只能是 ok/damaged/missing', 'bad_condition')
    conn.commit()


def approve_supply(conn, request_id, supervisor_id, ts=None):
    """物业主管缺件报损审批：破损件核销 written_off，缺件采购申请批准。"""
    ts = ts or now()
    require_role(conn, supervisor_id, 'supervisor')
    req = conn.execute("SELECT * FROM supply_requests WHERE id=?", (request_id,)).fetchone()
    if not req:
        raise ServiceError('补充单不存在', 'no_request')
    if req['status'] != 'requested':
        raise ServiceError('该补充单已审批', 'already_decided')
    if req['reason'] == 'damage_writeoff' and req['equipment_code']:
        conn.execute(
            "UPDATE equipment SET status='written_off', version=version+1 WHERE code=?",
            (req['equipment_code'],))
    conn.execute(
        "UPDATE supply_requests SET status='approved', approved_by=? WHERE id=?",
        (supervisor_id, request_id))
    conn.commit()


def restock(conn, request_id, person_id, new_code=None, shelf=None, ts=None):
    """补充入库：生成新器材，缺件/报损流程闭环。"""
    ts = ts or now()
    require_role(conn, person_id, 'worker', 'supervisor')
    req = conn.execute("SELECT * FROM supply_requests WHERE id=?", (request_id,)).fetchone()
    if not req:
        raise ServiceError('补充单不存在', 'no_request')
    if req['status'] != 'approved':
        raise ServiceError('需先经主管批准才能入库', 'not_approved')
    prefix = {'panel': 'B', 'post': 'P', 'seal': 'S'}[req['kind']]
    if not new_code:
        row = conn.execute(
            "SELECT COUNT(*)+50 c FROM equipment WHERE kind=?", (req['kind'],)).fetchone()
        new_code = f"{prefix}-N{row['c']:02d}"
    if conn.execute("SELECT 1 FROM equipment WHERE code=?", (new_code,)).fetchone():
        raise ServiceError(f'编号 {new_code} 已存在', 'code_exists')
    name = {'panel': '挡板（补充）', 'post': '立柱（补充）', 'seal': '密封条（补充）'}[req['kind']]
    conn.execute(
        "INSERT INTO equipment (code, kind, name, shelf, status) VALUES (?,?,?,?,'available')",
        (new_code, req['kind'], name, shelf))
    conn.execute(
        "UPDATE supply_requests SET status='restocked', resolved_at=? WHERE id=?",
        (ts, request_id))
    conn.commit()
    return new_code


def complete_return(conn, task_id, person_id, ts=None):
    """归架盘点完成：条件更新保证并发只完成一次；差异全部闭环且逐件核对后器材重新可用。"""
    ts = ts or now()
    require_role(conn, person_id, 'worker', 'supervisor')
    task = _get_task(conn, task_id)
    if task['status'] == 'completed':
        raise ServiceError('任务已完成', 'already_completed')
    if task['status'] != 'return_check':
        raise ServiceError('任务不在归架核对阶段', 'bad_status')
    unchecked = conn.execute(
        "SELECT COUNT(*) c FROM task_items WHERE task_id=? AND returned_at IS NULL",
        (task_id,)).fetchone()['c']
    if unchecked:
        raise ServiceError(f'还有 {unchecked} 件器材未逐件核对，不能完成归架',
                           'return_incomplete')
    pending = conn.execute(
        "SELECT id, reason FROM supply_requests WHERE task_id=? AND status != 'restocked'",
        (task_id,)).fetchall()
    if pending:
        raise ServiceError(
            '归架差异未闭环：缺件/报损补充单 ' +
            '、'.join(f"#{r['id']}({r['reason']})" for r in pending) + ' 需主管审批并补充入库',
            'supply_open')

    cur = conn.execute(
        "UPDATE tasks SET status='completed', completed_at=? WHERE id=? AND status='return_check'",
        (ts, task_id))
    if cur.rowcount != 1:
        # 并发完成：另一个事务已经抢先
        fresh = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
        if fresh and fresh['status'] == 'completed':
            raise ServiceError('任务已完成，本次并发提交只生效一次', 'already_completed')
        raise ServiceError('任务刚被其他操作更新，请刷新后重试', 'concurrent_finish')
    conn.execute(
        "UPDATE equipment SET status='available', current_task_id=NULL, version=version+1 "
        "WHERE current_task_id=? AND status='shelved'", (task_id,))
    conn.commit()


# ---------------------------------------------------------------- alert

def _active_alert(conn):
    return conn.execute(
        "SELECT * FROM alerts WHERE status='active' ORDER BY id DESC LIMIT 1").fetchone()


def active_alert(conn):
    return _active_alert(conn)


LEVEL_RANK = {'blue': 0, 'yellow': 1, 'orange': 2, 'red': 3}
LEVEL_CN = {'blue': '蓝色', 'yellow': '黄色', 'orange': '橙色', 'red': '红色'}


def level_from_readings(rainfall_mm, entrance_levels):
    """雨量与各入口水位取最高预警等级。entrance_levels: [(entrance_row, cm), ...]"""
    level = 'blue'
    if rainfall_mm >= 16:
        level = 'orange'
    elif rainfall_mm >= 8:
        level = 'yellow'
    for ent, cm in entrance_levels:
        if cm >= ent['red_cm']:
            cand = 'red'
        elif cm >= ent['orange_cm']:
            cand = 'orange'
        elif cm >= ent['warn_cm']:
            cand = 'yellow'
        else:
            cand = 'blue'
        if LEVEL_RANK[cand] > LEVEL_RANK[level]:
            level = cand
    return level


def ensure_alert(conn, level, rainfall_mm, reason, ts=None):
    ts = ts or now()
    alert = _active_alert(conn)
    if alert is None:
        cur = conn.execute(
            "INSERT INTO alerts (level, rainfall_mm, reason, started_at) VALUES (?,?,?,?)",
            (level, rainfall_mm, reason, ts))
        conn.commit()
        return cur.lastrowid, True
    if LEVEL_RANK[level] > LEVEL_RANK[alert['level']]:
        conn.execute("UPDATE alerts SET level=?, rainfall_mm=?, reason=? WHERE id=?",
                     (level, rainfall_mm, reason, alert['id']))
    else:
        conn.execute("UPDATE alerts SET rainfall_mm=? WHERE id=?", (rainfall_mm, alert['id']))
    conn.commit()
    return alert['id'], False


def clear_alert(conn, reason, ts=None):
    ts = ts or now()
    cur = conn.execute(
        "UPDATE alerts SET status='cleared', cleared_at=? WHERE status='active'", (ts,))
    conn.commit()
    return cur.rowcount
