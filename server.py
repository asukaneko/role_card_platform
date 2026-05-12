"""
角色卡平台 - 主入口文件
采用渐进式模块化重构，使用新模块替代原有功能
"""
import io
import json
import os
import re
import secrets
import sqlite3
import time
import zipfile
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    session,
    url_for,
)
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFProtect
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix

# 导入新模块
from app.config import (
    PROJECT_ROOT, DATA_DIR, UPLOAD_DIR, AVATAR_DIR, CARD_DIR, DB_PATH,
    MAX_CARD_BYTES, MAX_AVATAR_BYTES, MAX_ZIP_BYTES,
    ALLOWED_AVATAR_EXTENSIONS, IMAGE_SIGNATURES, Config
)
from app.models import init_db, get_db, User, RoleCard, Comment, UserLike, UserFavorite, Reviewer, ReviewQueue, AIReviewConfig, EmailConfig, CardRelation, UserFollow, Notification, Collection, CreatorStats, CardVersion, Report
from app.auth import (
    generate_user_api_token, resolve_api_user, api_token_valid,
    admin_token, get_current_user, login_required, AuthService,
    get_or_create_admin_user, is_admin_user
)
from app.utils import (
    ensure_dirs, slugify, unique_slug, normalize_tags, limit_text,
    validate_image_content, save_avatar, save_avatar_bytes,
    extract_zip_cards, card_from_json_upload, to_export_json
)
from app.card_utils import normalize_role_card_data, card_from_form, validate_card
from app.ai_review import AIReviewer
from app.email_service import send_code, verify_code, is_valid_email
from app.email_queue import EmailQueue, queue_register_success_email, queue_login_alert_email

# 创建 Flask 应用
server = Flask(__name__, template_folder="app/templates", static_folder="app/static")
server.config.from_object(Config)
server.debug = True
# 初始化邮件队列表并启动工作线程
EmailQueue.init_db()
EmailQueue.start_worker()

# 初始化安全设置
Config.init_app(server)

