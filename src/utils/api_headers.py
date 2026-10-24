"""Validation and parsing for per-source HTTP request headers."""

import json
import re


_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


def parse_api_headers(value):
    """Return a validated header dictionary from a JSON object or string."""
    if value is None or value == "":
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("请求头必须是有效的 JSON 对象") from exc
    if not isinstance(value, dict):
        raise ValueError("请求头必须是 JSON 对象")
    if len(value) > 20:
        raise ValueError("请求头最多支持 20 项")
    headers = {}
    for name, header_value in value.items():
        if not isinstance(name, str) or not _HEADER_NAME.fullmatch(name):
            raise ValueError("请求头名称不合法")
        if not isinstance(header_value, str) or "\r" in header_value or "\n" in header_value:
            raise ValueError(f"请求头 {name} 的值必须是不含换行的字符串")
        headers[name] = header_value
    if len(json.dumps(headers, ensure_ascii=False)) > 8192:
        raise ValueError("请求头配置不能超过 8 KB")
    return headers


def serialize_api_headers(value):
    return json.dumps(parse_api_headers(value), ensure_ascii=False)
