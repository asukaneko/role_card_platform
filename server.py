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
    session,
    url_for,
)
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix

# 导入新模块
from config import (
    BASE_DIR, DATA_DIR, UPLOAD_DIR, AVATAR_DIR, CARD_DIR, DB_PATH,
    MAX_CARD_BYTES, MAX_AVATAR_BYTES, MAX_ZIP_BYTES,
    ALLOWED_AVATAR_EXTENSIONS, IMAGE_SIGNATURES, Config
)
from models import init_db, get_db, User, RoleCard, Comment, UserLike, UserFavorite, Reviewer, ReviewQueue, AIReviewConfig
from auth import (
    generate_user_api_token, resolve_api_user, api_token_valid,
    admin_token, get_current_user, login_required, AuthService
)
from utils import (
    ensure_dirs, slugify, unique_slug, normalize_tags, limit_text,
    validate_image_content, save_avatar, save_avatar_bytes,
    extract_zip_cards, card_from_json_upload, to_export_json
)
from card_utils import normalize_role_card_data, card_from_form, validate_card
from ai_review import AIReviewer

# 创建 Flask 应用
server = Flask(__name__)
server.config.from_object(Config)
server.wsgi_app = ProxyFix(server.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# 初始化速率限制器
limiter = Limiter(
    get_remote_address,
    app=server,
    default_limits=Config.RATELIMIT_DEFAULT_LIMITS,
    storage_uri=Config.RATELIMIT_STORAGE_URI,
)


@server.route("/register", methods=["GET", "POST"])
@limiter.limit("5 per minute")
def register():
    if request.method == "GET":
        return render_template("register.html")

    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    confirm = request.form.get("confirm") or ""

    if not username or not password:
        flash("用户名和密码不能为空", "error")
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

    with get_db() as db:
        existing = db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        if existing:
            flash("该用户名已被注册", "error")
            return redirect(url_for("register"))

        now = datetime.now().isoformat(timespec="seconds")
        password_hash = generate_password_hash(password)
        api_token = generate_user_api_token()
        db.execute(
            "INSERT INTO users (username, password_hash, display_name, bio, api_token, created_at) VALUES (?, ?, ?, '', ?, ?)",
            (username, password_hash, username, api_token, now),
        )
        db.commit()
        user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()

    session["user_id"] = user["id"]
    flash("注册成功，欢迎加入！")
    return redirect(url_for("index"))


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
    with get_db() as db:
        user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if not user:
            abort(404)
        is_self = session.get("user_id") == user["id"]
        if is_self:
            rows = db.execute(
                "SELECT * FROM role_cards WHERE user_id = ? ORDER BY created_at DESC",
                (user["id"],),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM role_cards WHERE user_id = ? AND visibility = 'public' ORDER BY created_at DESC",
                (user["id"],),
            ).fetchall()
    cards = [RoleCard.row_to_card(row) for row in rows]
    favorite_cards = UserFavorite.get_by_user(user["id"]) if is_self else []
    return render_template("user_profile.html", profile_user=dict(user), cards=cards, favorite_cards=favorite_cards, is_self=is_self, tab=tab)


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


@server.route("/assets/<path:filename>")
def asset_file(filename):
    # 清理文件名，防止路径遍历攻击
    # 移除任何 .. 或绝对路径
    safe_parts = []
    for part in filename.replace("\\", "/").split("/"):
        if part == ".." or part.startswith("/") or not part:
            continue
        safe_parts.append(part)
    safe_filename = "/".join(safe_parts)
    if not safe_filename:
        abort(404)

    # 构建目标路径（不使用 resolve() 避免符号链接问题）
    target = BASE_DIR / safe_filename
    target = target.absolute()

    # 确保解析后的路径仍在 BASE_DIR 内
    base_resolved = BASE_DIR.absolute()
    try:
        # 检查目标路径是否以基础路径开头
        if not str(target).startswith(str(base_resolved)):
            abort(404)
    except ValueError:
        abort(404)

    if not target.exists() or not target.is_file():
        abort(404)
    return send_file(target, max_age=3600)


@server.route("/")
def index():
    query = request.args.get("q", "").strip()
    tag = request.args.get("tag", "").strip()
    sort = request.args.get("sort", "latest")

    where = ["visibility = 'public'", "status = 'approved'"]
    params = []
    if query:
        where.append("(name LIKE ? OR description LIKE ? OR creator LIKE ?)")
        term = f"%{query}%"
        params.extend([term, term, term])
    if tag:
        where.append("tags_json LIKE ?")
        params.append(f"%{tag}%")

    order_by = {
        "popular": "downloads DESC, likes DESC, created_at DESC",
        "liked": "likes DESC, created_at DESC",
        "latest": "created_at DESC",
    }.get(sort, "created_at DESC")

    with get_db() as db:
        rows = db.execute(
            f"SELECT * FROM role_cards WHERE {' AND '.join(where)} ORDER BY {order_by}",
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
        q=query,
        tag=tag,
        sort=sort,
        all_tags=all_tags,
    )


@server.route("/card/<identifier>")
def card_detail(identifier):
    user_id = session.get("user_id")

    # 先尝试获取已审核的角色卡
    card = RoleCard.get_by_slug(identifier)

    # 如果找不到，检查是否是所有者或审核员在查看待审核内容
    if not card:
        with get_db() as db:
            if str(identifier).isdigit():
                row = db.execute("SELECT * FROM role_cards WHERE id = ?", (identifier,)).fetchone()
            else:
                row = db.execute("SELECT * FROM role_cards WHERE slug = ?", (identifier,)).fetchone()

            if row:
                # 检查权限：只有所有者、审核员和管理员可以查看待审核内容
                is_owner = user_id and row["user_id"] == user_id
                is_reviewer = user_id and Reviewer.is_reviewer(user_id)
                is_admin = request.args.get("admin") == admin_token()

                if is_owner or is_reviewer or is_admin:
                    card = RoleCard.row_to_card(row)
                else:
                    abort(404)
            else:
                abort(404)

    # 检查权限：私有卡片只有所有者和管理员可以查看
    if card["visibility"] != "public":
        is_owner = user_id and card.get("user_id") == user_id
        is_admin = request.args.get("admin") == admin_token()
        if not is_owner and not is_admin:
            abort(404)

    user_liked = False
    user_favorited = False

    with get_db() as db:
        # 评论查询：普通用户只显示已审核评论，所有者和审核员显示全部
        is_owner = user_id and card.get("user_id") == user_id
        is_reviewer = user_id and Reviewer.is_reviewer(user_id)

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

        # 检查当前用户是否已经喜欢过该角色
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

    return render_template("detail.html", card=card, comments=comments, user_liked=user_liked, user_favorited=user_favorited, current_user_id=user_id)


@server.route("/card/<int:card_id>/comment", methods=["POST"])
@login_required
def post_comment(card_id):
    RoleCard.get_or_404(card_id)
    content = (request.form.get("content") or "").strip()
    if not content:
        flash("评论内容不能为空", "error")
        return redirect(url_for("card_detail", identifier=card_id))
    if len(content) > 1000:
        flash("评论内容不能超过 1000 字", "error")
        return redirect(url_for("card_detail", identifier=card_id))
    now = datetime.now().isoformat(timespec="seconds")
    user_id = session.get("user_id")
    with get_db() as db:
        db.execute(
            "INSERT INTO comments (card_id, user_id, content, created_at) VALUES (?, ?, ?, ?)",
            (card_id, user_id, content, now),
        )
        db.commit()
    flash("评论已提交审核，审核通过后将显示")
    return redirect(url_for("card_detail", identifier=card_id))


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

            params.append(card_id)
            db.execute(
                f"UPDATE role_cards SET {', '.join(sets)} WHERE id = ?",
                params,
            )
            db.commit()
            # 获取更新后的 slug 用于跳转
            new_row = db.execute("SELECT slug FROM role_cards WHERE id = ?", (card_id,)).fetchone()
            new_slug = new_row["slug"] if new_row else card["slug"]

        flash("角色卡已更新")
        return redirect(url_for("card_detail", identifier=new_slug))
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("edit_card", card_id=card_id))


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
        card_file = request.files.get("card_file")
        if card_file and card_file.filename:
            if card_file.filename.lower().endswith(".zip"):
                imported_cards = extract_zip_cards(card_file)
                user_id = session.get("user_id")
                saved_cards = [RoleCard.create(card, avatar_path, user_id=user_id) for card, avatar_path in imported_cards]
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
        if not card.get("creator"):
            current = get_current_user()
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
                    basic_info, example_dialogues, response_format, rules_json, state_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                ),
            )
            db.commit()
        flash("角色卡已提交审核，审核通过后将自动发布")
        return redirect(url_for("user_profile", username=current["username"]))
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("upload"))


