from flask import Blueprint, jsonify, render_template, request
import logging
from zipfile import BadZipFile

from src.utils.auth_utils import token_required
from src.services.resource_service import (
    list_resources,
    get_resource_detail,
    add_resource_and_share,
    update_resource_info,
    delete_resource_and_share,
)
from src.services.resource_excel_service import MAX_FILE_BYTES, ResourceExcelError, parse_resource_excel
from src.db.resources import get_cinema_featured_resources, set_cinema_featured_resources

logger = logging.getLogger(__name__)

resources_bp = Blueprint("resources", __name__)


@resources_bp.route("/admin/api/resources/cinema-featured", methods=["GET", "PUT"])
@token_required
def cinema_featured_resources():
    if request.method == "GET":
        return jsonify({"success": True, "data": get_cinema_featured_resources()})
    payload = request.get_json(silent=True) or {}
    success, message = set_cinema_featured_resources(payload.get("resource_ids"))
    if not success:
        return jsonify({"success": False, "message": message}), 400
    return jsonify({"success": True, "data": get_cinema_featured_resources()})


@resources_bp.route("/admin/resources")
@token_required
def resources_page():
    """资源管理页面，需要JWT验证"""
    return render_template("resource.html")


@resources_bp.route("/admin/api/resources", methods=["GET"])
@token_required
def get_resources():
    """获取资源列表，支持分页、搜索与排序功能"""
    page = request.args.get("page", 1, type=int)
    page_size = request.args.get("page_size", 10, type=int)
    search = request.args.get("search", "", type=str)
    sort_by = request.args.get("sort_by", "id", type=str)
    order = request.args.get("order", "desc", type=str)

    success, message, data = list_resources(
        page=page, page_size=page_size, search=search, sort_by=sort_by, order=order
    )
    if not success:
        return jsonify({"success": False, "message": message}), 500
    return jsonify({"success": True, "data": data})


@resources_bp.route("/admin/api/resources/<int:resource_id>", methods=["GET"])
@token_required
def get_resource(resource_id):
    """获取单个资源详情"""
    success, message, resource = get_resource_detail(resource_id)
    if not success:
        status = 404 if message == "资源不存在" else 500
        return jsonify({"success": False, "message": message}), status
    return jsonify({"success": True, "data": resource})


@resources_bp.route("/admin/api/resources", methods=["POST"])
@token_required
def add_resource():
    """添加新资源"""
    resource_data = request.get_json()
    success, message, new_id = add_resource_and_share(resource_data)
    if not success:
        status = 400 if "必填项" in message else 500 if "数据库" in message else 500
        return jsonify({"success": False, "message": message}), status
    return jsonify({"success": True, "message": message, "id": new_id}), 201


@resources_bp.route("/admin/api/resources/import-excel", methods=["POST"])
@token_required
def import_resources_excel():
    """Import resource titles and links from an uploaded .xlsx workbook."""
    upload = request.files.get("file")
    if not upload or not (upload.filename or "").lower().endswith(".xlsx"):
        return jsonify({"success": False, "message": "请选择 .xlsx 文件"}), 400

    content = upload.stream.read(MAX_FILE_BYTES + 1)
    try:
        resources, errors = parse_resource_excel(content)
    except (ResourceExcelError, BadZipFile) as exc:
        message = str(exc) if isinstance(exc, ResourceExcelError) else "文件不是有效的 .xlsx 工作簿"
        return jsonify({"success": False, "message": message}), 400

    if not resources:
        return jsonify({"success": False, "message": "没有识别到可导入的标题和链接", "errors": errors[:20]}), 400

    imported = skipped = 0
    for resource in resources:
        row_number = resource.pop("row")
        sheet_number = resource.pop("sheet")
        resource["origin_type"] = "batch_import"
        resource["save_to_netdisk"] = {}
        success, message, _ = add_resource_and_share(resource)
        if not success:
            errors.append(f"工作表 {sheet_number} 第 {row_number} 行：{message}")
        elif message == "资源链接已存在，无需重复导入":
            skipped += 1
        else:
            imported += 1

    return jsonify({
        "success": True,
        "message": f"导入完成：新增 {imported} 条，重复 {skipped} 条，失败 {len(errors)} 条",
        "imported": imported,
        "skipped": skipped,
        "failed": len(errors),
        "errors": errors[:20],
    })


@resources_bp.route("/admin/api/resources/<int:resource_id>", methods=["PUT"])
@token_required
def update_resource(resource_id):
    """更新资源信息"""
    resource_data = request.get_json()
    success, message = update_resource_info(resource_id, resource_data)
    if not success:
        status = 400 if "必填项" in message else 404 if message == "资源不存在" else 500
        return jsonify({"success": False, "message": message}), status
    return jsonify({"success": True, "message": message})


@resources_bp.route("/admin/api/resources/<int:resource_id>", methods=["DELETE"])
@token_required
def delete_resource(resource_id):
    """删除资源"""
    raw_flag = request.args.get("delete_netdisk_file", None)
    if raw_flag is None:
        payload = request.get_json(silent=True) or {}
        delete_netdisk_file = bool(payload.get("delete_netdisk_file", False))
    else:
        delete_netdisk_file = str(raw_flag).lower() in ("true", "1", "yes")

    success, message = delete_resource_and_share(resource_id, delete_netdisk_file=delete_netdisk_file)
    if not success:
        status = 404 if message == "资源不存在" else 500
        return jsonify({"success": False, "message": message}), status
    return jsonify({"success": True, "message": message})