# 配置ProxyFix（仅在可信代理后启用）
if os.getenv("ROLE_CARD_BEHIND_PROXY", "").lower() in {"1", "true", "yes", "on"}:
    server.wsgi_app = ProxyFix(server.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# 初始化CSRF保护
csrf = CSRFProtect(server)

# 初始化速率限制器
limiter = Limiter(
    get_remote_address,
    app=server,
    default_limits=Config.RATELIMIT_DEFAULT_LIMITS,
    storage_uri=Config.RATELIMIT_STORAGE_URI,
)


# 应用启动时初始化
@server.before_request
def init_admin_user():
    """确保 admin 用户存在"""
    # 使用一个标志位确保只执行一次
    if not hasattr(server, '_admin_initialized'):
        get_or_create_admin_user()
        server._admin_initialized = True


@server.route("/register", methods=["GET", "POST"])
@limiter.limit("5 per minute")
def register():
    if request.method == "GET":
        return render_template("register.html")

    username = (request.form.get("username") or "").strip()
    email = (request.form.get("email") or "").strip().lower()
    verification_code = (request.form.get("verification_code") or "").strip()
    password = request.form.get("password") or ""
    confirm = request.form.get("confirm") or ""

    if not username or not password or not email:
        flash("用户名、邮箱和密码不能为空", "error")
        return redirect(url_for("register"))

    if not is_valid_email(email):
        flash("邮箱格式不正确", "error")
        return redirect(url_for("register"))

    if len(username) < 3 or len(username) > 24:
        flash("用户名长度需在 3-24 个字符之间", "error")
        return redirect(url_for("register"))

    if not re.match(r"^[a-zA-Z0-9_\u4e00-\u9fff]+$", username):
        flash("用户名只能包含字母、数字、下划线和中文", "error")
        return redirect(url_for("register"))

    if len(password) < 6:
        flash("密码长度至少 6 个字符", "error")
        return redirect(url_for("register"))

    if password != confirm:
        flash("两次输入的密码不一致", "error")
        return redirect(url_for("register"))

    # 验证邮箱验证码
    if not verify_code(email, verification_code):
        flash("验证码错误或已过期，请重新获取", "error")
        return redirect(url_for("register"))

    with get_db() as db:
        existing = db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        if existing:
            flash("该用户名已被注册", "error")
            return redirect(url_for("register"))

        existing_email = db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if existing_email:
            # 不暴露邮箱已注册，统一提示注册信息无效
            flash("注册信息无效或验证码错误", "error")
            return redirect(url_for("register"))

        now = datetime.now().isoformat(timespec="seconds")
        password_hash = generate_password_hash(password)
        api_token = generate_user_api_token()
        db.execute(
            "INSERT INTO users (username, password_hash, display_name, bio, api_token, email, email_verified, created_at) VALUES (?, ?, ?, '', ?, ?, 1, ?)",
            (username, password_hash, username, api_token, email, now),
        )
        db.commit()
        user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()

    session["user_id"] = user["id"]

    # 异步发送注册成功邮件
    try:
        queue_register_success_email(email, username)
    except Exception:
        pass

    flash("注册成功，欢迎加入！")
    return redirect(url_for("index"))


@server.route("/send-verification-code", methods=["POST"])
@limiter.limit("5 per minute")
def send_verification_code():
    """发送邮箱验证码"""
    email = (request.form.get("email") or "").strip().lower()

    if not email:
        return jsonify({"success": False, "error": "请输入邮箱地址"}), 400

    if not is_valid_email(email):
        return jsonify({"success": False, "error": "邮箱格式不正确"}), 400

    try:
        # 检查邮箱是否已被注册（不暴露给用户，统一返回成功提示）
        with get_db() as db:
            existing = db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if existing:
            # 不告诉攻击者邮箱已注册，统一返回成功提示
            return jsonify({"success": True, "message": "如果该邮箱可用，验证码将会发送"})

        success, message = send_code(email)
        if success:
            return jsonify({"success": True, "message": "如果该邮箱可用，验证码将会发送"})
        else:
            status_code = 429 if "频繁" in message else 400
            return jsonify({"success": False, "error": message}), status_code
    except Exception as e:
        # 不暴露内部异常信息
        print(f"[发送验证码错误] {str(e)}")
        return jsonify({"success": False, "error": "服务暂时不可用，请稍后再试"}), 500


@server.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute")
def login():
    if request.method == "GET":
        return render_template("login.html")

    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""

    if not username or not password:
        flash("请输入用户名和密码", "error")
        return redirect(url_for("login"))

    with get_db() as db:
        user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()

    if not user or not check_password_hash(user["password_hash"], password):
        flash("用户名或密码错误", "error")
        return redirect(url_for("login"))

    # 老用户兼容：登录时自动补全 API Token
    if not user["api_token"]:
        with get_db() as db:
            new_token = generate_user_api_token()
            db.execute("UPDATE users SET api_token = ? WHERE id = ?", (new_token, user["id"]))
            db.commit()

    session["user_id"] = user["id"]

    # 异步发送登录提醒邮件
    try:
        if user.get("email"):
            from app.email_service import get_client_ip
            login_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            queue_login_alert_email(
                user["email"],
                user["display_name"] or user["username"],
                get_client_ip(),
                login_time
            )
    except Exception:
        pass

    flash(f"欢迎回来，{user['display_name'] or user['username']}！")
    return redirect(url_for("index"))


@server.route("/logout", methods=["POST"])
def logout():
    session.pop("user_id", None)
    flash("已退出登录")
    return redirect(url_for("index"))


@server.route("/user/<username>")
def user_profile(username):
    tab = request.args.get("tab", "cards")
    status_filter = request.args.get("status", "all")  # 状态筛选：all, approved, pending, draft, rejected
    with get_db() as db:
        user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if not user:
            abort(404)
        is_self = session.get("user_id") == user["id"]
        is_admin = is_admin_user(session.get("user_id"))
        
        # 统计各状态数量（仅自己查看时，基于所有卡片统计）
        status_counts = {"all": 0, "approved": 0, "pending": 0, "draft": 0, "rejected": 0}
        
        if is_self:
            # 先查询所有卡片用于统计数量
            all_rows = db.execute(
                "SELECT * FROM role_cards WHERE user_id = ? ORDER BY created_at DESC",
                (user["id"],),
            ).fetchall()
            status_counts["all"] = len(all_rows)
            for row in all_rows:
                status = dict(row).get("status", "approved")
                if status in status_counts:
                    status_counts[status] += 1
            
            # 根据筛选条件返回对应卡片
            if status_filter and status_filter != "all":
                rows = db.execute(
                    "SELECT * FROM role_cards WHERE user_id = ? AND status = ? ORDER BY created_at DESC",
                    (user["id"], status_filter),
                ).fetchall()
            else:
                rows = all_rows
        else:
            # 其他人查看：只显示已审核通过且公开的角色卡
            rows = db.execute(
                "SELECT * FROM role_cards WHERE user_id = ? AND visibility = 'public' AND status = 'approved' ORDER BY created_at DESC",
                (user["id"],),
            ).fetchall()
    cards = [RoleCard.row_to_card(row) for row in rows]
    favorite_cards = UserFavorite.get_by_user(user["id"]) if is_self else []

    # 关注相关数据
    follower_count = UserFollow.get_follower_count(user["id"])
    following_count = UserFollow.get_following_count(user["id"])
    is_following = False
    current_user_id = session.get("user_id")
    if current_user_id and not is_self:
        is_following = UserFollow.is_following(current_user_id, user["id"])

    # 关注列表
    following_list = []
    if is_self and tab == "following":
        following_list = UserFollow.get_following(user["id"])

    # 合集列表（仅自己查看时传递，用于卡片加入合集功能）
    collections = []
    if is_self:
        collections = Collection.get_by_user(user["id"], include_private=True)
    elif tab == "collections":
        collections = Collection.get_by_user(user["id"], include_private=False)

    return render_template("user_profile.html", profile_user=dict(user), cards=cards, favorite_cards=favorite_cards, is_self=is_self, tab=tab, status_filter=status_filter, status_counts=status_counts, is_admin=is_admin, follower_count=follower_count, following_count=following_count, is_following=is_following, following_list=following_list, collections=collections)


@server.route("/feed")
@login_required
def feed():
    """动态页面 - 展示关注者的角色卡"""
    current = get_current_user()
    if not current:
        flash("请先登录", "error")
        return redirect(url_for("login"))

    following_ids = []
    with get_db() as db:
        rows = db.execute(
            "SELECT following_id FROM user_follows WHERE follower_id = ?",
            (current["id"],)
        ).fetchall()
        following_ids = [r["following_id"] for r in rows]

    cards = []
    if following_ids:
        placeholders = ",".join("?" for _ in following_ids)
        with get_db() as db:
            rows = db.execute(
                f"""
                SELECT rc.*, u.username as owner_username
                FROM role_cards rc
                LEFT JOIN users u ON rc.user_id = u.id
                WHERE rc.user_id IN ({placeholders})
                AND rc.visibility = 'public' AND rc.status = 'approved'
                ORDER BY rc.created_at DESC
                """,
                following_ids
            ).fetchall()
        cards = [RoleCard.row_to_card(row) for row in rows]

    return render_template("feed.html", cards=cards, current_user=current)


@server.route("/user/<int:user_id>/follow", methods=["POST"])
@login_required
def follow_user(user_id):
    """关注用户"""
    current = get_current_user()
    if not current:
        return jsonify({"error": "请先登录"}), 401
    if current["id"] == user_id:
        return jsonify({"error": "不能关注自己"}), 400
    UserFollow.follow(current["id"], user_id)
    # 异步记录粉丝变化
    try:
        CreatorStats.record_followers(user_id)
    except Exception:
        pass
    return jsonify({"following": True})


@server.route("/user/<int:user_id>/unfollow", methods=["POST"])
@login_required
def unfollow_user(user_id):
    """取消关注用户"""
    current = get_current_user()
    if not current:
        return jsonify({"error": "请先登录"}), 401
    UserFollow.unfollow(current["id"], user_id)
    # 异步记录粉丝变化
    try:
        CreatorStats.record_followers(user_id)
    except Exception:
        pass
    return jsonify({"following": False})


@server.route("/user/<username>/regen-token", methods=["POST"])
@login_required
def regen_api_token(username):
    user = get_current_user()
    if not user or user["username"] != username:
        abort(403)
    new_token = generate_user_api_token()
    with get_db() as db:
        db.execute("UPDATE users SET api_token = ? WHERE id = ?", (new_token, user["id"]))
        db.commit()
    flash("API Token 已重新生成", "success")
    return redirect(url_for("user_profile", username=username))


@server.route("/user/<username>/stats")
@login_required
def creator_stats(username):
    """创作者数据面板页面"""
    current = get_current_user()
    if not current or current["username"] != username:
        abort(403)

    user = User.get_by_username(username)
    if not user:
        abort(404)

    # 获取创作者统计数据
    summary = CreatorStats.get_creator_summary(user["id"])
    trend_data = CreatorStats.get_user_cards_trend(user["id"], days=30)
    follower_trend = CreatorStats.get_user_follower_trend(user["id"], days=30)
    top_cards = CreatorStats.get_top_cards(user["id"], limit=5)

    return render_template(
        "creator_stats.html",
        profile_user=user,
        summary=summary,
        trend_data=trend_data,
        follower_trend=follower_trend,
        top_cards=top_cards,
    )


@server.route("/user/<username>/edit", methods=["GET", "POST"])
@login_required
def edit_profile(username):
    user = get_current_user()
    if not user or user["username"] != username:
        abort(403)
    
    if request.method == "POST":
        display_name = (request.form.get("display_name") or "").strip()
        bio = (request.form.get("bio") or "").strip()
        
        # 处理头像上传
        avatar_path = user["avatar_path"]
        avatar_file = request.files.get("avatar")
        if avatar_file and avatar_file.filename:
            try:
                avatar_path = save_avatar(avatar_file)
            except ValueError as e:
                flash(str(e), "error")
                return redirect(url_for("edit_profile", username=username))
        
        with get_db() as db:
            db.execute(
                "UPDATE users SET display_name = ?, bio = ?, avatar_path = ? WHERE id = ?",
                (display_name, bio, avatar_path, user["id"])
            )
            db.commit()
        flash("资料已更新")
        return redirect(url_for("user_profile", username=username))
    
    return render_template("edit_profile.html", user=user)


@server.route("/assets/uploads/avatars/<path:filename>")
def avatar_file(filename):
    """提供头像文件访问 - 只允许访问 uploads/avatars 目录"""
    # 清理文件名，防止路径遍历攻击
    safe_name = secure_filename(filename)
    if not safe_name:
        abort(404)

    # 只允许访问 AVATAR_DIR 目录
    return send_from_directory(AVATAR_DIR, safe_name, max_age=3600)


@server.route("/assets/uploads/cards/<path:filename>")
def card_asset_file(filename):
    """提供角色卡导出文件访问 - 只允许访问 uploads/cards 目录"""
    # 清理文件名，防止路径遍历攻击
    safe_name = secure_filename(filename)
    if not safe_name or not safe_name.endswith(".json"):
        abort(404)

    # 只允许访问 CARD_DIR 目录
    return send_from_directory(CARD_DIR, safe_name, max_age=3600)



@server.route("/")
def landing():
    """首页着陆页"""
    # 获取统计数据
    with get_db() as db:
        total_cards = db.execute(
            "SELECT COUNT(*) as count FROM role_cards WHERE visibility = 'public' AND status = 'approved'"
        ).fetchone()["count"]
        total_authors = db.execute(
            "SELECT COUNT(DISTINCT user_id) as count FROM role_cards WHERE visibility = 'public' AND status = 'approved'"
        ).fetchone()["count"]
        total_downloads = db.execute(
            "SELECT COALESCE(SUM(downloads), 0) as count FROM role_cards WHERE visibility = 'public' AND status = 'approved'"
        ).fetchone()["count"]
        # 获取最近角色卡 - 最多12个（6列x2行）
        recent_rows = db.execute(
            """
            SELECT rc.*, u.username as owner_username
            FROM role_cards rc
            LEFT JOIN users u ON rc.user_id = u.id
            WHERE rc.visibility = 'public' AND rc.status = 'approved'
            ORDER BY rc.created_at DESC
            LIMIT 12
            """
        ).fetchall()

    recent_cards = [RoleCard.row_to_card(row) for row in recent_rows]

    return render_template(
        "landing.html",
        total_cards=total_cards,
        total_authors=total_authors,
        total_downloads=total_downloads,
        recent_cards=recent_cards,
    )


@server.route("/plaza")
def index():
    tag = request.args.get("tag", "").strip()
    sort = request.args.get("sort", "latest")

    where = ["rc.visibility = 'public'", "rc.status = 'approved'"]
    params = []
    if tag:
        where.append("rc.tags_json LIKE ?")
        params.append(f"%{tag}%")

    # 构建排序子句
    if sort == "comments":
        order_by = "comment_count DESC, rc.created_at DESC"
    elif sort == "popular":
        order_by = "rc.downloads DESC, rc.likes DESC, rc.created_at DESC"
    elif sort == "liked":
        order_by = "rc.likes DESC, rc.created_at DESC"
    elif sort == "views":
        order_by = "rc.views DESC, rc.created_at DESC"
    else:
        order_by = "rc.created_at DESC"

    with get_db() as db:
        rows = db.execute(
            f"""
            SELECT rc.*, u.username as owner_username,
                   (SELECT COUNT(*) FROM comments WHERE card_id = rc.id AND status = 'approved') as comment_count
            FROM role_cards rc
            LEFT JOIN users u ON rc.user_id = u.id
            WHERE {' AND '.join(where)}
            ORDER BY {order_by}
            """,
            params,
        ).fetchall()
        tag_rows = db.execute(
            "SELECT tags_json FROM role_cards WHERE visibility = 'public' AND status = 'approved'"
        ).fetchall()

    cards = [RoleCard.row_to_card(row) for row in rows]
    all_tags = []
    for row in tag_rows:
        for item in normalize_tags(json.loads(row["tags_json"] or "[]")):
            if item not in all_tags:
                all_tags.append(item)

    return render_template(
        "index.html",
        cards=cards,
        tag=tag,
        sort=sort,
        all_tags=all_tags,
    )


@server.route("/search")
def search():
    """搜索页面"""
    query = request.args.get("q", "").strip()
    tag = request.args.get("tag", "").strip()
    sort = request.args.get("sort", "latest")
    search_type = request.args.get("type", "all")  # all, name, creator, description

    cards = []
    all_tags = []
    
    if query or tag:
        where = ["rc.visibility = 'public'", "rc.status = 'approved'"]
        params = []
        
        if query:
            if search_type == "name":
                where.append("rc.name LIKE ?")
                params.append(f"%{query}%")
            elif search_type == "creator":
                where.append("(rc.creator LIKE ? OR u.username LIKE ?)")
                params.extend([f"%{query}%", f"%{query}%"])
            elif search_type == "description":
                where.append("(rc.description LIKE ? OR rc.personality LIKE ?)")
                params.extend([f"%{query}%", f"%{query}%"])
            else:
                where.append("(rc.name LIKE ? OR rc.description LIKE ? OR rc.creator LIKE ? OR u.username LIKE ?)")
                term = f"%{query}%"
                params.extend([term, term, term, term])
        
        if tag:
            where.append("rc.tags_json LIKE ?")
            params.append(f"%{tag}%")

        order_by = {
            "popular": "rc.downloads DESC, rc.likes DESC, rc.created_at DESC",
            "liked": "rc.likes DESC, rc.created_at DESC",
            "latest": "rc.created_at DESC",
            "views": "rc.views DESC, rc.created_at DESC",
        }.get(sort, "rc.created_at DESC")

        with get_db() as db:
            rows = db.execute(
                f"""
                SELECT rc.*, u.username as owner_username
                FROM role_cards rc
                LEFT JOIN users u ON rc.user_id = u.id
                WHERE {' AND '.join(where)}
                ORDER BY {order_by}
                """,
                params,
            ).fetchall()
            tag_rows = db.execute(
                "SELECT tags_json FROM role_cards WHERE visibility = 'public' AND status = 'approved'"
            ).fetchall()

        cards = [RoleCard.row_to_card(row) for row in rows]
        for row in tag_rows:
            for item in normalize_tags(json.loads(row["tags_json"] or "[]")):
                if item not in all_tags:
                    all_tags.append(item)

    return render_template(
        "search.html",
        cards=cards,
        q=query,
        tag=tag,
        sort=sort,
        search_type=search_type,
        all_tags=all_tags,
    )


@server.route("/leaderboard")
def leaderboard():
    """排行榜页面"""
    sort_by = request.args.get("sort", "likes")  # likes, views, downloads, newest
    
    # 获取角色卡排行榜
    card_leaderboard = RoleCard.get_leaderboard(sort_by=sort_by, limit=20)
    
    # 获取用户排行榜
    user_leaderboard = User.get_leaderboard(limit=20)
    
    return render_template(
        "leaderboard.html",
        card_leaderboard=card_leaderboard,
        user_leaderboard=user_leaderboard,
        sort_by=sort_by,
    )


@server.route("/card/<identifier>")
def card_detail(identifier):
    user_id = session.get("user_id")

    # 检查是否为 admin 用户
    is_admin = is_admin_user(user_id)

    # 尝试通过API Token获取用户身份（nekobot等API客户端）
    api_user_id = resolve_api_user()
    if api_user_id is not None and api_user_id != 0:
        # API Token有效且不是管理员Token，合并身份
        if not user_id:
            user_id = api_user_id
        is_admin = is_admin or is_admin_user(api_user_id)

    # 先查找卡片（不限状态）：优先按 slug 查找，再按 ID 查找
    with get_db() as db:
        row = db.execute("SELECT * FROM role_cards WHERE slug = ?", (identifier,)).fetchone()
        if not row and str(identifier).isdigit():
            row = db.execute("SELECT * FROM role_cards WHERE id = ?", (identifier,)).fetchone()

    if not row:
        abort(404)

    card = RoleCard.row_to_card(row)
    card_status = card.get("status", "approved")

    # 检查访问权限：
    # 1. 已审核的公开卡片：所有人都可以查看
    # 2. 非 approved 状态：只有所有者、审核员、管理员可以查看
    # 3. 私有卡片：只有所有者和管理员可以查看
    is_owner = user_id and card.get("user_id") == user_id
    is_reviewer = user_id and Reviewer.is_reviewer(user_id)
    # API上传的卡片（user_id为None）允许任何有效API Token查看
    is_api_uploader = api_user_id is not None and row["user_id"] is None

    # 非公开状态需要特殊权限
    if card_status != "approved":
        if not (is_owner or is_reviewer or is_admin or is_api_uploader):
            abort(404)

    # 私有卡片只有所有者和管理员可以查看
    if card["visibility"] != "public":
        if not (is_owner or is_admin):
            abort(404)

    # 增加浏览量（仅已审核的公开卡片），并异步记录到每日统计
    if card_status == "approved" and card.get("visibility") == "public":
        RoleCard.increment_views(card["id"])
        card["views"] = card.get("views", 0) + 1
        try:
            CreatorStats.record_card_view(card["id"])
        except Exception:
            pass

    user_liked = False
    user_favorited = False

    with get_db() as db:
        # 评论查询：普通用户只显示已审核评论，所有者和审核员显示全部
        if is_owner or is_reviewer:
            rows = db.execute(
                "SELECT c.*, u.username, u.display_name FROM comments c "
                "JOIN users u ON c.user_id = u.id "
                "WHERE c.card_id = ? ORDER BY c.created_at ASC",
                (card["id"],),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT c.*, u.username, u.display_name FROM comments c "
                "JOIN users u ON c.user_id = u.id "
                "WHERE c.card_id = ? AND c.status = 'approved' ORDER BY c.created_at ASC",
                (card["id"],),
            ).fetchall()

        # 检查当前用户是否已经喜欢/收藏过该角色
        if user_id:
            like_row = db.execute(
                "SELECT id FROM user_likes WHERE user_id = ? AND card_id = ?",
                (user_id, card["id"])
            ).fetchone()
            user_liked = like_row is not None

            fav_row = db.execute(
                "SELECT id FROM user_favorites WHERE user_id = ? AND card_id = ?",
                (user_id, card["id"])
            ).fetchone()
            user_favorited = fav_row is not None

    comments = []
    for row in rows:
        item = dict(row)
        item["can_delete"] = user_id == row["user_id"]
        comments.append(item)

    # 获取关联的角色卡
    related_cards = CardRelation.get_related_cards(card["id"])
    linked_by_cards = CardRelation.get_linked_by_cards(card["id"])

    # 获取用户的合集列表（用于添加到合集）
    user_collections = []
    if is_owner:
        user_collections = Collection.get_by_user(user_id, include_private=True)

    # 检查是否关注了作者
    is_following_author = False
    if user_id and card.get("user_id"):
        is_following_author = UserFollow.is_following(user_id, card["user_id"])

    return render_template("detail.html", card=card, comments=comments, user_liked=user_liked, user_favorited=user_favorited, current_user_id=user_id, related_cards=related_cards, linked_by_cards=linked_by_cards, is_following_author=is_following_author, is_owner=is_owner, user_collections=user_collections)


@server.route("/card/<int:card_id>/relate", methods=["POST"])
@login_required
def add_card_relation(card_id):
    """关联其他角色卡"""
    card = owner_required(card_id)
    related_card_id = request.form.get("related_card_id", type=int)
    if not related_card_id:
        return jsonify({"error": "请指定要关联的角色卡"}), 400
    related_card = RoleCard.get_or_404(related_card_id)
    CardRelation.add(card_id, related_card_id)
    return jsonify({"success": True})


@server.route("/card/<int:card_id>/unrelate/<int:related_card_id>", methods=["POST"])
@login_required
def remove_card_relation(card_id, related_card_id):
    """取消关联角色卡"""
    card = owner_required(card_id)
    CardRelation.remove(card_id, related_card_id)
    flash("关联已解除")
    return redirect(url_for("card_detail", identifier=card["slug"]))


@server.route("/card/<int:card_id>/related")
def get_related_cards(card_id):
    """获取关联的角色卡列表"""
    cards = CardRelation.get_related_cards(card_id)
    result = []
    for c in cards:
        result.append({
            "id": c["id"],
            "name": c["name"],
            "slug": c["slug"],
            "avatar_path": c.get("avatar_path", ""),
            "description": c.get("description", ""),
        })
    return jsonify({"cards": result})


@server.route("/api/cards/search")
def api_search_cards():
    """搜索角色卡（用于关联时的名称搜索）"""
    query = request.args.get("q", "").strip()
    exclude_id = request.args.get("exclude_id", type=int)
    if not query or len(query) < 1:
        return jsonify({"cards": []})

    term = f"%{query}%"
    with get_db() as db:
        sql = """
            SELECT id, name, slug, avatar_path, description
            FROM role_cards
            WHERE visibility = 'public' AND status = 'approved'
            AND (name LIKE ? OR description LIKE ?)
        """
        params = [term, term]
        if exclude_id:
            sql += " AND id != ?"
            params.append(exclude_id)
        sql += " ORDER BY name LIMIT 10"
        rows = db.execute(sql, params).fetchall()

    result = []
    for row in rows:
        result.append({
            "id": row["id"],
            "name": row["name"],
            "slug": row["slug"],
            "avatar_path": row["avatar_path"] or "",
            "description": (row["description"] or "")[:50],
        })
    return jsonify({"cards": result})


@server.route("/card/<int:card_id>/comment", methods=["POST"])
@login_required
def post_comment(card_id):
    """通过 card_id 提交评论（兼容旧链接）"""
    card = RoleCard.get_or_404(card_id, include_pending=True)
    return _process_comment(card)


@server.route("/card/<slug>/comment", methods=["POST"])
@login_required
def post_comment_by_slug(slug):
    """通过 slug 提交评论"""
    card = RoleCard.get_by_slug(slug, include_pending=True)
    if not card:
        abort(404)
    return _process_comment(card)


def _process_comment(card):
    """处理评论提交的通用逻辑"""
    content = (request.form.get("content") or "").strip()
    if not content:
        flash("评论内容不能为空", "error")
        return redirect(url_for("card_detail", identifier=card["slug"]))
    if len(content) > 1000:
        flash("评论内容不能超过 1000 字", "error")
        return redirect(url_for("card_detail", identifier=card["slug"]))
    now = datetime.now().isoformat(timespec="seconds")
    user_id = session.get("user_id")
    with get_db() as db:
        cursor = db.execute(
            "INSERT INTO comments (card_id, user_id, content, created_at, status) VALUES (?, ?, ?, ?, 'pending')",
            (card["id"], user_id, content, now),
        )
        comment_id = cursor.lastrowid
        db.commit()

    # 给角色卡作者发送通知
    if card.get("user_id") and user_id != card["user_id"]:
        Notification.create(
            user_id=card["user_id"],
            type=Notification.TYPE_CARD_COMMENTED,
            actor_id=user_id,
            card_id=card["id"],
            comment_id=comment_id,
            message=f"您的角色卡「{card['name']}」收到了一条评论"
        )

    # 评论者获得经验值（不能给自己的卡片评论获得经验）
    if user_id and user_id != card.get("user_id"):
        from app.models import User
        User.add_exp(user_id, 3)  # 发表评论获得3经验
    
    flash("评论已提交审核，审核通过后将显示")
    return redirect(url_for("card_detail", identifier=card["slug"]))


@server.route("/comment/<int:comment_id>/delete", methods=["POST"])
@login_required
def delete_comment(comment_id):
    with get_db() as db:
        row = db.execute("SELECT * FROM comments WHERE id = ?", (comment_id,)).fetchone()
        if not row:
            abort(404)
        if row["user_id"] != session.get("user_id"):
            abort(403)
        card_id = row["card_id"]
        db.execute("DELETE FROM comments WHERE id = ?", (comment_id,))
        db.commit()
    flash("评论已删除")
    return redirect(url_for("card_detail", identifier=card_id))


def owner_required(card_id):
    """验证当前用户是卡片所有者，返回卡片数据，否则 403"""
    with get_db() as db:
        row = db.execute("SELECT * FROM role_cards WHERE id = ?", (card_id,)).fetchone()
    if not row:
        abort(404)
    card = RoleCard.row_to_card(row)
    user_id = session.get("user_id")
    if not user_id or card.get("user_id") != user_id:
        abort(403)
    return card


@server.route("/card/<int:card_id>/edit", methods=["GET", "POST"])
@login_required
def edit_card(card_id):
    card = owner_required(card_id)

    if request.method == "GET":
        return render_template("edit.html", card=card)

    try:
        updated = card_from_form(request.form)
        avatar_path = save_avatar(request.files.get("avatar"))

        # 检查是否需要重新提交审核
        action = request.form.get("action", "update")
        resubmit = action == "submit" and card["status"] in ("draft", "rejected")

        now = datetime.now().isoformat(timespec="seconds")
        with get_db() as db:
            sets = [
                "name = ?", "description = ?", "personality = ?",
                "scenario = ?", "first_message = ?", "system_prompt = ?",
                "tags_json = ?", "creator = ?", "visibility = ?",
                "updated_at = ?",
                # NekoBot 扩展字段
                "basic_info = ?", "example_dialogues = ?", "response_format = ?",
                "rules_json = ?", "state_json = ?",
            ]
            params = [
                updated["name"],
                updated["description"],
                updated["personality"],
                updated["scenario"],
                updated["first_message"],
                updated["system_prompt"],
                json.dumps(updated["tags"], ensure_ascii=False),
                updated["creator"],
                updated["visibility"],
                now,
                # NekoBot 扩展字段
                updated["basic_info"],
                updated["example_dialogues"],
                updated["response_format"],
                json.dumps(updated["rules"], ensure_ascii=False),
                json.dumps(updated["state"], ensure_ascii=False),
            ]
            # 如果上传了新头像则更新
            if avatar_path:
                sets.append("avatar_path = ?")
                params.append(avatar_path)
            # 如果名称变了，slug 也需要更新
            if updated["name"] != card["name"]:
                new_slug = unique_slug(db, updated["name"], existing_id=card_id)
                sets.append("slug = ?")
                params.append(new_slug)
            
            # 如果重新提交审核，更新状态为 pending 并清理审核记录
            if resubmit:
                sets.append("status = ?")
                sets.append("reviewed_by = ?")
                sets.append("reviewed_at = ?")
                sets.append("review_result = ?")
                params.extend(["pending", None, None, None])

            params.append(card_id)
            db.execute(
                f"UPDATE role_cards SET {', '.join(sets)} WHERE id = ?",
                params,
            )
            db.commit()
            # 编辑后保存快照，捕获编辑后的新状态
            user_id = session.get("user_id")
            CardVersion.create_snapshot(card_id, user_id)
            # 获取更新后的 slug 用于跳转
            new_row = db.execute("SELECT slug FROM role_cards WHERE id = ?", (card_id,)).fetchone()
            new_slug = new_row["slug"] if new_row else card["slug"]

        if resubmit:
            flash("角色卡已重新提交审核")
        else:
            flash("角色卡已更新")
        return redirect(url_for("card_detail", identifier=new_slug))
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("edit_card", card_id=card_id))


