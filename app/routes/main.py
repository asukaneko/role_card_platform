"""
主路由 - 包含首页、搜索、静态资源等
"""
from flask import Blueprint, abort, render_template, request, send_file

from ..config import PROJECT_ROOT
from ..models import RoleCard

bp = Blueprint('main', __name__)


@bp.route("/assets/<path:filename>")
def asset_file(filename):
    """静态资源文件服务"""
    # 清理文件名，防止路径遍历攻击
    safe_parts = []
    for part in filename.replace("\\", "/").split("/"):
        if part == ".." or part.startswith("/") or not part:
            continue
        safe_parts.append(part)
    safe_filename = "/".join(safe_parts)
    if not safe_filename:
        abort(404)

    # 构建目标路径（不使用 resolve() 避免符号链接问题）
    target = PROJECT_ROOT / safe_filename
    target = target.absolute()

    # 确保解析后的路径仍在 PROJECT_ROOT 内
    base_resolved = PROJECT_ROOT.absolute()
    try:
        # 检查目标路径是否以基础路径开头
        if not str(target).startswith(str(base_resolved)):
            abort(404)
    except ValueError:
        abort(404)

    if not target.exists() or not target.is_file():
        abort(404)
    return send_file(target, max_age=0)


@bp.route("/")
def index():
    """首页 - 角色卡广场"""
    query = request.args.get("q", "").strip()
    tag = request.args.get("tag", "").strip()
    sort = request.args.get("sort", "latest")

    cards = RoleCard.search(query=query, tag=tag, sort=sort)
    all_tags = RoleCard.get_all_tags()

    return render_template(
        "index.html",
        cards=cards,
        q=query,
        tag=tag,
        sort=sort,
        all_tags=all_tags,
    )