@resources_bp.route("/admin/api/resources/audit", methods=["POST"])
@token_required
def audit_resources_route():
    """批量巡检入库资源有效性与健康状态"""
    from src.services.resource_health_service import audit_resources_batch

    payload = request.get_json(silent=True) or {}
    ids = payload.get("ids")
    limit = max(1, min(int(payload.get("limit", 100)), 500))
    max_workers = max(1, min(int(payload.get("max_workers", 4)), 10))

    if ids and not isinstance(ids, list):
        return jsonify({"success": False, "message": "ids 必须为资源ID列表"}), 400

    result = audit_resources_batch(resource_ids=ids, limit=limit, max_workers=max_workers)
    return jsonify({"success": True, "message": "巡检完成", "data": result})


@resources_bp.route("/admin/api/resources/<int:resource_id>/check", methods=["POST"])
@token_required
def check_single_resource_route(resource_id):
    """单条资源实时健康测活与更新"""
    from src.services.resource_health_service import audit_single_resource

    success, message, result = audit_single_resource(resource_id)
    if not success:
        status = 404 if message == "资源不存在" else 500
        return jsonify({"success": False, "message": message}), status
    return jsonify({"success": True, "message": message, "data": result})


@resources_bp.route("/admin/api/resources/cleanup-dead", methods=["POST"])
@token_required
def cleanup_dead_resources_route():
    """一键清理标记为失效 (bad) 的死链资源"""
    from src.services.resource_health_service import cleanup_dead_resources

    payload = request.get_json(silent=True) or {}
    dry_run = bool(payload.get("dry_run", False))
    limit = max(1, min(int(payload.get("limit", 100)), 500))

    result = cleanup_dead_resources(dry_run=dry_run, limit=limit)
    msg = f"成功检测到 {result['total_found']} 条失效资源，已清理 {result['cleaned_count']} 条"
    return jsonify({"success": True, "message": msg, "data": result})


@resources_bp.route("/admin/api/resources/set-origin", methods=["POST"])
@token_required
def set_resources_origin_route():
    """批量或单条设置资源来源类型（实现防删锁定 / 设为永久保留 / 设为临时中转）"""
    from src.db.resources import update_resources_origin_type

    payload = request.get_json(silent=True) or {}
    ids = payload.get("ids")
    origin_type = payload.get("origin_type", "manual")

    if not ids or not isinstance(ids, list):
        return jsonify({"success": False, "message": "ids 必须为非空资源ID列表"}), 400

    if origin_type not in ("manual", "batch_import", "relay"):
        return jsonify({"success": False, "message": "无效的来源类型"}), 400

    success, message, count = update_resources_origin_type(ids, origin_type)
    if not success:
        return jsonify({"success": False, "message": message}), 500

    action_label = "设为永久保留（免自动清理）" if origin_type in ("manual", "batch_import") else "设为临时中转（受保留期物理删除管控）"
    return jsonify({
        "success": True,
        "message": f"成功将 {count} 项资源{action_label}",
        "count": count,
        "origin_type": origin_type
    })


@resources_bp.route("/admin/api/resources/health-stats", methods=["GET"])
@token_required
def get_resource_health_stats_route():
    """获取资源库各健康状态数据统计"""
    from src.services.resource_health_service import get_resource_health_overview

    stats = get_resource_health_overview()
    return jsonify({"success": True, "data": stats})


# ==================== 用户求片需求后台管理 ====================

@resources_bp.route("/admin/requests")
@token_required
def requests_page():
    """用户求片需求管理页面"""
    from src.db.resource_requests import get_resource_request_counts
    counts = get_resource_request_counts()
    return render_template("admin_requests.html", counts=counts, active_page="requests")


@resources_bp.route("/admin/api/requests", methods=["GET"])
@token_required
def get_requests_api():
    """获取用户求片列表及统计"""
    from src.db.resource_requests import get_resource_requests, get_resource_request_counts

    page = request.args.get("page", 1, type=int)
    page_size = request.args.get("page_size", 20, type=int)
    status = request.args.get("status", "all", type=str)
    search = request.args.get("search", "", type=str)

    items, total = get_resource_requests(status=status, keyword=search, page=page, page_size=page_size)
    counts = get_resource_request_counts()

    return jsonify({
        "success": True,
        "data": {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "counts": counts,
        }
    })


@resources_bp.route("/admin/api/requests/<int:req_id>/status", methods=["POST"])
@token_required
def update_request_status_api(req_id: int):
    """更新求片状态 (pending / completed / ignored)"""
    from src.db.resource_requests import update_resource_request_status

    payload = request.get_json(silent=True) or {}
    status = str(payload.get("status", "")).strip()

    if status not in {"pending", "completed", "ignored"}:
        return jsonify({"success": False, "message": "无效的状态值"}), 400

    ok = update_resource_request_status(req_id, status)
    if not ok:
        return jsonify({"success": False, "message": "状态更新失败或记录不存在"}), 404

    return jsonify({"success": True, "message": "状态更新成功"})


@resources_bp.route("/admin/api/requests/<int:req_id>", methods=["DELETE"])
@token_required
def delete_request_api(req_id: int):
    """删除求片记录"""
    from src.db.resource_requests import delete_resource_request

    ok = delete_resource_request(req_id)
    if not ok:
        return jsonify({"success": False, "message": "删除失败或记录不存在"}), 404

    return jsonify({"success": True, "message": "删除成功"})