@server.route("/card/<int:card_id>/history")
@login_required
def card_history(card_id):
    """查看角色卡版本历史"""
    card = owner_required(card_id)
    versions = CardVersion.get_versions(card_id)
    return render_template("card_history.html", card=card, versions=versions)


@server.route("/card/version/<int:version_id>")
@login_required
def card_version_detail(version_id):
    """查看单个版本详情"""
    version = CardVersion.get_version(version_id)
    if not version:
        abort(404)
    card = owner_required(version["card_id"])
    return render_template("card_version_detail.html", card=card, version=version)


@server.route("/card/version/compare")
@login_required
def card_version_compare():
    """对比两个版本"""
    v1_id = request.args.get("v1", type=int)
    v2_id = request.args.get("v2", type=int)
    if not v1_id or not v2_id:
        flash("请选择两个版本进行对比", "error")
        return redirect(url_for("index"))

    comparison = CardVersion.compare_versions(v1_id, v2_id)
    if not comparison:
        abort(404)

    card = owner_required(comparison["version1"]["card_id"])
    return render_template("card_version_compare.html", card=card, comparison=comparison)


@server.route("/card/<int:card_id>/rollback/<int:version_id>", methods=["POST"])
@login_required
def card_rollback(card_id, version_id):
    """回滚到指定版本"""
    card = owner_required(card_id)
    success = CardVersion.rollback(card_id, version_id)
    if success:
        flash("角色卡已回滚到指定版本")
    else:
        flash("回滚失败", "error")
    return redirect(url_for("card_detail", identifier=card["slug"]))


