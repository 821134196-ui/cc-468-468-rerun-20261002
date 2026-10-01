"""HTTP 服务：静态页面 + JSON API；照片写本地目录。仅依赖标准库。"""
from __future__ import annotations

import base64
import json
import logging
import re
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import service
from .db import connect

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / 'web'
PHOTOS = ROOT / 'photos'

log = logging.getLogger('http')

KIND_CN = {'panel': '挡板', 'post': '立柱', 'seal': '密封条'}
STATUS_CN = {
    'created': '待领取', 'claimed': '已领取', 'installing': '安装中',
    'deployed': '已部署', 'dismantled': '已拆除', 'cleaning': '清洗中',
    'drying': '晾干中', 'return_check': '归架核对', 'completed': '已完成',
    'cancelled': '已取消',
    'available': '可用', 'locked': '已锁定', 'installed': '已安装',
    'shelved': '已归架', 'damaged': '破损', 'missing': '缺件',
    'written_off': '已核销', 'cleaning_e': '清洗中', 'drying_e': '晾干中',
}


class AppState:
    def __init__(self, db_path, adapter=None, monitor=None):
        self.db_path = db_path
        self.adapter = adapter
        self.monitor = monitor


def rows(conn, sql, args=()):
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


# ----------------------------------------------------------------- serializers

def task_detail(conn, tid):
    t = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    if not t:
        return None
    d = dict(t)
    d['status_cn'] = STATUS_CN.get(t['status'], t['status'])
    ent = conn.execute("SELECT * FROM entrances WHERE id=?", (t['entrance_id'],)).fetchone()
    d['entrance'] = dict(ent)
    d['items'] = rows(conn, """
        SELECT ti.*, e.name AS equipment_name, e.shelf AS equipment_shelf,
               e.status AS equipment_status,
               pc.name AS connection_by_name, ps.name AS seal_by_name
        FROM task_items ti
        LEFT JOIN equipment e ON e.code = ti.locked_code
        LEFT JOIN personnel pc ON pc.id = ti.connection_confirmed_by
        LEFT JOIN personnel ps ON ps.id = ti.seal_confirmed_by
        WHERE ti.task_id=? ORDER BY ti.kind DESC, ti.slot_index, ti.id""", (tid,))
    # 上面排序仅为稳定，前端按 kind 自行归位
    d['incidents'] = rows(conn, """
        SELECT i.*, p.name AS raised_name, h.name AS handled_name
        FROM incidents i LEFT JOIN personnel p ON p.id=i.raised_by
        LEFT JOIN personnel h ON h.id=i.handled_by
        WHERE i.task_id=? ORDER BY i.id DESC""", (tid,))
    d['supplies'] = rows(conn, """
        SELECT s.*, p.name AS requested_name, a.name AS approved_name
        FROM supply_requests s LEFT JOIN personnel p ON p.id=s.requested_by
        LEFT JOIN personnel a ON a.id=s.approved_by
        WHERE s.task_id=? ORDER BY s.id""", (tid,))
    d['passes'] = rows(conn, "SELECT * FROM temp_passes WHERE task_id=? ORDER BY id", (tid,))
    d['photos'] = rows(conn, "SELECT * FROM photos WHERE task_id=? ORDER BY id", (tid,))
    d['crew'] = rows(conn, """
        SELECT tp.*, p.name, p.role FROM task_personnel tp
        JOIN personnel p ON p.id=tp.person_id WHERE tp.task_id=?""", (tid,))
    return d


