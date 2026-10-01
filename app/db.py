"""SQLite schema and connection helpers for the flood-barrier system."""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys = ON;

-- 现场人员（演示数据）
CREATE TABLE IF NOT EXISTS personnel (
    id      INTEGER PRIMARY KEY,
    name    TEXT NOT NULL,
    role    TEXT NOT NULL CHECK (role IN ('guard', 'worker', 'supervisor')),
    phone   TEXT
);

-- 车库入口
CREATE TABLE IF NOT EXISTS entrances (
    id            TEXT PRIMARY KEY,          -- E1 / E2 ...
    name          TEXT NOT NULL,
    slot_count    INTEGER NOT NULL,          -- 挡板分段（槽位）数量
    warn_cm       REAL NOT NULL DEFAULT 5,   -- 黄色水位阈值
    orange_cm     REAL NOT NULL DEFAULT 10,  -- 橙色水位阈值（部署阈值）
    red_cm        REAL NOT NULL DEFAULT 20,
    gain          REAL NOT NULL DEFAULT 18,  -- 汇水抬升幅度（演示）
    lag           REAL NOT NULL DEFAULT 0,   -- 汇水滞后秒（演示）
    sort_order    INTEGER NOT NULL DEFAULT 0
);

-- 器材：挡板 panel / 立柱 post / 密封条 seal
CREATE TABLE IF NOT EXISTS equipment (
    code           TEXT PRIMARY KEY,         -- B-A01 / P-01 / S-01
    kind           TEXT NOT NULL CHECK (kind IN ('panel', 'post', 'seal')),
    name           TEXT NOT NULL,
    shelf          TEXT,                     -- 存放架编号
    status         TEXT NOT NULL DEFAULT 'available'
                   CHECK (status IN ('available','locked','installed',
                                     'cleaning','drying','shelved',
                                     'damaged','missing','written_off')),
    current_task_id INTEGER,
    version        INTEGER NOT NULL DEFAULT 0,
    notes          TEXT DEFAULT '',
    FOREIGN KEY (current_task_id) REFERENCES tasks(id)
);

-- 预警
CREATE TABLE IF NOT EXISTS alerts (
    id           INTEGER PRIMARY KEY,
    level        TEXT NOT NULL CHECK (level IN ('blue','yellow','orange','red')),
    rainfall_mm  REAL NOT NULL DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','cleared')),
    reason       TEXT DEFAULT '',
    started_at   TEXT NOT NULL,
    cleared_at   TEXT
);

-- 部署任务（每个入口一次部署流程一个任务）
CREATE TABLE IF NOT EXISTS tasks (
    id            INTEGER PRIMARY KEY,
    entrance_id   TEXT NOT NULL,
    alert_id      INTEGER,
    status        TEXT NOT NULL DEFAULT 'created'
                  CHECK (status IN ('created','claimed','installing','deployed',
                                    'dismantled','cleaning','drying','return_check',
                                    'completed','cancelled')),
    channel_controlled   INTEGER NOT NULL DEFAULT 0,  -- 通道封控
    controlled_at       TEXT,
    controlled_by       INTEGER,
    early_removal_status TEXT NOT NULL DEFAULT 'none'
                         CHECK (early_removal_status IN ('none','requested','approved','rejected')),
    early_removal_reason TEXT DEFAULT '',
    early_removal_by     INTEGER,
    early_removal_at     TEXT,
    early_approval_by    INTEGER,
    early_approval_at    TEXT,
    cleaned_at   TEXT,
    dried_at     TEXT,
    completed_at TEXT,
    created_at   TEXT NOT NULL,
    deployed_at  TEXT,
    dismantled_at TEXT,
    FOREIGN KEY (entrance_id) REFERENCES entrances(id),
    FOREIGN KEY (alert_id)    REFERENCES alerts(id)
);