@server.route("/card/<int:card_id>/delete", methods=["POST"])
@login_required
def delete_card(card_id):
    card = owner_required(card_id)
    owner_username = card.get("owner_username", "")
    with get_db() as db:
        db.execute("DELETE FROM role_cards WHERE id = ?", (card_id,))
        db.commit()
    flash("角色卡已删除")
    if owner_username:
        return redirect(url_for("user_profile", username=owner_username))
    return redirect(url_for("index"))


@server.route("/card/<int:card_id>/visibility", methods=["POST"])
@login_required
def toggle_visibility(card_id):
    card = owner_required(card_id)
    new_visibility = "private" if card["visibility"] == "public" else "public"
    with get_db() as db:
        db.execute("UPDATE role_cards SET visibility = ?, updated_at = ? WHERE id = ?",
                   (new_visibility, datetime.now().isoformat(timespec="seconds"), card_id))
        db.commit()
    flash(f"角色卡已设为{'公开' if new_visibility == 'public' else '私有'}")
    source = request.form.get("source", "")
    owner_username = card.get("owner_username", "")
    # 设为私有后跳转到用户主页，设为公开后留在卡片详情页
    if new_visibility == "private" and owner_username:
        return redirect(url_for("user_profile", username=owner_username))
    if source == "profile" and owner_username:
        return redirect(url_for("user_profile", username=owner_username))
    return redirect(url_for("card_detail", identifier=card["slug"]))


