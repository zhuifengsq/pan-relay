import datetime
import logging
import uuid
from functools import wraps

import jwt
from flask import jsonify, redirect, request, url_for

from src.configs.app_config import SECRET_KEY

logger = logging.getLogger(__name__)


def _get_token_version() -> int:
    """当前管理员令牌版本；DB 异常时回退 0，避免后台完全不可用。"""
    try:
        from src.services.admin_token_service import get_admin_token_version

        return get_admin_token_version()
    except Exception as err:  # noqa: BLE001
        logger.warning("读取管理员令牌版本失败: %s", err)
        return 0


def create_jwt_token(username: str = "admin") -> str:
    """创建 JWT 令牌，有效期 24 小时，携带可吊销的 jti 与令牌版本。"""
    now = datetime.datetime.now(datetime.timezone.utc)
    expiration = now + datetime.timedelta(hours=24)
    payload = {
        "exp": expiration,
        "iat": now,
        "sub": username or "admin",
        "jti": uuid.uuid4().hex,
        "ver": _get_token_version(),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")


def decode_jwt_token(token: str):
    """校验签名与有效期，成功返回 payload，失败返回 None。"""
    if not token:
        return None
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None


def is_token_valid(token: str) -> bool:
    """令牌是否仍然有效：签名/有效期 + 主动吊销 + 令牌版本（改密后旧令牌失效）。"""
    payload = decode_jwt_token(token)
    if not payload:
        return False

    jti = payload.get("jti")
    if jti:
        try:
            from src.services.admin_token_service import is_token_revoked

            if is_token_revoked(str(jti)):
                return False
        except Exception as err:  # noqa: BLE001
            logger.warning("检查令牌吊销状态失败: %s", err)

    try:
        token_version = int(payload.get("ver", 0))
    except (TypeError, ValueError):
        return False
    return token_version == _get_token_version()


def token_required(f):
    """JWT 令牌验证装饰器"""

    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.cookies.get("token")

        def _handle_unauthorized():
            is_api_request = (
                request.path.startswith("/admin/api/")
                or request.path.startswith("/api/")
                or request.is_json
                or request.headers.get("X-Requested-With") == "XMLHttpRequest"
                or "application/json" in request.headers.get("Accept", "")
            )
            if is_api_request:
                return jsonify({"success": False, "message": "未登录或登录态已失效，请重新登录"}), 401
            return redirect(url_for("auth.login"))

        if not token or not is_token_valid(token):
            return _handle_unauthorized()

        return f(*args, **kwargs)

    return decorated