-- 任务清单明细：每槽一块挡板 + 一条密封条；槽间及两端各一根立柱
CREATE TABLE IF NOT EXISTS task_items (
    id           INTEGER PRIMARY KEY,
    task_id      INTEGER NOT NULL,
    slot_index   INTEGER NOT NULL,           -- 挡板/密封条：1..N；立柱：0..N
    pos_label    TEXT NOT NULL,              -- 槽1 / 柱0
    kind         TEXT NOT NULL CHECK (kind IN ('panel','post','seal')),
    planned_code TEXT,                       -- 生成清单时的建议器材
    locked_code  TEXT,                       -- 实际锁定（领取）的器材
    connection_confirmed_at TEXT,
    connection_confirmed_by INTEGER,
    seal_confirmed_at       TEXT,
    seal_confirmed_by       INTEGER,
    installed_at TEXT,
    returned_at  TEXT,
    return_condition TEXT DEFAULT 'ok' CHECK (return_condition IN ('ok','damaged','missing')),
    note TEXT DEFAULT '',
    FOREIGN KEY (task_id) REFERENCES tasks(id),
    FOREIGN KEY (locked_code) REFERENCES equipment(code)
);

-- 现场照片（文件落本地目录，库存路径）
CREATE TABLE IF NOT EXISTS photos (
    id          INTEGER PRIMARY KEY,
    task_id     INTEGER,
    entrance_id TEXT,
    stage       TEXT NOT NULL CHECK (stage IN
                ('install','control','release','dismantle','clean','return','general')),
    slot_index  INTEGER,
    filename    TEXT NOT NULL,
    taken_by    INTEGER,
    note        TEXT DEFAULT '',
    created_at  TEXT NOT NULL
);

-- 人工积水观察
CREATE TABLE IF NOT EXISTS water_observations (
    id          INTEGER PRIMARY KEY,
    entrance_id TEXT NOT NULL,
    task_id     INTEGER,
    level_cm    REAL NOT NULL,
    note        TEXT DEFAULT '',
    observed_by INTEGER,
    created_at  TEXT NOT NULL
);

-- 适配器自动读数（雨量）
CREATE TABLE IF NOT EXISTS rainfall_readings (
    id     INTEGER PRIMARY KEY,
    ts     TEXT NOT NULL,
    value_mm REAL NOT NULL
);

-- 适配器自动读数（各入口水位）
CREATE TABLE IF NOT EXISTS level_readings (
    id          INTEGER PRIMARY KEY,
    ts          TEXT NOT NULL,
    entrance_id TEXT NOT NULL,
    level_cm    REAL NOT NULL
);

-- 异常事件
CREATE TABLE IF NOT EXISTS incidents (
    id          INTEGER PRIMARY KEY,
    task_id     INTEGER,
    entrance_id TEXT,
    type        TEXT NOT NULL CHECK (type IN
                ('temp_vehicle_pass','wrong_segment','seal_damaged',
                 'personnel_late','water_ingress','early_dismantle','return_shortage')),
    status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','handled')),
    detail      TEXT DEFAULT '',
    raised_by   INTEGER,
    handled_by  INTEGER,
    resolution  TEXT DEFAULT '',
    created_at  TEXT NOT NULL,
    handled_at  TEXT
);

-- 临时车辆放行登记
CREATE TABLE IF NOT EXISTS temp_passes (
    id          INTEGER PRIMARY KEY,
    task_id     INTEGER NOT NULL,
    reason      TEXT NOT NULL,
    opened_by   INTEGER,
    opened_at   TEXT NOT NULL,
    restored_by INTEGER,
    restored_at TEXT
);

-- 任务派工
CREATE TABLE IF NOT EXISTS task_personnel (
    id         INTEGER PRIMARY KEY,
    task_id    INTEGER NOT NULL,
    person_id  INTEGER NOT NULL,
    duty       TEXT NOT NULL DEFAULT 'install',  -- install/guard/supervise
    due_at     TEXT,
    arrived_at TEXT,
    late       INTEGER NOT NULL DEFAULT 0,
    UNIQUE (task_id, person_id)
);

-- 缺件 / 报损 / 补充单
CREATE TABLE IF NOT EXISTS supply_requests (
    id          INTEGER PRIMARY KEY,
    task_id     INTEGER,
    kind        TEXT NOT NULL CHECK (kind IN ('panel','post','seal')),
    equipment_code TEXT,                     -- 报损时指向具体器材
    reason      TEXT NOT NULL CHECK (reason IN ('shortage','damage_writeoff')),
    quantity    INTEGER NOT NULL DEFAULT 1,
    status      TEXT NOT NULL DEFAULT 'requested'
                CHECK (status IN ('requested','approved','restocked','rejected')),
    requested_by INTEGER,
    approved_by  INTEGER,
    note        TEXT DEFAULT '',
    created_at  TEXT NOT NULL,
    resolved_at TEXT
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 3000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