@server.route("/upload", methods=["GET", "POST"])
@login_required
def upload():
    if request.method == "GET":
        return render_template("upload.html")

    try:
        # 根据表单提交的动作决定状态
        action = request.form.get("action", "draft")
        card_status = "draft" if action == "draft" else "pending"
        
        card_file = request.files.get("card_file")
        if card_file and card_file.filename:
            if card_file.filename.lower().endswith(".zip"):
                imported_cards = extract_zip_cards(card_file)
                user_id = session.get("user_id")
                saved_cards = [RoleCard.create(card, avatar_path, user_id=user_id, status=card_status) for card, avatar_path in imported_cards]
                flash(f"Imported {len(saved_cards)} role card(s) from ZIP")
                if len(saved_cards) == 1:
                    return redirect(url_for("card_detail", identifier=saved_cards[0]["slug"]))
                return redirect(url_for("index"))
            card = card_from_json_upload(card_file)
            form_card = card_from_form(request.form)
            for key, value in form_card.items():
                if value:
                    card[key] = value
        else:
            card = card_from_form(request.form)

        validate_card(card)
        avatar_path = save_avatar(request.files.get("avatar"))

        # 登录用户上传时，自动填充作者为用户名
        user_id = session.get("user_id")
        current = get_current_user()
        if not card.get("creator"):
            if current:
                card["creator"] = current["display_name"] or current["username"]

        now = datetime.now().isoformat(timespec="seconds")
        
        with get_db() as db:
            slug = unique_slug(db, card["name"])
            db.execute(
                """
                INSERT INTO role_cards (
                    name, slug, avatar_path, description, personality, scenario,
                    first_message, system_prompt, tags_json, creator, visibility,
                    user_id, created_at, updated_at,
                    basic_info, example_dialogues, response_format, rules_json, state_json,
                    status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    card["name"],
                    slug,
                    avatar_path,
                    card["description"],
                    card["personality"],
                    card["scenario"],
                    card["first_message"],
                    card["system_prompt"],
                    json.dumps(card["tags"], ensure_ascii=False),
                    card["creator"],
                    card["visibility"],
                    user_id,
                    now,
                    now,
                    # NekoBot 扩展字段
                    card.get("basic_info", ""),
                    card.get("example_dialogues", ""),
                    card.get("response_format", ""),
                    json.dumps(card.get("rules", []), ensure_ascii=False),
                    json.dumps(card.get("state", {}), ensure_ascii=False),
                    card_status,
                ),
            )
            db.commit()
            # 创建初始版本快照 v1
            new_card = db.execute("SELECT id FROM role_cards WHERE slug = ?", (slug,)).fetchone()
            if new_card:
                CardVersion.create_snapshot(new_card["id"], user_id)

        # 增加经验值奖励（非草稿状态）
        if card_status != "draft" and user_id:
            from app.models import User
            exp_result = User.add_exp(user_id, 50)  # 上传角色卡获得50经验
            if exp_result and exp_result.get("level_up"):
                flash(f"🎉 恭喜升级到 Lv{exp_result['new_level']}！", "success")

        if card_status == "draft":
            flash("草稿已保存")
        else:
            flash("角色卡已提交审核，审核通过后将自动发布")
        
        if current:
            return redirect(url_for("user_profile", username=current["username"]))
        return redirect(url_for("index"))
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("upload"))


@server.route("/api/cards", methods=["POST"])
@limiter.limit("10 per minute")
@csrf.exempt  # API接口使用Token认证，豁免CSRF
def api_create_card():
    # API接口只接受API Token认证，不使用session（防止CSRF攻击）
    user_id = resolve_api_user()
    # user_id 为 None 表示无效Token；0 表示管理员Token（允许上传）
    if user_id is None:
        return jsonify({"success": False, "error": "需要提供有效的 API Token"}), 403

    try:
        # 检查是否上传了ZIP文件（NekoBot格式）
        card_file = request.files.get("card_file") or request.files.get("file")
        avatar_path = ""

        # API上传的角色卡保持pending审核状态，但上传者可通过API Token查看
        # user_id为0表示管理员Token，转为None表示无归属用户
        actual_user_id = user_id if user_id != 0 else None
        status = "pending"

        if card_file and card_file.filename:
            if card_file.filename.lower().endswith(".zip"):
                # 处理NekoBot ZIP格式
                imported_cards = extract_zip_cards(card_file)
                if not imported_cards:
                    return jsonify({"success": False, "error": "ZIP文件中没有找到有效的角色卡"}), 400
                # ZIP导入通常只包含一个角色卡
                card_data, avatar_path = imported_cards[0]
                saved = RoleCard.create(card_data, avatar_path, user_id=actual_user_id, status=status)
                return jsonify(
                    {
                        "success": True,
                        "card": saved,
                        "url": url_for("card_detail", identifier=saved["slug"], _external=True),
                    }
                )
            elif card_file.filename.lower().endswith(".json"):
                # 处理JSON文件上传
                raw_card = card_from_json_upload(card_file)
                avatar_path = save_avatar(request.files.get("avatar"))
                card = normalize_role_card_data(raw_card, request.form.get("visibility", "public"))
                saved = RoleCard.create(card, avatar_path, user_id=actual_user_id, status=status)
                return jsonify(
                    {
                        "success": True,
                        "card": saved,
                        "url": url_for("card_detail", identifier=saved["slug"], _external=True),
                    }
                )

        # 处理直接JSON数据上传
        avatar_path = save_avatar(request.files.get("avatar"))
        if request.is_json:
            payload = request.get_json(silent=True) or {}
            raw_card = payload.get("character") or payload.get("card") or payload
        else:
            character_text = request.form.get("character") or request.form.get("card_json") or "{}"
            raw_card = json.loads(character_text)

        if not isinstance(raw_card, dict):
            return jsonify({"success": False, "error": "Card JSON must be an object"}), 400

        card = normalize_role_card_data(raw_card, request.form.get("visibility", "public"))
        saved = RoleCard.create(card, avatar_path, user_id=actual_user_id, status=status)
        return jsonify(
            {
                "success": True,
                "card": saved,
                "url": url_for("card_detail", identifier=saved["slug"], _external=True),
            }
        )
    except json.JSONDecodeError:
        return jsonify({"success": False, "error": "Invalid JSON"}), 400
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except Exception:
        # 生产环境不暴露详细错误信息
        return jsonify({"success": False, "error": "Upload failed"}), 500


@server.route("/card/<int:card_id>/download")
def download_card(card_id):
    card = RoleCard.get_or_404(card_id)
    with get_db() as db:
        db.execute("UPDATE role_cards SET downloads = downloads + 1 WHERE id = ?", (card_id,))
        db.commit()

    # 异步记录到每日统计
    try:
        CreatorStats.record_card_download(card_id)
    except Exception:
        pass
    
    # 给卡片作者增加经验值（不能给自己下载获得经验）
    try:
        user_id = session.get("user_id")
        if card.get("user_id") and card["user_id"] != user_id:
            from app.models import User
            User.add_exp(card["user_id"], 2)  # 被下载获得2经验
    except Exception:
        pass

    export_path = CARD_DIR / f"{card['slug']}.json"
    export_path.write_text(
        json.dumps(to_export_json(card), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return send_file(export_path, as_attachment=True, download_name=f"{card['slug']}.json")


@server.route("/card/<int:card_id>/download-nekozip")
def download_nekozip(card_id):
    card = RoleCard.get_or_404(card_id)
    with get_db() as db:
        db.execute("UPDATE role_cards SET downloads = downloads + 1 WHERE id = ?", (card_id,))
        db.commit()

    # 异步记录到每日统计
    try:
        CreatorStats.record_card_download(card_id)
    except Exception:
        pass

    memory_file = io.BytesIO()
    with zipfile.ZipFile(memory_file, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("character.json", json.dumps(to_export_json(card), ensure_ascii=False, indent=2))
        if card.get("avatar_path"):
            avatar_path = PROJECT_ROOT / card["avatar_path"]
            if avatar_path.exists() and avatar_path.resolve().is_relative_to(PROJECT_ROOT):
                zf.write(avatar_path, f"portrait{avatar_path.suffix}")
    memory_file.seek(0)
    return send_file(
        memory_file,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"{card['slug']}_nekobot.zip",
    )


def _generate_qr_svg(data: str, module_size: int = 4) -> str:
    """Generate a scannable local QR code as SVG."""
    try:
        import qrcode

        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=module_size,
            border=0,
        )
        qr.add_data(data)
        qr.make(fit=True)
        matrix = qr.get_matrix()
        size = len(matrix) * module_size
        rects = []
        for y, row in enumerate(matrix):
            for x, dark in enumerate(row):
                if dark:
                    rects.append(
                        f'<rect x="{x * module_size}" y="{y * module_size}" '
                        f'width="{module_size}" height="{module_size}" fill="#1a1a2e"/>'
                    )
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
            f'viewBox="0 0 {size} {size}">'
            f'<rect width="{size}" height="{size}" fill="white"/>'
            f'{"".join(rects)}</svg>'
        )
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"qrcode QR generation failed: {e}")
        try:
            import segno

            qr = segno.make(data, error='m')
            out = io.StringIO()
            qr.save(out, kind='svg', scale=module_size, dark='#1a1a2e', light='white')
            svg_content = out.getvalue()
            out.close()
            if svg_content.startswith('<?xml'):
                svg_content = svg_content[svg_content.find('<svg'):]
            if 'xmlns=' not in svg_content:
                svg_content = svg_content.replace('<svg', '<svg xmlns="http://www.w3.org/2000/svg"')
            return svg_content
        except Exception as fallback_error:
            logging.getLogger(__name__).error(f"segno QR generation failed: {fallback_error}")
            return '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 100 100"><rect width="100" height="100" fill="white"/></svg>'


@server.route("/card/<int:card_id>/share-image")
def share_card_image(card_id):
    """生成角色卡分享图片"""
    from io import BytesIO as _BytesIO
    from html import escape

    card = RoleCard.get_or_404(card_id)

    card_url = url_for("card_detail", identifier=card["slug"], _external=True)
    creator = escape((card.get("creator") or card.get("owner_username") or "匿名作者")[:48])
    description = escape((card.get("description") or "")[:120])
    name = escape(card["name"][:32])
    tags = escape(" ".join(f"#{t}" for t in (card.get("tags") or [])[:5]))
    card_url_text = escape(card_url)

    # 生成二维码SVG
    qr_svg = _generate_qr_svg(card_url, module_size=4)
    # 提取segno生成的SVG中的path元素，直接内联到分享图片中
    import re
    # segno使用<path>元素绘制二维码，提取所有path
    qr_paths = re.findall(r'<path[^>]+/>', qr_svg)
    # 也尝试提取viewBox以确定尺寸
    qr_vb_match = re.search(r'viewBox="0 0 (\d+) (\d+)"', qr_svg)
    qr_width_match = re.search(r'width="(\d+)"', qr_svg)
    qr_height_match = re.search(r'height="(\d+)"', qr_svg)
    if qr_vb_match:
        qr_w = int(qr_vb_match.group(1))
        qr_h = int(qr_vb_match.group(2))
    elif qr_width_match and qr_height_match:
        qr_w = int(qr_width_match.group(1))
        qr_h = int(qr_height_match.group(1))
    else:
        qr_w = qr_h = 100
    qr_content = "\n".join(qr_paths)
    # 如果没有path，尝试提取rect
    if not qr_content:
        qr_rects = re.findall(r'<rect[^>]+/>', qr_svg)
        qr_content = "\n".join(qr_rects)

    # 角色立绘背景
    avatar_bg = ""
    if card.get("avatar_path"):
        try:
            import base64
            import mimetypes

            avatar_path = PROJECT_ROOT / card["avatar_path"]
            resolved_avatar = avatar_path.resolve()
            if resolved_avatar.exists() and resolved_avatar.is_relative_to(PROJECT_ROOT):
                mime_type = mimetypes.guess_type(resolved_avatar.name)[0] or "image/png"
                avatar_b64 = base64.b64encode(resolved_avatar.read_bytes()).decode("ascii")
                avatar_href = f"data:{mime_type};base64,{avatar_b64}"
            else:
                avatar_href = ""
        except Exception:
            avatar_href = ""

        # 立绘直接嵌入 SVG，避免 <img> 预览时浏览器阻止加载外部图片。
        avatar_bg = f'''<image x="0" y="0" width="640" height="360" preserveAspectRatio="xMidYMid slice" href="{avatar_href}" xlink:href="{avatar_href}" opacity="0.55"/>
  <rect x="0" y="0" width="640" height="360" fill="url(#bgGrad)" opacity="0.65" rx="20"/>'''
    else:
        avatar_bg = '<rect width="640" height="360" fill="url(#bgGrad)" rx="20"/>'

    # 美化后的分享卡片SVG
    svg_content = f'''<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="640" height="360" viewBox="0 0 640 360">
  <defs>
    <linearGradient id="bgGrad" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" style="stop-color:#0a0a18;stop-opacity:1" />
      <stop offset="40%" style="stop-color:#0f0f22;stop-opacity:1" />
      <stop offset="100%" style="stop-color:#151530;stop-opacity:1" />
    </linearGradient>
    <linearGradient id="accentGrad" x1="0%" y1="0%" x2="100%" y2="0%">
      <stop offset="0%" style="stop-color:#e94560;stop-opacity:1" />
      <stop offset="100%" style="stop-color:#ff6b6b;stop-opacity:1" />
    </linearGradient>
    <filter id="shadow" x="-20%" y="-20%" width="140%" height="140%">
      <feDropShadow dx="0" dy="4" stdDeviation="8" flood-color="rgba(0,0,0,0.3)"/>
    </filter>
    <clipPath id="cardClip">
      <rect width="640" height="360" rx="20"/>
    </clipPath>
  </defs>

  <g clip-path="url(#cardClip)">
    <!-- 角色立绘背景 -->
    {avatar_bg}

    <!-- 装饰圆 -->
    <circle cx="580" cy="60" r="120" fill="rgba(233,69,96,0.06)"/>
    <circle cx="60" cy="300" r="80" fill="rgba(255,107,107,0.04)"/>
  </g>

  <!-- 顶部装饰条 -->
  <rect x="40" y="30" width="4" height="40" fill="url(#accentGrad)" rx="2"/>

  <!-- 角色卡名称 -->
  <text x="56" y="58" font-family="system-ui, -apple-system, sans-serif" font-size="28" font-weight="800" fill="#ffffff">{name}</text>

  <!-- 作者 -->
  <text x="56" y="88" font-family="system-ui, -apple-system, sans-serif" font-size="14" fill="rgba(255,255,255,0.7)">by {creator}</text>

  <!-- 描述 -->
  <text x="56" y="125" font-family="system-ui, -apple-system, sans-serif" font-size="13" fill="rgba(255,255,255,0.6)">{description}</text>

  <!-- 标签 -->
  <text x="56" y="158" font-family="system-ui, -apple-system, sans-serif" font-size="12" fill="rgba(233,69,96,0.9)">{tags}</text>

  <!-- 分隔线 -->
  <line x1="56" y1="185" x2="400" y2="185" stroke="rgba(255,255,255,0.15)" stroke-width="1"/>

  <!-- 底部信息 -->
  <text x="56" y="220" font-family="system-ui, -apple-system, sans-serif" font-size="13" font-weight="600" fill="rgba(255,255,255,0.9)">角色卡平台 · Role Card Library</text>
  <text x="56" y="245" font-family="monospace, sans-serif" font-size="11" fill="rgba(255,255,255,0.5)">{card_url_text}</text>

  <!-- 二维码区域 -->
  <g transform="translate(480, 200)">
    <rect x="-12" y="-12" width="124" height="124" fill="white" rx="12" filter="url(#shadow)"/>
    <g transform="translate(-6, -6) scale({112/qr_w:.4f})">
      {qr_content}
    </g>
  </g>

  <!-- 扫码提示 -->
  <text x="524" y="330" font-family="system-ui, -apple-system, sans-serif" font-size="11" fill="rgba(255,255,255,0.6)" text-anchor="middle">扫码查看角色卡</text>
</svg>'''

    response = send_file(
        _BytesIO(svg_content.encode("utf-8")),
        mimetype="image/svg+xml",
        max_age=3600
    )
    return response


@server.route("/card/<int:card_id>/like", methods=["POST"])
@limiter.limit("30 per minute")
def like_card(card_id):
    # 检查用户是否登录
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "请先登录"}), 401
    
    with get_db() as db:
        # 检查用户是否已经喜欢过该角色
        existing = db.execute(
            "SELECT id FROM user_likes WHERE user_id = ? AND card_id = ?",
            (user_id, card_id)
        ).fetchone()
        
        if existing:
            return jsonify({"error": "您已经喜欢过这个角色了"}), 400
        
        # 添加喜欢记录
        now = datetime.now().isoformat()
        db.execute(
            "INSERT INTO user_likes (user_id, card_id, created_at) VALUES (?, ?, ?)",
            (user_id, card_id, now)
        )
        
        # 更新角色卡的喜欢数
        db.execute("UPDATE role_cards SET likes = likes + 1 WHERE id = ?", (card_id,))
        db.commit()

        # 异步记录到每日统计
        try:
            CreatorStats.record_card_like(card_id)
        except Exception:
            pass
        
        # 给卡片作者增加经验值（不能给自己点赞获得经验）
        try:
            card_owner = db.execute("SELECT user_id FROM role_cards WHERE id = ?", (card_id,)).fetchone()
            if card_owner and card_owner["user_id"] and card_owner["user_id"] != user_id:
                from app.models import User
                User.add_exp(card_owner["user_id"], 5)  # 被点赞获得5经验
        except Exception:
            pass

        row = db.execute("SELECT likes FROM role_cards WHERE id = ?", (card_id,)).fetchone()

    if not row:
        abort(404)
    return jsonify({"likes": row["likes"], "liked": True})


@server.route("/card/<int:card_id>/favorite", methods=["POST"])
@limiter.limit("30 per minute")
def toggle_favorite(card_id):
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "请先登录"}), 401

    card = RoleCard.get_or_404(card_id)

    if UserFavorite.exists(user_id, card_id):
        UserFavorite.remove(user_id, card_id)
        return jsonify({"favorited": False})
    else:
        UserFavorite.add(user_id, card_id)
        return jsonify({"favorited": True})


def admin_or_reviewer_required():
    """检查是否为管理员或审核员
    
    管理员通过session验证（必须是admin用户）
    审核员通过session验证
    """
    user_id = session.get("user_id")
    is_admin = is_admin_user(user_id)
    is_reviewer_user = user_id and Reviewer.is_reviewer(user_id)
    
    return is_admin, is_reviewer_user, None


@server.route("/admin/login", methods=["POST"])
def admin_login():
    """管理员登录处理"""
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "").strip()
    
    # 确保 admin 用户存在
    get_or_create_admin_user()
    
    # 验证登录
    user, error = AuthService.login(username, password)
    if error or user["username"] != "admin":
        flash("用户名或密码错误", "error")
        return render_template("admin_login.html")
    
    # 登录成功，设置 session
    session["user_id"] = user["id"]
    flash("登录成功")
    return redirect(url_for("admin"))


@server.route("/admin")
def admin():
    is_admin, is_reviewer_user, token = admin_or_reviewer_required()
    if not is_admin and not is_reviewer_user:
        return render_template("admin_login.html")
    tab = request.args.get("tab", "cards")
    with get_db() as db:
        if tab == "users":
            # 只有管理员可以查看用户管理
            if not is_admin:
                return redirect(url_for("admin", tab="cards"))
            # 获取用户列表及统计信息
            users = db.execute(
                """
                SELECT u.*, 
                       COUNT(DISTINCT rc.id) as card_count,
                       COUNT(DISTINCT c.id) as comment_count
                FROM users u
                LEFT JOIN role_cards rc ON u.id = rc.user_id
                LEFT JOIN comments c ON u.id = c.user_id
                GROUP BY u.id
                ORDER BY u.created_at DESC
                """
            ).fetchall()
            return render_template("admin.html", users=users, tab=tab, is_admin=is_admin, is_reviewer=is_reviewer_user)
        else:
            rows = db.execute("SELECT * FROM role_cards ORDER BY created_at DESC").fetchall()
            return render_template("admin.html", cards=[RoleCard.row_to_card(row) for row in rows], tab=tab, is_admin=is_admin, is_reviewer=is_reviewer_user)


@server.route("/admin/card/<int:card_id>/<action>", methods=["POST"])
def admin_action(card_id, action):
    is_admin, is_reviewer_user, token = admin_or_reviewer_required()
    if not is_admin and not is_reviewer_user:
        abort(403)
    with get_db() as db:
        if action == "hide":
            db.execute("UPDATE role_cards SET visibility = 'private' WHERE id = ?", (card_id,))
        elif action == "publish":
            db.execute("UPDATE role_cards SET visibility = 'public' WHERE id = ?", (card_id,))
        elif action == "delete":
            db.execute("DELETE FROM role_cards WHERE id = ?", (card_id,))
        else:
            abort(404)
        db.commit()
    # 返回空响应，由前端处理刷新
    return "", 204


@server.route("/admin/batch", methods=["POST"])
def admin_batch():
    is_admin, is_reviewer_user, token = admin_or_reviewer_required()
    if not is_admin and not is_reviewer_user:
        abort(403)
    action = request.form.get("action", "")
    ids_raw = request.form.get("ids", "")
    if not action or not ids_raw:
        flash("请选择操作和目标角色卡", "error")
        return redirect(url_for("admin"))
    try:
        card_ids = [int(x) for x in ids_raw.split(",") if x.strip()]
    except ValueError:
        abort(400)
    if not card_ids:
        flash("请选择至少一张角色卡", "error")
        return redirect(url_for("admin"))
    with get_db() as db:
        placeholders = ",".join("?" for _ in card_ids)
        if action == "hide":
            db.execute(f"UPDATE role_cards SET visibility = 'private' WHERE id IN ({placeholders})", card_ids)
        elif action == "publish":
            db.execute(f"UPDATE role_cards SET visibility = 'public' WHERE id IN ({placeholders})", card_ids)
        elif action == "delete":
            db.execute(f"DELETE FROM role_cards WHERE id IN ({placeholders})", card_ids)
        else:
            abort(404)
        db.commit()
    action_labels = {"hide": "隐藏", "publish": "公开", "delete": "删除"}
    flash(f"已批量{action_labels.get(action, '操作')} {len(card_ids)} 张角色卡")
    return redirect(url_for("admin"))


@server.route("/admin/user/<int:user_id>/delete", methods=["POST"])
def admin_delete_user(user_id):
    # 检查是否为 admin 用户
    if not is_admin_user(session.get("user_id")):
        abort(403)
    with get_db() as db:
        # 删除用户（关联的角色卡和评论会通过外键级联删除）
        db.execute("DELETE FROM users WHERE id = ?", (user_id,))
        db.commit()
    flash("用户已删除")
    return redirect(url_for("admin", tab="users"))


@server.route("/admin/user/batch", methods=["POST"])
def admin_user_batch():
    # 检查是否为 admin 用户
    if not is_admin_user(session.get("user_id")):
        abort(403)
    action = request.form.get("action", "")
    ids_raw = request.form.get("ids", "")
    if not action or not ids_raw:
        flash("请选择操作和目标用户", "error")
        return redirect(url_for("admin", tab="users"))
    try:
        user_ids = [int(x) for x in ids_raw.split(",") if x.strip()]
    except ValueError:
        abort(400)
    if not user_ids:
        flash("请选择至少一个用户", "error")
        return redirect(url_for("admin", tab="users"))
    with get_db() as db:
        placeholders = ",".join("?" for _ in user_ids)
        if action == "delete":
            db.execute(f"DELETE FROM users WHERE id IN ({placeholders})", user_ids)
        else:
            abort(404)
        db.commit()
    flash(f"已批量删除 {len(user_ids)} 个用户")
    return redirect(url_for("admin", tab="users"))


def reviewer_required(f):
    """要求审核员权限的装饰器"""
    @wraps(f)
    def decorated(*args, **kwargs):
        user_id = session.get("user_id")
        if not user_id:
            flash("请先登录", "error")
            return redirect(url_for("login"))
        if not Reviewer.is_reviewer(user_id):
            flash("需要审核员权限", "error")
            return redirect(url_for("index"))
        return f(*args, **kwargs)
    return decorated


@server.route("/review")
@reviewer_required
def review_queue():
    """审核队列页面"""
    tab = request.args.get("tab", "cards")
    page = request.args.get("page", 1, type=int)
    per_page = 10  # 每页显示数量
    
    stats = ReviewQueue.get_stats()

    if tab == "comments":
        items, total = ReviewQueue.get_pending_comments_paginated(page, per_page)
    else:
        items, total = ReviewQueue.get_pending_cards_paginated(page, per_page)
    
    # 计算分页信息
    total_pages = (total + per_page - 1) // per_page
    has_prev = page > 1
    has_next = page < total_pages
    
    # 生成目录（用于快速跳转）
    toc = []
    for i, item in enumerate(items):
        if tab == "comments":
            toc.append({
                "id": f"comment-{item['id']}",
                "title": item.get("card_name", "评论")[:20],
                "author": item.get("username", "匿名")
            })
        else:
            toc.append({
                "id": f"card-{item['id']}",
                "title": item.get('name', '角色卡')[:20],
                "author": item.get('creator') or "匿名"
            })

    return render_template(
        "review_queue.html", 
        items=items, 
        tab=tab, 
        stats=stats,
        page=page,
        total_pages=total_pages,
        has_prev=has_prev,
        has_next=has_next,
        total=total,
        toc=toc
    )


@server.route("/review/card/<int:card_id>/approve", methods=["POST"])
@reviewer_required
def review_approve_card(card_id):
    """批准角色卡"""
    reviewer_id = session.get("user_id")
    result = request.form.get("result", "")
    ReviewQueue.approve_card(card_id, reviewer_id, result)

    # 获取卡片信息和作者 ID
    card = RoleCard.get_by_id(card_id, include_pending=True)
    if card and card.get("user_id"):
        # 给作者增加经验值
        from app.models import User
        exp_result = User.add_exp(card["user_id"], 50)  # 审核通过获得50经验
        
        Notification.create(
            user_id=card["user_id"],
            type=Notification.TYPE_CARD_APPROVED,
            actor_id=reviewer_id,
            card_id=card_id,
            message=f"您的角色卡「{card['name']}」已通过审核"
        )
        
        # 如果升级了，发送升级通知
        if exp_result and exp_result.get("level_up"):
            Notification.create(
                user_id=card["user_id"],
                type=Notification.TYPE_SYSTEM,
                message=f"🎉 恭喜！您的角色卡通过审核，升级到 Lv{exp_result['new_level']}！"
            )

    flash("角色卡已通过审核")
    # 检查是否是 AJAX 请求
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return "", 204
    return redirect(url_for("review_queue", tab="cards"))


@server.route("/review/card/<int:card_id>/reject", methods=["POST"])
@reviewer_required
def review_reject_card(card_id):
    """拒绝角色卡"""
    reviewer_id = session.get("user_id")
    result = request.form.get("result", "")
    ReviewQueue.reject_card(card_id, reviewer_id, result)

    # 获取卡片信息和作者 ID
    card = RoleCard.get_by_id(card_id, include_pending=True)
    if card and card.get("user_id"):
        Notification.create(
            user_id=card["user_id"],
            type=Notification.TYPE_CARD_REJECTED,
            actor_id=reviewer_id,
            card_id=card_id,
            message=f"您的角色卡「{card['name']}」未通过审核：{result or '请查看审核意见'}"
        )

    flash("角色卡已被拒绝")
    # 检查是否是 AJAX 请求
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return "", 204
    return redirect(url_for("review_queue", tab="cards"))


@server.route("/review/comment/<int:comment_id>/approve", methods=["POST"])
@reviewer_required
def review_approve_comment(comment_id):
    """批准评论"""
    reviewer_id = session.get("user_id")
    result = request.form.get("result", "")
    ReviewQueue.approve_comment(comment_id, reviewer_id, result)
    flash("评论已通过审核")
    return redirect(url_for("review_queue", tab="comments"))


@server.route("/review/comment/<int:comment_id>/reject", methods=["POST"])
@reviewer_required
def review_reject_comment(comment_id):
    """拒绝评论"""
    reviewer_id = session.get("user_id")
    result = request.form.get("result", "")
    ReviewQueue.reject_comment(comment_id, reviewer_id, result)
    flash("评论已被拒绝")
    return redirect(url_for("review_queue", tab="comments"))


@server.route("/review/ai-card/<int:card_id>", methods=["POST"])
@reviewer_required
def review_ai_card(card_id):
    """使用AI审核角色卡"""
    card = RoleCard.get_by_id(card_id, include_pending=True)
    if not card:
        abort(404)

    is_approved, result = AIReviewer.review_card(card)

    reviewer_id = session.get("user_id")
    if is_approved:
        ReviewQueue.approve_card(card_id, reviewer_id, f"[AI审核] {result}")
        flash(f"AI审核完成：通过。{result}")
    else:
        ReviewQueue.reject_card(card_id, reviewer_id, f"[AI审核] {result}")
        flash(f"AI审核完成：拒绝。{result}", "error")

    return redirect(url_for("review_queue", tab="cards"))


@server.route("/review/ai-comment/<int:comment_id>", methods=["POST"])
@reviewer_required
def review_ai_comment(comment_id):
    """使用AI审核评论"""
    comment = Comment.get_by_id(comment_id)
    if not comment:
        abort(404)

    is_approved, result = AIReviewer.review_comment(comment["content"])

    reviewer_id = session.get("user_id")
    if is_approved:
        ReviewQueue.approve_comment(comment_id, reviewer_id, f"[AI审核] {result}")
        flash(f"AI审核完成：通过。{result}")
    else:
        ReviewQueue.reject_comment(comment_id, reviewer_id, f"[AI审核] {result}")
        flash(f"AI审核完成：拒绝。{result}", "error")

    return redirect(url_for("review_queue", tab="comments"))


# Admin 审核员管理路由
@server.route("/admin/reviewers", methods=["GET", "POST"])
def admin_reviewers():
    """管理审核员"""
    # 检查是否为 admin 用户
    if not is_admin_user(session.get("user_id")):
        abort(403)

    if request.method == "POST":
        action = request.form.get("action", "")

        if action == "add":
            username = request.form.get("username", "").strip()
            user = User.get_by_username(username)
            if not user:
                flash(f"用户 '{username}' 不存在", "error")
            elif Reviewer.is_reviewer(user["id"]):
                flash(f"用户 '{username}' 已经是审核员", "error")
            else:
                admin_id = session.get("user_id") or 0
                Reviewer.add(user["id"], admin_id)
                flash(f"已添加审核员：{username}")

        elif action == "remove":
            user_id = int(request.form.get("user_id", 0))
            if user_id:
                Reviewer.remove(user_id)
                flash("已移除审核员")

        return redirect(url_for("admin_reviewers"))

    reviewers = Reviewer.list_all()
    return render_template("admin_reviewers.html", reviewers=reviewers)


@server.route("/admin/ai-config", methods=["GET", "POST"])
def admin_ai_config():
    """AI审核配置"""
    # 检查是否为 admin 用户
    if not is_admin_user(session.get("user_id")):
        abort(403)

    if request.method == "POST":
        api_key = request.form.get("api_key", "")
        api_url = request.form.get("api_url", "")
        model = request.form.get("model", "")
        enabled = request.form.get("enabled") == "on"

        AIReviewConfig.update(api_key, api_url, model, enabled)
        flash("AI审核配置已更新")
        return redirect(url_for("admin_ai_config"))

    config = AIReviewConfig.get()
    return render_template("admin_ai_config.html", config=config)


@server.route("/admin/email-config", methods=["GET", "POST"])
def admin_email_config():
    """邮件服务配置"""
    # 检查是否为 admin 用户
    if not is_admin_user(session.get("user_id")):
        abort(403)

    if request.method == "POST":
        smtp_server = request.form.get("smtp_server", "").strip()
        smtp_port = int(request.form.get("smtp_port", "587") or "587")
        smtp_username = request.form.get("smtp_username", "").strip()
        smtp_password = request.form.get("smtp_password", "").strip()
        sender_email = request.form.get("sender_email", "").strip()
        sender_name = request.form.get("sender_name", "角色卡平台").strip()
        use_tls = request.form.get("use_tls") == "on"
        enabled = request.form.get("enabled") == "on"

        # 密码留空则保留旧密码
        if not smtp_password:
            old_config = EmailConfig.get()
            smtp_password = old_config.get("smtp_password", "")

        EmailConfig.update(smtp_server, smtp_port, smtp_username, smtp_password,
                           sender_email, sender_name, use_tls, enabled)
        flash("邮件配置已更新")
        return redirect(url_for("admin_email_config"))

    config = EmailConfig.get()
    # 不将密码传给模板
    safe_config = dict(config)
    safe_config["smtp_password"] = ""
    safe_config["has_password"] = bool(config.get("smtp_password"))
    return render_template("admin_email_config.html", config=safe_config)


@server.route("/notifications")
@login_required
def notifications():
    """通知列表页面"""
    user_id = session.get("user_id")
    notifications_list = Notification.get_by_user(user_id, limit=50)
    return render_template("notifications.html", notifications=notifications_list)


@server.route("/notifications/mark-all-read", methods=["POST"])
@login_required
def mark_all_notifications_read():
    """标记所有通知为已读"""
    user_id = session.get("user_id")
    Notification.mark_all_read(user_id)
    return jsonify({"success": True})


@server.route("/notifications/<int:notification_id>/mark-read", methods=["POST"])
@login_required
def mark_notification_read(notification_id):
    """标记单条通知为已读"""
    user_id = session.get("user_id")
    Notification.mark_read(notification_id, user_id)
    return jsonify({"success": True})


@server.route("/notifications/clear-read", methods=["POST"])
@login_required
def clear_read_notifications():
    """清空已读通知"""
    user_id = session.get("user_id")
    Notification.clear_read(user_id)
    return jsonify({"success": True})


@server.route("/notifications/<int:notification_id>/delete", methods=["POST"])
@login_required
def delete_notification(notification_id):
    """删除单条通知"""
    user_id = session.get("user_id")
    Notification.delete(notification_id, user_id)
    return jsonify({"success": True})


@server.route("/collections")
@login_required
def collections_list():
    """合集列表页面"""
    user_id = session.get("user_id")
    collections = Collection.get_by_user(user_id, include_private=True)
    return render_template("collections_list.html", collections=collections)


@server.route("/collections/new", methods=["GET", "POST"])
@login_required
def create_collection():
    """创建合集"""
    if request.method == "GET":
        return render_template("collection_form.html", collection=None)

    title = (request.form.get("title") or "").strip()
    description = (request.form.get("description") or "").strip()
    visibility = request.form.get("visibility", "public")

    if not title:
        flash("合集名称不能为空", "error")
        return render_template("collection_form.html", collection=None)

    user_id = session.get("user_id")
    collection = Collection.create(title=title, description=description, user_id=user_id, visibility=visibility)
    flash("合集已创建")
    return redirect(url_for("collection_detail", slug=collection["slug"]))


@server.route("/collections/<slug>")
def collection_detail(slug):
    """合集详情页"""
    collection = Collection.get_by_slug(slug)
    if not collection:
        abort(404)

    user_id = session.get("user_id")
    is_owner = user_id and collection["user_id"] == user_id
    is_admin = is_admin_user(user_id)

    # 权限检查：私有合集只有所有者和管理员可以查看
    if collection["visibility"] != "public" and not (is_owner or is_admin):
        abort(404)

    cards = Collection.get_cards(collection["id"])
    is_following_author = False
    if user_id and collection["user_id"]:
        is_following_author = UserFollow.is_following(user_id, collection["user_id"])

    return render_template(
        "collection_detail.html",
        collection=collection,
        cards=cards,
        is_owner=is_owner,
        is_following_author=is_following_author,
    )


@server.route("/collections/<slug>/edit", methods=["GET", "POST"])
@login_required
def edit_collection(slug):
    """编辑合集"""
    collection = Collection.get_by_slug(slug)
    if not collection:
        abort(404)

    user_id = session.get("user_id")
    is_owner = user_id and collection["user_id"] == user_id
    is_admin = is_admin_user(user_id)
    if not (is_owner or is_admin):
        abort(403)

    if request.method == "POST":
        action = request.form.get("action", "")

        if action == "add_card":
            card_id = request.form.get("card_id", type=int)
            if card_id:
                Collection.add_card(collection["id"], card_id)
                flash("角色卡已添加到合集")
            return redirect(url_for("edit_collection", slug=slug))

        if action == "remove_card":
            card_id = request.form.get("card_id", type=int)
            if card_id:
                Collection.remove_card(collection["id"], card_id)
                flash("角色卡已从合集移除")
            return redirect(url_for("edit_collection", slug=slug))

        title = (request.form.get("title") or "").strip()
        description = (request.form.get("description") or "").strip()
        visibility = request.form.get("visibility", "public")

        if not title:
            flash("合集名称不能为空", "error")
            user_cards = RoleCard.get_by_user(user_id)
            collection_cards = Collection.get_cards(collection["id"])
            collection_card_ids = {c["id"] for c in collection_cards}
            return render_template("collection_form.html", collection=collection, user_cards=user_cards, collection_cards=collection_cards, collection_card_ids=collection_card_ids)

        Collection.update(collection["id"], title=title, description=description, visibility=visibility)
        flash("合集已更新")
        return redirect(url_for("collection_detail", slug=title.lower().replace(" ", "-") if title != collection["title"] else slug))

    # GET request
    user_cards = RoleCard.get_by_user(user_id)
    collection_cards = Collection.get_cards(collection["id"])
    collection_card_ids = {c["id"] for c in collection_cards}
    return render_template("collection_form.html", collection=collection, user_cards=user_cards, collection_cards=collection_cards, collection_card_ids=collection_card_ids)


@server.route("/collections/<slug>/delete", methods=["POST"])
@login_required
def delete_collection(slug):
    """删除合集"""
    collection = Collection.get_by_slug(slug)
    if not collection:
        abort(404)

    user_id = session.get("user_id")
    is_owner = user_id and collection["user_id"] == user_id
    if not (is_owner or is_admin_user(user_id)):
        abort(403)

    Collection.delete(collection["id"])
    flash("合集已删除")
    current_user = get_current_user()
    username = current_user["username"] if current_user else "me"
    return redirect(url_for("user_profile", username=username, tab="collections"))


@server.route("/collections/<slug>/add-card", methods=["POST"])
@login_required
def collection_add_card(slug):
    """向合集添加角色卡"""
    collection = Collection.get_by_slug(slug)
    if not collection:
        abort(404)

    user_id = session.get("user_id")
    is_owner = user_id and collection["user_id"] == user_id
    if not (is_owner or is_admin_user(user_id)):
        abort(403)

    card_id = request.form.get("card_id")
    if not card_id:
        flash("请选择角色卡", "error")
        return redirect(url_for("collection_detail", slug=slug))

    Collection.add_card(collection["id"], int(card_id))
    flash("角色卡已添加到合集")
    return redirect(url_for("collection_detail", slug=slug))


@server.route("/collections/<slug>/remove-card/<int:card_id>", methods=["POST"])
@login_required
def collection_remove_card(slug, card_id):
    """从合集移除角色卡"""
    collection = Collection.get_by_slug(slug)
    if not collection:
        abort(404)

    user_id = session.get("user_id")
    is_owner = user_id and collection["user_id"] == user_id
    if not (is_owner or is_admin_user(user_id)):
        abort(403)

    Collection.remove_card(collection["id"], card_id)
    flash("角色卡已从合集移除")
    return redirect(url_for("collection_detail", slug=slug))


@server.route("/collections/<slug>/download", methods=["GET"])
def collection_download(slug):
    """下载合集 ZIP 包"""
    collection = Collection.get_by_slug(slug)
    if not collection:
        abort(404)

    user_id = session.get("user_id")
    is_owner = user_id and collection["user_id"] == user_id
    is_admin = is_admin_user(user_id)

    # 权限检查
    if collection["visibility"] != "public" and not (is_owner or is_admin):
        abort(404)

    cards = Collection.get_cards(collection["id"])
    if not cards:
        flash("合集为空，无法下载", "error")
        return redirect(url_for("collection_detail", slug=slug))

    import zipfile
    import io as io_module

    zip_buffer = io_module.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for card in cards:
            # 生成角色卡 JSON 文件
            card_json = {
                "name": card["name"],
                "description": card["description"],
                "personality": card["personality"],
                "scenario": card["scenario"],
                "first_message": card["first_message"],
                "system_prompt": card["system_prompt"],
                "tags": card["tags"],
                "creator": card["creator"],
                "basic_info": card.get("basic_info", ""),
                "example_dialogues": card.get("example_dialogues", ""),
                "response_format": card.get("response_format", ""),
                "rules": card.get("rules", []),
                "state": card.get("state", {}),
            }
            safe_name = re.sub(r'[\\/*?:"<>|]', "", card["name"]) or f"card_{card['id']}"
            zf.writestr(f"{safe_name}.json", json.dumps(card_json, ensure_ascii=False, indent=2))

            # 包含角色立绘图
            if card.get("avatar_path"):
                avatar_path = PROJECT_ROOT / card["avatar_path"]
                if avatar_path.exists() and avatar_path.resolve().is_relative_to(PROJECT_ROOT):
                    zf.write(avatar_path, f"{safe_name}_portrait{avatar_path.suffix}")

    zip_buffer.seek(0)
    safe_filename = re.sub(r'[\\/*?:"<>|]', "", collection["title"]) or "collection"
    return send_file(
        zip_buffer,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"{safe_filename}.zip"
    )


def _format_relative_time(iso_str: str) -> str:
    """将 ISO 时间字符串转换为相对时间"""
    try:
        from datetime import datetime
        target = datetime.fromisoformat(iso_str)
        now = datetime.now()
        delta = now - target

        if delta.days > 0:
            if delta.days > 30:
                return target.strftime("%Y-%m-%d %H:%M")
            return f"{delta.days}天前"
        hours = delta.seconds // 3600
        if hours > 0:
            return f"{hours}小时前"
        minutes = (delta.seconds % 3600) // 60
        if minutes > 0:
            return f"{minutes}分钟前"
        return "刚刚"
    except Exception:
        return iso_str


def _get_notification_url(n: dict) -> str:
    """根据通知类型生成跳转链接"""
    if n.get("card_id"):
        with get_db() as db:
            card = db.execute("SELECT slug FROM role_cards WHERE id = ?", (n["card_id"],)).fetchone()
            if card:
                return url_for("card_detail", identifier=card["slug"])
    if n.get("actor_id"):
        with get_db() as db:
            actor = db.execute("SELECT username FROM users WHERE id = ?", (n["actor_id"],)).fetchone()
            if actor:
                return url_for("user_profile", username=actor["username"])
    return url_for("notifications")


@server.route("/report", methods=["POST"])
@login_required
def submit_report():
    """提交举报"""
    current = get_current_user()
    if not current:
        return jsonify({"error": "请先登录"}), 401

    target_type = request.form.get("target_type", "").strip()
    target_id = request.form.get("target_id", type=int)
    reason = request.form.get("reason", "").strip()
    description = request.form.get("description", "").strip()

    if not target_type or not target_id or not reason:
        return jsonify({"error": "请填写完整的举报信息"}), 400

    if target_type not in (Report.TARGET_CARD, Report.TARGET_COMMENT, Report.TARGET_USER):
        return jsonify({"error": "无效的举报类型"}), 400

    if reason not in Report.REASON_LABELS:
        return jsonify({"error": "无效的举报原因"}), 400

    report = Report.create(
        target_type=target_type,
        target_id=target_id,
        reporter_id=current["id"],
        reason=reason,
        description=description
    )

    if report:
        return jsonify({"success": True, "message": "举报已提交，管理员会尽快处理"})
    else:
        return jsonify({"error": "举报提交失败"}), 500


@server.route("/admin/reports")
@login_required
def admin_reports():
    """管理后台举报处理队列"""
    if not is_admin_user(session.get("user_id")):
        abort(403)

    status = request.args.get("status", "pending")
    page = request.args.get("page", 1, type=int)
    per_page = 20

    reports = Report.get_list(status=status, limit=per_page, offset=(page - 1) * per_page)
    stats = Report.get_stats()

    # 获取举报目标信息
    for r in reports:
        if r["target_type"] == Report.TARGET_CARD:
            card = RoleCard.get_by_id(r["target_id"], include_pending=True)
            r["target_name"] = card["name"] if card else "未知角色卡"
            r["target_url"] = url_for("card_detail", identifier=card["slug"]) if card else ""
        elif r["target_type"] == Report.TARGET_COMMENT:
            comment = Comment.get_by_id(r["target_id"])
            r["target_name"] = (comment["content"][:30] + "...") if comment and comment.get("content") else "未知评论"
            r["target_url"] = ""
        elif r["target_type"] == Report.TARGET_USER:
            user = User.get_by_id(r["target_id"])
            r["target_name"] = user["username"] if user else "未知用户"
            r["target_url"] = url_for("user_profile", username=user["username"]) if user else ""

    return render_template("admin_reports.html", reports=reports, status=status, stats=stats, Report=Report)


@server.route("/admin/report/<int:report_id>/resolve", methods=["POST"])
@login_required
def admin_resolve_report(report_id):
    """处理举报"""
    if not is_admin_user(session.get("user_id")):
        abort(403)

    resolver_id = session.get("user_id")
    Report.resolve(report_id, resolver_id)
    flash("举报已处理")
    return redirect(url_for("admin_reports"))


@server.route("/admin/report/<int:report_id>/reject", methods=["POST"])
@login_required
def admin_reject_report(report_id):
    """拒绝举报"""
    if not is_admin_user(session.get("user_id")):
        abort(403)

    resolver_id = session.get("user_id")
    Report.reject(report_id, resolver_id)
    flash("举报已拒绝")
    return redirect(url_for("admin_reports"))


@server.context_processor
def inject_globals():
    from app.models import Reviewer
    user = get_current_user()
    is_reviewer = False
    is_admin = False
    unread_notifications = 0
    pending_reports = 0
    if user:
        is_reviewer = Reviewer.is_reviewer(user["id"])
        is_admin = is_admin_user(user["id"])
        unread_notifications = Notification.get_unread_count(user["id"])
        if is_admin:
            pending_reports = Report.get_pending_count()
    return {
        "admin_token": admin_token,
        "current_user": user,
        "is_reviewer": is_reviewer,
        "is_admin": is_admin,
        "unread_notifications": unread_notifications,
        "pending_reports": pending_reports,
        "format_time": _format_relative_time,
        "get_notification_url": _get_notification_url
    }


if __name__ == "__main__":
    init_db()
    port = int(os.getenv("ROLE_CARD_PORT", "7861"))
    debug = os.getenv("ROLE_CARD_DEBUG", "").lower() in {"1", "true", "yes", "on"}
    server.run(host="0.0.0.0", port=port, debug=debug, use_reloader=debug, threaded=True)
