"""Runtime-configurable public URL prefix for the admin console."""

import json
import re

from src.db.system_configs import get_config_value, set_config_value

CONFIG_KEY = "admin_entry_path"
DEFAULT_PATH = "/admin"
RESERVED = {"api", "static", "themes", "search", "docs", "login", "logout", "favicon.ico"}


def validate_admin_path(value):
    path = str(value or "").strip().rstrip("/")
    if not re.fullmatch(r"/[A-Za-z][A-Za-z0-9_-]{3,39}", path):
        raise ValueError("路径须以 / 开头，后接 4–40 位英文字母、数字、- 或 _")
    if path[1:].lower() in RESERVED:
        raise ValueError("该路径与现有站点路由冲突")
    return path


def get_admin_path():
    raw = get_config_value(CONFIG_KEY)
    if raw:
        try:
            return validate_admin_path(json.loads(raw).get("path"))
        except (ValueError, TypeError, AttributeError):
            pass
    return DEFAULT_PATH


def save_admin_path(path):
    return set_config_value(CONFIG_KEY, {"path": validate_admin_path(path)})
