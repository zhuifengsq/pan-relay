#!/usr/bin/env python
"""pan-relay 后端基准脚本（可重复、离线）。

用法（在仓库根目录）：
    .venv/Scripts/python.exe scripts/bench_backend.py --requests 40 --warmup 10
    .venv/Scripts/python.exe scripts/bench_backend.py --concurrency 8 --requests 200
    .venv/Scripts/python.exe scripts/bench_backend.py --json-out data/bench.json

说明：
- 自动复制 data/pan_relay.db(+wal/shm) 到 data/pan_relay.bench.db 作为基准库，
  通过 SQLITE_DB_PATH 让 app 使用副本，不会写入真实数据库。
- 导入 app 前会禁用 start_scheduler()，避免后台定时任务干扰测量。
- 输出每个接口的 p50/p95 延迟、状态码，以及每次请求的 SQL 执行次数/连接数
  （单线程模式下计数；SQL 计数使用 sqlite3 trace 回调）。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import statistics
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
REAL_DB = ROOT / "data" / "pan_relay.db"
BENCH_DB = ROOT / "data" / "pan_relay.bench.db"

ENDPOINTS = [
    ("home", "/"),
    ("api_status", "/api/v1/status"),
    ("api_resources", "/api/v1/resources?page=1&page_size=20"),
    ("api_search_own", "/api/v1/search?keyword=%E6%96%97%E7%BD%97&scope=own&limit=10"),
]

# ---- SQLite 计数 ----
_orig_connect = sqlite3.connect
_counters = {"connects": 0, "statements": 0}
_counter_lock = threading.Lock()
_instrument = {"on": False}


def _trace_cb(_sql: str) -> None:
    with _counter_lock:
        _counters["statements"] += 1


def _counting_connect(*args, **kwargs):
    if _instrument["on"]:
        with _counter_lock:
            _counters["connects"] += 1
    conn = _orig_connect(*args, **kwargs)
    if _instrument["on"]:
        conn.set_trace_callback(_trace_cb)
    return conn


def reset_counters() -> None:
    with _counter_lock:
        _counters["connects"] = 0
        _counters["statements"] = 0


def snapshot_counters() -> tuple[int, int]:
    with _counter_lock:
        return _counters["connects"], _counters["statements"]


def prepare_db(force: bool = True) -> Path:
    if force or not BENCH_DB.exists():
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(REAL_DB) + suffix)
            dst = Path(str(BENCH_DB) + suffix)
            if src.exists():
                shutil.copy2(src, dst)
            elif dst.exists():
                dst.unlink()
    return BENCH_DB


def import_app(bench_db: Path):
    os.environ["SQLITE_DB_PATH"] = str(bench_db)
    # 导入 app 之前禁用调度器（app.py 在 import 时调用 start_scheduler）
    import src.services.scheduler_service as scheduler_service

    scheduler_service.start_scheduler = lambda *a, **k: False

    from app import app as flask_app

    return flask_app


def tune_bench_config() -> None:
    """调整副本库里的配置，避免限流/开关影响基准。"""
    from src.db.system_configs import get_config_value, set_config_value

    raw = get_config_value("api_mode_config")
    mode = json.loads(raw) if raw else {}
    mode["enable_frontend"] = True
    mode["api_only"] = False
    set_config_value("api_mode_config", mode)
    set_config_value("public_search_api", {"enabled": True})

    from src.services.system_config_service import DEFAULT_SECURITY_CONFIG, get_security_config, save_security_config

    sec = get_security_config()
    sec["rate_limit_per_minute"] = 1000000
    sec["max_concurrent_searches"] = 100000
    sec["min_keyword_length"] = 1
    save_security_config(sec)


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((p / 100.0) * (len(ordered) - 1)))))
    return ordered[idx] * 1000.0


def hit_once(client, path: str) -> tuple[float, int, int]:
    t0 = time.perf_counter()
    resp = client.get(path)
    dt = time.perf_counter() - t0
    return dt, resp.status_code, len(resp.data)


def run_sequential(flask_app, requests_n: int, warmup: int) -> list[dict]:
    results = []
    client = flask_app.test_client()
    for name, path in ENDPOINTS:
        for _ in range(warmup):
            hit_once(client, path)
        latencies: list[float] = []
        statuses: dict[int, int] = {}
        for _ in range(requests_n):
            dt, status, size = hit_once(client, path)
            latencies.append(dt)
            statuses[status] = statuses.get(status, 0) + 1
        results.append(
            {
                "name": name,
                "path": path,
                "n": requests_n,
                "p50_ms": round(pct(latencies, 50), 2),
                "p95_ms": round(pct(latencies, 95), 2),
                "max_ms": round(max(latencies) * 1000.0, 2),
                "statuses": statuses,
            }
        )
    return results


def count_queries(flask_app, iterations: int) -> list[dict]:
    """单线程下统计每个请求平均新建连接数/SQL 执行数（缓存加热后）。"""
    _instrument["on"] = True
    out = []
    client = flask_app.test_client()
    try:
        for name, path in ENDPOINTS:
            for _ in range(3):
                hit_once(client, path)
            reset_counters()
            for _ in range(iterations):
                hit_once(client, path)
            connects, statements = snapshot_counters()
            out.append(
                {
                    "name": name,
                    "connects_per_req": round(connects / iterations, 2),
                    "statements_per_req": round(statements / iterations, 2),
                }
            )
    finally:
        _instrument["on"] = False
    return out


def run_concurrent(flask_app, requests_n: int, concurrency: int, warmup: int) -> list[dict]:
    results = []
    for name, path in ENDPOINTS:
        warm_client = flask_app.test_client()
        for _ in range(warmup):
            hit_once(warm_client, path)

        latencies: list[float] = []
        statuses: dict[int, int] = {}
        lock = threading.Lock()
        per_thread = max(1, requests_n // concurrency)
        barrier = threading.Barrier(concurrency)

        def worker() -> None:
            client = flask_app.test_client()
            local_lat: list[float] = []
            local_status: dict[int, int] = {}
            barrier.wait()
            for _ in range(per_thread):
                dt, status, _size = hit_once(client, path)
                local_lat.append(dt)
                local_status[status] = local_status.get(status, 0) + 1
            with lock:
                latencies.extend(local_lat)
                for k, v in local_status.items():
                    statuses[k] = statuses.get(k, 0) + v

        threads = [threading.Thread(target=worker) for _ in range(concurrency)]
        t0 = time.perf_counter()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        elapsed = time.perf_counter() - t0
        total = len(latencies)
        results.append(
            {
                "name": name,
                "path": path,
                "n": total,
                "concurrency": concurrency,
                "rps": round(total / elapsed, 1) if elapsed > 0 else 0,
                "p50_ms": round(pct(latencies, 50), 2),
                "p95_ms": round(pct(latencies, 95), 2),
                "max_ms": round(max(latencies) * 1000.0, 2),
                "statuses": statuses,
            }
        )
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="pan-relay backend benchmark")
    parser.add_argument("--requests", type=int, default=40)
    parser.add_argument("--warmup", type=int, default=10)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--count-sql", action="store_true", default=True)
    parser.add_argument("--json-out", type=str, default="")
    args = parser.parse_args()

    bench_db = prepare_db()
    sqlite3.connect = _counting_connect
    flask_app = import_app(bench_db)
    tune_bench_config()

    payload = {
        "python": sys.version.split()[0],
        "bench_db": str(bench_db),
        "requests": args.requests,
        "warmup": args.warmup,
        "concurrency": args.concurrency,
    }

    if args.concurrency > 1:
        payload["results"] = run_concurrent(flask_app, args.requests, args.concurrency, args.warmup)
        print(f"\n== 并发基准 (concurrency={args.concurrency}, 每接口 {args.requests} 次) ==")
        for r in payload["results"]:
            print(
                f"{r['name']:<16} n={r['n']:<4} rps={r['rps']:<8} "
                f"p50={r['p50_ms']:<7} p95={r['p95_ms']:<7} max={r['max_ms']:<8} status={r['statuses']}"
            )
    else:
        seq = run_sequential(flask_app, args.requests, args.warmup)
        counts = count_queries(flask_app, max(5, min(10, args.requests)))
        by_name = {c["name"]: c for c in counts}
        for r in seq:
            c = by_name.get(r["name"], {})
            r["connects_per_req"] = c.get("connects_per_req")
            r["statements_per_req"] = c.get("statements_per_req")
        payload["results"] = seq
        print(f"\n== 单线程基准 (每接口 {args.requests} 次，warmup {args.warmup}) ==")
        for r in seq:
            print(
                f"{r['name']:<16} p50={r['p50_ms']:<7} p95={r['p95_ms']:<7} max={r['max_ms']:<8} "
                f"sql/req={r.get('statements_per_req')} conn/req={r.get('connects_per_req')} status={r['statuses']}"
            )

    if args.json_out:
        out = ROOT / args.json_out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print("json ->", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
