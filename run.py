"""进程入口：初始化 SQLite、载入演示样例、启动监控线程与 HTTP 服务。"""
from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

from app.db import connect
from app.monitor import Monitor
from app.seed import seed
from app.server import build_server
from app.weather import WeatherAdapter


def main():
    ap = argparse.ArgumentParser(description='社区地下车库防汛挡板部署与归位系统')
    ap.add_argument('--db', default='data/flood.db')
    ap.add_argument('--host', default='0.0.0.0')
    ap.add_argument('--port', type=int, default=8000)
    ap.add_argument('--interval', type=float, default=3.0, help='监控周期秒')
    ap.add_argument('--speed', type=float, default=1.0, help='天气时间轴倍速（演示）')
    ap.add_argument('--elapsed', type=float, default=12.0, help='启动时已经过秒数')
    ap.add_argument('--reset', action='store_true', help='删除旧库重新载入样例')
    ap.add_argument('--no-seed', action='store_true', help='保留现有数据库')
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(name)s: %(message)s')

    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if args.reset and db_path.exists():
        for suffix in ('', '-wal', '-shm'):
            p = Path(str(db_path) + suffix)
            if p.exists():
                p.unlink()
        logging.info('已重置数据库 %s', db_path)

    conn = connect(db_path)
    if args.no_seed:
        from app.db import init_db
        init_db(conn)
    else:
        # 每次启动均确保样例场景存在（已存在数据则保留）
        seed(conn, scenarios=True)
    ents = conn.execute("SELECT * FROM entrances ORDER BY sort_order").fetchall()
    conn.close()

    adapter = WeatherAdapter(speed=args.speed, start_elapsed=args.elapsed)
    monitor = Monitor(db_path, adapter, ents, interval=args.interval)
    monitor.start()

    httpd = build_server(db_path, adapter, monitor, args.host, args.port)
    logging.info('防汛挡板系统已启动：http://%s:%s', args.host, args.port)
    # 估算首个入口水位到橙色阈值（约 t≈32.5s）还需的真实时间
    eta = max(0.0, (32.5 - args.elapsed) / args.speed)
    logging.info('演示时间轴倍速 %sx，起始经过 %.0fs（约 %.0f 秒后水位达部署阈值）',
                 args.speed, args.elapsed, eta)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        monitor.stop()
        httpd.server_close()
        time.sleep(0.2)


if __name__ == '__main__':
    main()