def dashboard(conn, state: AppState):
    alert = service.active_alert(conn)
    rain = rows(conn, "SELECT ts, value_mm FROM rainfall_readings ORDER BY id DESC LIMIT 60")
    rain.reverse()
    levels = {}
    for r in conn.execute("""SELECT lr.* FROM level_readings lr
            JOIN (SELECT entrance_id, MAX(id) m FROM level_readings GROUP BY entrance_id) g
            ON g.m=lr.id"""):
        levels[r['entrance_id']] = dict(r)
    hist = rows(conn, """
        SELECT lr.ts, lr.entrance_id, lr.level_cm FROM level_readings lr
        WHERE lr.id > (SELECT COALESCE(MAX(id),0)-180 FROM level_readings)
        ORDER BY lr.id""")
    entrances = []
    for e in conn.execute("SELECT * FROM entrances ORDER BY sort_order, id"):
        ed = dict(e)
        ed['latest'] = levels.get(e['id'])
        ed['level_cm'] = levels[e['id']]['level_cm'] if e['id'] in levels else None
        active_t = conn.execute(
            "SELECT id, status FROM tasks WHERE entrance_id=? AND status IN (%s) "
            "ORDER BY id DESC LIMIT 1" % ','.join('?' * len(service.ACTIVE_TASK_STATUSES)),
            (e['id'], *service.ACTIVE_TASK_STATUSES)).fetchone()
        ed['active_task_id'] = active_t['id'] if active_t else None
        ed['active_task_status'] = active_t['status'] if active_t else None
        entrances.append(ed)
    tasks = rows(conn, """
        SELECT t.*, e.name AS entrance_name FROM tasks t
        JOIN entrances e ON e.id=t.entrance_id
        WHERE t.status != 'completed' ORDER BY t.id DESC""")
    for t in tasks:
        t['status_cn'] = STATUS_CN.get(t['status'], t['status'])
    completed = conn.execute(
        "SELECT COUNT(*) c FROM tasks WHERE status='completed'").fetchone()['c']
    eq_counts = {k: dict(r) for k, r in
                 [('all', conn.execute(
                     "SELECT COUNT(*) total, SUM(status='available') avail FROM equipment")
                   .fetchone())]}
    open_incidents = conn.execute(
        "SELECT COUNT(*) c FROM incidents WHERE status='open'").fetchone()['c']
    pending_supplies = conn.execute(
        "SELECT COUNT(*) c FROM supply_requests WHERE status='requested'").fetchone()['c']
    return {
        'alert': dict(alert) if alert else None,
        'monitor_tick': getattr(state.monitor, 'last_tick', None),
        'rain_series': rain,
        'level_series': hist,
        'entrances': entrances,
        'tasks': tasks,
        'completed_count': completed,
        'equipment': eq_counts['all'],
        'open_incidents': open_incidents,
        'pending_supplies': pending_supplies,
    }


# ----------------------------------------------------------------- handler