@server.route("/api/cards", methods=["POST"])
@limiter.limit("10 per minute")
def api_create_card():
    # 优先通过 API Token 获取用户身份
    user_id = session.get("user_id")
    if not user_id:
        user_id = resolve_api_user()
    # 管理员 Token 返回 0，转为 None 表示无归属用户
    if not user_id or user_id == 0:
        return jsonify({"success": False, "error": "需要登录或提供有效的 API Token"}), 403

    try:
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
        saved = RoleCard.create(card, avatar_path, user_id=user_id)
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

    memory_file = io.BytesIO()
    with zipfile.ZipFile(memory_file, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("character.json", json.dumps(to_export_json(card), ensure_ascii=False, indent=2))
        if card.get("avatar_path"):
            avatar_path = BASE_DIR / card["avatar_path"]
            if avatar_path.exists() and avatar_path.resolve().is_relative_to(BASE_DIR):
                zf.write(avatar_path, f"portrait{avatar_path.suffix}")
    memory_file.seek(0)
    return send_file(
        memory_file,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"{card['slug']}_nekobot.zip",
    )


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
    """检查是否为管理员或审核员"""
    token = request.args.get("token", "") or request.form.get("token", "")
    user_id = session.get("user_id")
    is_admin = token == admin_token()
    is_reviewer_user = user_id and Reviewer.is_reviewer(user_id)
    return is_admin, is_reviewer_user, token


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
                return redirect(url_for("admin", token=token, tab="cards"))
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
            return render_template("admin.html", users=users, token=token, tab=tab, is_admin=is_admin, is_reviewer=is_reviewer_user)
        else:
            rows = db.execute("SELECT * FROM role_cards ORDER BY created_at DESC").fetchall()
            return render_template("admin.html", cards=[RoleCard.row_to_card(row) for row in rows], token=token, tab=tab, is_admin=is_admin, is_reviewer=is_reviewer_user)


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
        return redirect(url_for("admin", token=token))
    try:
        card_ids = [int(x) for x in ids_raw.split(",") if x.strip()]
    except ValueError:
        abort(400)
    if not card_ids:
        flash("请选择至少一张角色卡", "error")
        return redirect(url_for("admin", token=token))
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
    return redirect(url_for("admin", token=token))


@server.route("/admin/user/<int:user_id>/delete", methods=["POST"])
def admin_delete_user(user_id):
    token = request.form.get("token", "")
    if token != admin_token():
        abort(403)
    with get_db() as db:
        # 删除用户（关联的角色卡和评论会通过外键级联删除）
        db.execute("DELETE FROM users WHERE id = ?", (user_id,))
        db.commit()
    flash("用户已删除")
    return redirect(url_for("admin", token=token, tab="users"))


@server.route("/admin/user/batch", methods=["POST"])
def admin_user_batch():
    token = request.form.get("token", "")
    if token != admin_token():
        abort(403)
    action = request.form.get("action", "")
    ids_raw = request.form.get("ids", "")
    if not action or not ids_raw:
        flash("请选择操作和目标用户", "error")
        return redirect(url_for("admin", token=token, tab="users"))
    try:
        user_ids = [int(x) for x in ids_raw.split(",") if x.strip()]
    except ValueError:
        abort(400)
    if not user_ids:
        flash("请选择至少一个用户", "error")
        return redirect(url_for("admin", token=token, tab="users"))
    with get_db() as db:
        placeholders = ",".join("?" for _ in user_ids)
        if action == "delete":
            db.execute(f"DELETE FROM users WHERE id IN ({placeholders})", user_ids)
        else:
            abort(404)
        db.commit()
    flash(f"已批量删除 {len(user_ids)} 个用户")
    return redirect(url_for("admin", token=token, tab="users"))


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
    stats = ReviewQueue.get_stats()

    if tab == "comments":
        items = ReviewQueue.get_pending_comments()
    else:
        items = ReviewQueue.get_pending_cards()

    return render_template("review_queue.html", items=items, tab=tab, stats=stats)


@server.route("/review/card/<int:card_id>/approve", methods=["POST"])
@reviewer_required
def review_approve_card(card_id):
    """批准角色卡"""
    reviewer_id = session.get("user_id")
    result = request.form.get("result", "")
    ReviewQueue.approve_card(card_id, reviewer_id, result)
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
    token = request.args.get("token", "") or request.form.get("token", "")
    if token != admin_token():
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

        return redirect(url_for("admin_reviewers", token=token))

    reviewers = Reviewer.list_all()
    return render_template("admin_reviewers.html", reviewers=reviewers, token=token)


@server.route("/admin/ai-config", methods=["GET", "POST"])
def admin_ai_config():
    """AI审核配置"""
    token = request.args.get("token", "") or request.form.get("token", "")
    if token != admin_token():
        abort(403)

    if request.method == "POST":
        api_key = request.form.get("api_key", "")
        api_url = request.form.get("api_url", "")
        model = request.form.get("model", "")
        enabled = request.form.get("enabled") == "on"

        AIReviewConfig.update(api_key, api_url, model, enabled)
        flash("AI审核配置已更新")
        return redirect(url_for("admin_ai_config", token=token))

    config = AIReviewConfig.get()
    return render_template("admin_ai_config.html", config=config, token=token)


@server.context_processor
def inject_globals():
    from models import Reviewer
    user = get_current_user()
    is_reviewer = False
    if user:
        is_reviewer = Reviewer.is_reviewer(user["id"])
    return {"admin_token": admin_token, "current_user": user, "is_reviewer": is_reviewer}


if __name__ == "__main__":
    init_db()
    port = int(os.getenv("ROLE_CARD_PORT", "7861"))
    debug = os.getenv("ROLE_CARD_DEBUG", "").lower() in {"1", "true", "yes", "on"}
    server.run(host="0.0.0.0", port=port, debug=debug, use_reloader=False, threaded=True)
