"""Expose the existing /admin routes under a configurable external path."""

import re

from src.services.admin_path_service import DEFAULT_PATH, get_admin_path

ADMIN_URL_PATTERN = re.compile(rb"/admin(?=[/?#'\"` <]|$)")


class AdminPathMiddleware:
    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        public_path = get_admin_path()
        path = environ.get("PATH_INFO", "")
        if public_path != DEFAULT_PATH:
            if path == public_path or path.startswith(public_path + "/"):
                environ = environ.copy()
                environ["PATH_INFO"] = DEFAULT_PATH + path[len(public_path):]
            elif path == DEFAULT_PATH or path.startswith(DEFAULT_PATH + "/") or path in ("/login", "/logout"):
                body = b"Not Found"
                start_response("404 Not Found", [("Content-Type", "text/plain; charset=utf-8"), ("Content-Length", str(len(body)))])
                return [body]

        # Flask static-file conditional responses would otherwise retain old URLs.
        if public_path != DEFAULT_PATH and path.endswith(".js"):
            environ = environ.copy()
            environ.pop("HTTP_IF_NONE_MATCH", None)
            environ.pop("HTTP_IF_MODIFIED_SINCE", None)

        captured = {}

        def capture(status, headers, exc_info=None):
            captured.update(status=status, headers=headers, exc_info=exc_info)

        result = self.app(environ, capture)
        status = captured["status"]
        headers = captured["headers"]
        if public_path != DEFAULT_PATH:
            headers = [(k, (public_path + v[len(DEFAULT_PATH):] if k.lower() == "location" and (v == DEFAULT_PATH or v.startswith(DEFAULT_PATH + "/") or v.startswith(DEFAULT_PATH + "?")) else v)) for k, v in headers]
        content_type = next((v for k, v in headers if k.lower() == "content-type"), "").lower()
        rewrite_body = public_path != DEFAULT_PATH and ("text/html" in content_type or "javascript" in content_type)
        if rewrite_body and not status.startswith("304"):
            try:
                body = ADMIN_URL_PATTERN.sub(public_path.encode("ascii"), b"".join(result))
            finally:
                if hasattr(result, "close"):
                    result.close()
            headers = [(k, v) for k, v in headers if k.lower() not in ("content-length", "etag", "last-modified", "cache-control")]
            headers.extend([("Content-Length", str(len(body))), ("Cache-Control", "no-store")])
            result = [body]
        start_response(status, headers, captured.get("exc_info"))
        return result