class Handler(BaseHTTPRequestHandler):
    state: AppState = None

    def log_message(self, fmt, *args):
        log.debug(fmt, *args)

    # --- helpers
    def _send(self, obj, status=200, ctype='application/json; charset=utf-8'):
        body = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json_body(self):
        n = int(self.headers.get('Content-Length') or 0)
        if not n:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode())
        except json.JSONDecodeError:
            raise service.ServiceError('请求体不是合法 JSON', 'bad_json')

    def _ok(self, data=None, status=200):
        self._send({'ok': True, 'data': data}, status)

    def _err(self, exc):
        status = {'forbidden': 403, 'concurrent_finish': 409,
                 'equipment_busy': 409, 'task_exists': 409}.get(exc.code, 400)
        self._send({'ok': False, 'code': exc.code, 'error': str(exc)}, status)

    # --- GET
    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path == '/':
                return self._static('index.html')
            if path.startswith('/photos/'):
                return self._photo(path[len('/photos/'):])
            static_map = {'/app.js': 'app.js', '/styles.css': 'styles.css'}
            if path in static_map:
                return self._static(static_map[path])
            conn = connect(self.state.db_path)
            try:
                if path == '/api/state':
                    return self._ok(dashboard(conn, self.state))
                if path == '/api/personnel':
                    return self._ok(rows(conn, "SELECT * FROM personnel ORDER BY id"))
                if path == '/api/equipment':
                    return self._ok(rows(conn, """
                        SELECT e.*, t.entrance_id AS task_entrance
                        FROM equipment e LEFT JOIN tasks t ON t.id=e.current_task_id
                        ORDER BY e.kind, e.code"""))
                if path == '/api/incidents':
                    return self._ok(rows(conn, """
                        SELECT i.*, p.name AS raised_name FROM incidents i
                        LEFT JOIN personnel p ON p.id=i.raised_by ORDER BY i.id DESC"""))
                if path == '/api/supplies':
                    return self._ok(rows(conn, """
                        SELECT s.*, p.name AS requested_name, a.name AS approved_name
                        FROM supply_requests s LEFT JOIN personnel p ON p.id=s.requested_by
                        LEFT JOIN personnel a ON a.id=s.approved_by ORDER BY s.id"""))
                m = re.fullmatch(r'/api/tasks/(\d+)', path)
                if m:
                    d = task_detail(conn, int(m.group(1)))
                    if not d:
                        raise service.ServiceError('任务不存在', 'no_task')
                    return self._ok(d)
                if path == '/api/tasks':
                    return self._ok(rows(conn, """
                        SELECT t.*, e.name AS entrance_name FROM tasks t
                        JOIN entrances e ON e.id=t.entrance_id ORDER BY t.id DESC"""))
                self._send({'ok': False, 'error': 'not found'}, 404)
            finally:
                conn.close()
        except service.ServiceError as exc:
            self._err(exc)

    def _static(self, name):
        f = WEB / name
        if not f.is_file():
            return self._send({'ok': False, 'error': 'not found'}, 404)
        ctype = {'index.html': 'text/html; charset=utf-8',
                 'app.js': 'application/javascript; charset=utf-8',
                 'styles.css': 'text/css; charset=utf-8'}.get(name, 'application/octet-stream')
        self._send(f.read_bytes(), 200, ctype)

    def _photo(self, name):
        name = Path(name).name  # 防目录穿越
        f = PHOTOS / name
        if not f.is_file():
            return self._send(b'not found', 404, 'text/plain')
        ext = f.suffix.lower()
        ctype = {'.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
                 '.webp': 'image/webp'}.get(ext, 'application/octet-stream')
        self._send(f.read_bytes(), 200, ctype)

    # --- POST
    def do_POST(self):
        path = urlparse(self.path).path
        conn = connect(self.state.db_path)
        try:
            body = self._json_body()
            pid = body.get('person_id')

            def person():
                if pid is None:
                    raise service.ServiceError('缺少 person_id', 'no_person')
                return int(pid)

            if path == '/api/checklist':
                tid = service.generate_checklist(conn, body['entrance_id'],
                                                 body.get('alert_id'))
                return self._ok({'task_id': tid}, 201)

            if path == '/api/weather/manual':
                if self.state.adapter:
                    self.state.adapter.set_manual(body.get('rain'), body.get('levels'))
                return self._ok({'manual': True})

            if path == '/api/photos':
                return self._ok(self._save_photo(conn, body), 201)

            m = re.fullmatch(r'/api/tasks/(\d+)/([\w-]+)', path)
            if m:
                tid, action = int(m.group(1)), m.group(2)
                return self._task_action(conn, tid, action, body, person)

            m = re.fullmatch(r'/api/items/(\d+)/([\w-]+)', path)
            if m:
                iid, action = int(m.group(1)), m.group(2)
                return self._item_action(conn, iid, action, body, person)

            m = re.fullmatch(r'/api/passes/(\d+)/restore', path)
            if m:
                service.restore_pass(conn, int(m.group(1)), person())
                return self._ok()

            m = re.fullmatch(r'/api/incidents/(\d+)/resolve', path)
            if m:
                service.resolve_incident(conn, int(m.group(1)), person(),
                                         body.get('resolution', '已处理'))
                return self._ok()

            m = re.fullmatch(r'/api/supplies/(\d+)/([\w-]+)', path)
            if m:
                rid, action = int(m.group(1)), m.group(2)
                if action == 'approve':
                    service.approve_supply(conn, rid, person())
                elif action == 'restock':
                    code = service.restock(conn, rid, person(), body.get('new_code'),
                                           body.get('shelf'))
                    return self._ok({'new_code': code})
                else:
                    raise service.ServiceError('未知操作', 'bad_action')
                return self._ok()

            m = re.fullmatch(r'/api/entrances/([\w-]+)/observe', path)
            if m:
                res = service.observe_water(
                    conn, m.group(1), float(body['level_cm']), person(),
                    body.get('note', ''), body.get('task_id'))
                return self._ok(res)

            self._send({'ok': False, 'error': 'not found'}, 404)
        except service.ServiceError as exc:
            self._err(exc)
        except (KeyError, ValueError) as exc:
            self._err(service.ServiceError(f'参数错误：{exc}', 'bad_param'))
        finally:
            conn.close()

    def _task_action(self, conn, tid, action, body, person):
        p = lambda: person()
        if action == 'claim':
            return self._ok(service.claim_plan(conn, tid, p()))
        if action == 'claim-one':
            service.claim_one(conn, tid, int(body['item_id']), body['code'], p())
            return self._ok()
        if action == 'install-panel':
            return self._ok(service.install_panel(conn, tid, int(body['slot']),
                                                  body['code'], p()))
        if action == 'fix-segment':
            return self._ok(service.fix_wrong_segment(conn, tid, int(body['slot']),
                                                      body['code'], p()))
        if action == 'damage-seal':
            return self._ok(service.damage_seal(conn, tid, int(body['slot']), p(),
                                                body.get('replacement_code')))
        if action == 'confirm':
            service.confirm_slot(conn, tid, int(body['slot']), body['check'], p())
            return self._ok()
        if action == 'control':
            service.control_channel(conn, tid, p(), bool(body.get('controlled', True)))
            return self._ok()
        if action == 'deploy':
            service.finish_deploy(conn, tid, p())
            return self._ok()
        if action == 'arrive':
            return self._ok(service.mark_arrival(conn, tid, p()))
        if action == 'temp-pass':
            pid_ = service.open_temp_pass(conn, tid, p(), body.get('reason', '临时车辆放行'))
            return self._ok({'pass_id': pid_})
        if action == 'request-early':
            service.request_early_removal(conn, tid, p(),
                                          body.get('reason', '现场需要提前拆除'))
            return self._ok()
        if action == 'decide-early':
            service.decide_early_removal(conn, tid, p(), bool(body.get('approve', True)))
            return self._ok()
        if action == 'dismantle':
            service.dismantle(conn, tid, p())
            return self._ok()
        if action == 'wash':
            service.wash(conn, tid, p())
            return self._ok()
        if action == 'dry':
            service.dry_finish(conn, tid, p())
            return self._ok()
        if action == 'complete':
            service.complete_return(conn, tid, p())
            return self._ok()
        raise service.ServiceError(f'未知操作 {action}', 'bad_action')

    def _item_action(self, conn, iid, action, body, person):
        if action == 'place':
            # 需要 task_id：从明细反查
            it = conn.execute("SELECT task_id FROM task_items WHERE id=?", (iid,)).fetchone()
            if not it:
                raise service.ServiceError('明细不存在', 'no_item')
            service.install_post_or_seal(conn, it['task_id'], iid, person())
            return self._ok()
        if action == 'return':
            it = conn.execute("SELECT task_id FROM task_items WHERE id=?", (iid,)).fetchone()
            if not it:
                raise service.ServiceError('明细不存在', 'no_item')
            service.return_item(conn, it['task_id'], iid, body['condition'], person(),
                                body.get('note', ''))
            return self._ok()
        raise service.ServiceError(f'未知操作 {action}', 'bad_action')

    def _save_photo(self, conn, body):
        data = body.get('data', '')
        m = re.match(r'data:image/(jpeg|png|webp);base64,(.*)', data, re.S)
        if m:
            ext, b64 = m.group(1), m.group(2)
            ext = 'jpg' if ext == 'jpeg' else ext
        else:
            ext, b64 = 'jpg', data
        try:
            raw = base64.b64decode(b64)
        except Exception:
            raise service.ServiceError('照片数据无法解码', 'bad_photo')
        if len(raw) > 8 * 1024 * 1024:
            raise service.ServiceError('照片超过 8MB', 'too_large')
        ts_compact = service.now().translate(str.maketrans({' ': '_', ':': '-'}))
        suffix = uuid.uuid4().hex[:8]
        fname = f"{body.get('stage','general')}_{ts_compact}_{suffix}.{ext}"
        PHOTOS.mkdir(exist_ok=True)
        (PHOTOS / fname).write_bytes(raw)
        cur = conn.execute(
            """INSERT INTO photos (task_id, entrance_id, stage, slot_index, filename,
               taken_by, note, created_at) VALUES (?,?,?,?,?,?,?,?)""",
            (body.get('task_id'), body.get('entrance_id'), body.get('stage', 'general'),
             body.get('slot_index'), fname, body.get('taken_by'),
             body.get('note', ''), service.now()))
        conn.commit()
        return {'photo_id': cur.lastrowid, 'filename': fname, 'url': f'/photos/{fname}'}


def build_server(db_path, adapter=None, monitor=None, host='127.0.0.1', port=8000):
    state = AppState(db_path, adapter, monitor)
    Handler.state = state
    httpd = ThreadingHTTPServer((host, port), Handler)
    return httpd
