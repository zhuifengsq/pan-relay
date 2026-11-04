"""生产环境 WSGI 启动入口：使用 waitress 替代 Flask 自带开发服务器。

Flask 的 app.run() 仅适合本地开发（单进程、无并发调优、错误处理弱）。
本入口复用 app.py 中已初始化的 Flask app（蓝图、SQLite、调度器），
交给 waitress 多线程 WSGI 服务器托管，适合容器与小规模生产部署。

用法:
    python serve.py

环境变量:
    HOST              监听地址，默认 0.0.0.0
    PORT              监听端口，默认 5004
    WAITRESS_THREADS  工作线程数，默认 8（SQLite 写操作较少时可适当调大）
"""

import logging
import os

from waitress import serve

from app import app

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(int(str(os.environ.get(name, "") or default).strip()), minimum)
    except (TypeError, ValueError):
        return default


def main() -> None:
    host = os.getenv("HOST", "0.0.0.0")
    port = _env_int("PORT", 5004)
    threads = _env_int("WAITRESS_THREADS", 8)
    logger.info(f"以 waitress 生产模式启动，监听 {host}:{port}，工作线程数 {threads}")
    serve(app, host=host, port=port, threads=threads, ident="pan-relay")


if __name__ == "__main__":
    main()
