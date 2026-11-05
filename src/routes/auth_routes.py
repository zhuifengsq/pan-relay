# src/routes/auth_routes.py

import logging
import os

from flask import Blueprint, jsonify, make_response, redirect, render_template, request, url_for

from src.db.logs import insert_system_log
from src.services.admin_auth_service import (
    change_admin_password,
    get_admin_username,
    is_using_default_password,
    verify_admin_credentials,
)
from src.services.admin_token_service import bump_admin_token_version, revoke_token
from src.utils.auth_utils import (
    create_jwt_token,
    decode_jwt_token,
    is_token_valid,
    token_required,
)
from src.utils.login_throttle import (
    check_login_allowed,
    record_login_failure,
    record_login_success,
)
from src.utils.request_utils import get_client_ip, request_is_secure

logger = logging.getLogger(__name__)

auth_bp = Blueprint('auth', __name__)


def _auth_cookie_secure() -> bool:
    """鉴权 Cookie 是否加 Secure 标记：COOKIE_SECURE 显式配置优先，否则跟随可信 HTTPS 判定。"""
    flag = os.environ.get("COOKIE_SECURE", "").strip().lower()
    if flag in {"1", "true", "yes", "on"}:
        return True
    if flag in {"0", "false", "no", "off"}:
        return False
    return request_is_secure()


def _set_auth_cookie(response, token: str) -> None:
    """统一设置管理员鉴权 Cookie：HttpOnly + SameSite=Lax（可选 Secure），降低 XSS/CSRF 风险。"""
    response.set_cookie(
        'token',
        token,
        httponly=True,
        samesite='Lax',
        secure=_auth_cookie_secure(),
    )


# 登录页面路由
@auth_bp.route('/login', methods=['GET', 'POST'])
@auth_bp.route('/admin', methods=['GET', 'POST'])
def login():
    token = request.cookies.get('token')
    if request.method == 'GET' and token and is_token_valid(token):
        return redirect(url_for('dashboard.dashboard_page'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        client_ip = get_client_ip()

        if not username or not password:
            return render_template('login.html', error='账号或密码不能为空')

        allowed, retry_after = check_login_allowed(client_ip)
        if not allowed:
            logger.warning(f"管理员登录被限速，IP: {client_ip}")
            insert_system_log(
                log_type="admin",
                action="管理员登录限速拦截",
                query_text=f"用户名: {username}",
                status_code=429,
                error_message=f"重试等待 {retry_after} 秒",
                client_ip=client_ip,
            )
            response = make_response(
                render_template('login.html', error=f'登录失败次数过多，请 {retry_after} 秒后再试'),
                429,
            )
            response.headers['Retry-After'] = str(retry_after)
            return response

        is_valid, msg = verify_admin_credentials(username, password)
        if is_valid:
            record_login_success(client_ip)
            token = create_jwt_token(username=username)
            response = redirect(url_for('dashboard.dashboard_page'))
            _set_auth_cookie(response, token)

            logger.info(f"管理员 {username} 登录成功")
            insert_system_log(
                log_type="admin",
                action="管理员登录成功",
                query_text=f"用户名: {username}",
                status_code=200,
                client_ip=client_ip,
            )
            return response
        else:
            record_login_failure(client_ip)
            logger.warning(f"管理员登录失败，用户名: {username}")
            insert_system_log(
                log_type="admin",
                action="管理员登录失败",
                query_text=f"用户名: {username}",
                status_code=401,
                error_message=msg,
                client_ip=client_ip,
            )
            return render_template('login.html', error='账号或密码错误')

    return render_template('login.html')


# 登出路由
@auth_bp.route('/logout')
@auth_bp.route('/admin/logout')
def logout():
    token = request.cookies.get('token')
    if token:
        payload = decode_jwt_token(token)
        if payload and payload.get('jti'):
            revoke_token(payload['jti'], payload.get('exp'))
    response = redirect(url_for('search_index'))
    response.set_cookie('token', '', expires=0)
    return response


# 修改密码接口
@auth_bp.route('/admin/api/change_password', methods=['POST'])
@auth_bp.route('/admin/api/change-password', methods=['POST'])
@token_required
def api_change_password():
    payload = request.get_json(silent=True) or request.form.to_dict() or {}
    old_password = str(payload.get('old_password') or '').strip()
    new_password = str(payload.get('new_password') or '').strip()
    confirm_password = str(payload.get('confirm_password') or '').strip()
    new_username = str(payload.get('username') or '').strip() or None

    if not old_password:
        return jsonify({'success': False, 'message': '请输入当前原密码'}), 400
    if not new_password:
        return jsonify({'success': False, 'message': '请输入新密码'}), 400
    if len(new_password) < 6:
        return jsonify({'success': False, 'message': '新密码长度至少需要 6 位字符'}), 400
    if confirm_password and new_password != confirm_password:
        return jsonify({'success': False, 'message': '两次输入的新密码不一致'}), 400

    success, message = change_admin_password(
        old_password=old_password,
        new_password=new_password,
        new_username=new_username,
    )

    if not success:
        return jsonify({'success': False, 'message': message}), 400

    current_user = new_username or get_admin_username()
    client_ip = get_client_ip()
    insert_system_log(
        log_type="admin",
        action="修改管理员密码成功",
        query_text=f"管理员: {current_user}",
        status_code=200,
        client_ip=client_ip,
    )

    # 改密后递增令牌版本，使所有旧 JWT 立即失效；再为当前会话签发新 Token
    bump_admin_token_version()
    new_token = create_jwt_token(username=current_user)
    resp = jsonify({'success': True, 'message': '密码修改成功'})
    _set_auth_cookie(resp, new_token)
    return resp


# 检查密码状态接口（查询是否为初始默认弱密码）
@auth_bp.route('/admin/api/password_status', methods=['GET'])
@token_required
def api_password_status():
    return jsonify({
        'success': True,
        'data': {
            'username': get_admin_username(),
            'is_default_password': is_using_default_password(),
        }
    })
