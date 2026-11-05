"""分页 / 搜索条数等外部输入的边界钳制。

所有面向公网或半公开的分页、limit 参数都必须经过这里，避免负数 LIMIT
（SQLite 中 -1 表示不限制）导致全表导出，或超大值造成内存/CPU 压力。
"""

import os


def _env_positive_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        value = int(str(os.environ.get(name, "") or default).strip())
    except (TypeError, ValueError):
        return default
    return max(value, minimum)


def max_page_size() -> int:
    return _env_positive_int("MAX_PAGE_SIZE", 100)


def max_search_limit() -> int:
    return _env_positive_int("MAX_SEARCH_LIMIT", 100)


def clamp_int(value, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def clamp_page(value, default: int = 1) -> int:
    return clamp_int(value, default, 1, 1_000_000)


def clamp_page_size(value, default: int = 10) -> int:
    return clamp_int(value, default, 1, max_page_size())


def clamp_search_limit(value, default: int = 100) -> int:
    return clamp_int(value, default, 1, max_search_limit())
