import json
import io
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


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = BASE_DIR / "uploads"
AVATAR_DIR = UPLOAD_DIR / "avatars"
CARD_DIR = UPLOAD_DIR / "cards"
DB_PATH = DATA_DIR / "role_cards.db"

MAX_CARD_BYTES = 256 * 1024
MAX_AVATAR_BYTES = 40 * 1024 * 1024
MAX_ZIP_BYTES = 64 * 1024 * 1024
ALLOWED_AVATAR_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def load_dotenv() -> None:
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_dotenv()

server = Flask(__name__)
server.config["SECRET_KEY"] = os.getenv("ROLE_CARD_SECRET_KEY", secrets.token_hex(24))
server.config["MAX_CONTENT_LENGTH"] = MAX_ZIP_BYTES

# 配置安全会话设置
server.config["SESSION_COOKIE_SECURE"] = os.getenv("ROLE_CARD_SECURE_COOKIE", "false").lower() in {"1", "true", "yes", "on"}
server.config["SESSION_COOKIE_HTTPONLY"] = True
server.config["SESSION_COOKIE_SAMESITE"] = "Lax"

# 初始化速率限制器
limiter = Limiter(
    get_remote_address,
    app=server,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://",
)


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    CARD_DIR.mkdir(parents=True, exist_ok=True)


def get_db():
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_db() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                display_name TEXT DEFAULT '',
                bio TEXT DEFAULT '',
                api_token TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS role_cards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                slug TEXT NOT NULL UNIQUE,
                avatar_path TEXT DEFAULT '',
                description TEXT DEFAULT '',
                personality TEXT DEFAULT '',
                scenario TEXT DEFAULT '',
                first_message TEXT DEFAULT '',
                system_prompt TEXT DEFAULT '',
                tags_json TEXT DEFAULT '[]',
                creator TEXT DEFAULT '',
                visibility TEXT DEFAULT 'public',
                downloads INTEGER DEFAULT 0,
                likes INTEGER DEFAULT 0,
                source_format TEXT DEFAULT 'platform',
                raw_json TEXT DEFAULT '{}',
                user_id INTEGER DEFAULT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
            """
        )
        columns = {row["name"] for row in db.execute("PRAGMA table_info(role_cards)").fetchall()}
        if "source_format" not in columns:
            db.execute("ALTER TABLE role_cards ADD COLUMN source_format TEXT DEFAULT 'platform'")
        if "raw_json" not in columns:
            db.execute("ALTER TABLE role_cards ADD COLUMN raw_json TEXT DEFAULT '{}'")
        if "user_id" not in columns:
            db.execute("ALTER TABLE role_cards ADD COLUMN user_id INTEGER DEFAULT NULL")
        # 用户表迁移：补充 api_token 字段
        user_columns = {row["name"] for row in db.execute("PRAGMA table_info(users)").fetchall()}
        if "api_token" not in user_columns:
            db.execute("ALTER TABLE users ADD COLUMN api_token TEXT NOT NULL DEFAULT ''")
        # 用户表迁移：补充 avatar_path 字段
        if "avatar_path" not in user_columns:
            db.execute("ALTER TABLE users ADD COLUMN avatar_path TEXT DEFAULT ''")
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                card_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (card_id) REFERENCES role_cards(id) ON DELETE CASCADE,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
            """
        )
        # 用户喜欢记录表
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS user_likes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                card_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (card_id) REFERENCES role_cards(id) ON DELETE CASCADE,
                UNIQUE(user_id, card_id)
            )
            """
        )
        db.commit()


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff]+", "-", value.strip()).strip("-").lower()
    return slug or f"card-{int(time.time())}"


def unique_slug(db, name: str, existing_id: int | None = None) -> str:
    base = slugify(name)
    candidate = base
    suffix = 2
    while True:
        row = db.execute("SELECT id FROM role_cards WHERE slug = ?", (candidate,)).fetchone()
        if not row or (existing_id and row["id"] == existing_id):
            return candidate
        candidate = f"{base}-{suffix}"
        suffix += 1


def normalize_tags(raw_tags) -> list[str]:
    if isinstance(raw_tags, list):
        tags = raw_tags
    else:
        tags = re.split(r"[,，#\s]+", str(raw_tags or ""))
    cleaned = []
    for tag in tags:
        text = str(tag).strip()
        if text and text not in cleaned:
            cleaned.append(text[:24])
    return cleaned[:12]


def limit_text(value, max_len: int) -> str:
    return str(value or "").strip()[:max_len]


def card_from_form(form) -> dict:
    return {
        "name": limit_text(form.get("name"), 80),
        "description": limit_text(form.get("description"), 500),
        "personality": limit_text(form.get("personality"), 3000),
        "scenario": limit_text(form.get("scenario"), 3000),
        "first_message": limit_text(form.get("first_message"), 1200),
        "system_prompt": limit_text(form.get("system_prompt"), 6000),
        "tags": normalize_tags(form.get("tags")),
        "creator": limit_text(form.get("creator"), 80),
        "visibility": "public" if form.get("visibility") == "public" else "private",
    }


def card_from_json_upload(file_storage) -> dict:
    raw = file_storage.read(MAX_CARD_BYTES + 1)
    if len(raw) > MAX_CARD_BYTES:
        raise ValueError("角色卡 JSON 不能超过 256KB")
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except Exception as exc:
        raise ValueError("JSON 格式不正确，请上传 UTF-8 编码的角色卡") from exc

    if not isinstance(data, dict):
        raise ValueError("角色卡 JSON 顶层必须是对象")

    return normalize_role_card_data(data)


def normalize_role_card_data(data: dict, visibility: str = "public") -> dict:
    """Accept platform cards, common role-card JSON, and NekoBot character cards."""
    raw = dict(data or {})
    description = raw.get("description") or raw.get("summary") or raw.get("basicInfo")
    first_message = raw.get("first_message") or raw.get("first_mes") or raw.get("firstMessage")
    system_prompt = raw.get("system_prompt") or raw.get("prompt") or raw.get("systemPrompt")
    source_format = raw.get("source_format") or raw.get("source") or "platform"
    if raw.get("basicInfo") or raw.get("firstMessage") or raw.get("systemPrompt"):
        source_format = "nekobot"

    return {
        "name": limit_text(raw.get("name") or raw.get("char_name"), 80),
        "description": limit_text(description, 500),
        "personality": limit_text(raw.get("personality"), 3000),
        "scenario": limit_text(raw.get("scenario") or raw.get("world"), 3000),
        "first_message": limit_text(first_message, 1200),
        "system_prompt": limit_text(system_prompt, 6000),
        "tags": normalize_tags(raw.get("tags")),
        "creator": limit_text(raw.get("creator") or raw.get("author"), 80),
        "visibility": "public" if visibility == "public" else "private",
        "source_format": limit_text(source_format, 40) or "platform",
        "raw_json": raw,
    }


def validate_card(card: dict) -> None:
    if not card.get("name"):
        raise ValueError("请填写角色名")
    if not card.get("description") and not card.get("personality"):
        raise ValueError("请至少填写简介或性格设定")


def validate_image_content(content: bytes) -> bool:
    """验证文件内容是否为有效的图片格式（通过文件头魔数）"""
    if len(content) < 8:
        return False

    # 图片文件头魔数
    image_signatures = {
        b'\x89PNG\r\n\x1a\n': '.png',
        b'\xff\xd8\xff': '.jpg',  # JPEG/JPG
        b'RIFF': '.webp',  # WebP 以 RIFF 开头
        b'GIF87a': '.gif',
        b'GIF89a': '.gif',
    }

    for signature, ext in image_signatures.items():
        if content.startswith(signature):
            return True
    return False


def save_avatar(file_storage) -> str:
    if not file_storage or not file_storage.filename:
        return ""

    ext = Path(file_storage.filename).suffix.lower()
    if ext not in ALLOWED_AVATAR_EXTENSIONS:
        raise ValueError("头像仅支持 png、jpg、jpeg、webp、gif")

    file_storage.stream.seek(0, os.SEEK_END)
    size = file_storage.stream.tell()
    file_storage.stream.seek(0)
    if size > MAX_AVATAR_BYTES:
        raise ValueError("头像不能超过 40MB")

    # 读取内容验证图片格式
    content = file_storage.read()
    file_storage.stream.seek(0)
    if not validate_image_content(content):
        raise ValueError("上传的文件不是有效的图片格式")

    safe_name = secure_filename(file_storage.filename) or f"avatar{ext}"
    filename = f"{int(time.time())}_{secrets.token_hex(6)}_{safe_name}"
    path = AVATAR_DIR / filename
    file_storage.save(path)
    return f"uploads/avatars/{filename}"


def save_avatar_bytes(filename: str, content: bytes) -> str:
    if not content:
        return ""
    ensure_dirs()
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_AVATAR_EXTENSIONS:
        return ""
    if len(content) > MAX_AVATAR_BYTES:
        raise ValueError("Avatar cannot exceed 40MB")
    # 验证图片内容
    if not validate_image_content(content):
        return ""
    safe_name = secure_filename(Path(filename).name) or f"avatar{ext}"
    stored = f"{int(time.time())}_{secrets.token_hex(6)}_{safe_name}"
    path = AVATAR_DIR / stored
    path.write_bytes(content)
    return f"uploads/avatars/{stored}"


def _is_safe_zip_path(filepath: str) -> bool:
    """检查 ZIP 中的路径是否安全（防止 Zip Slip 攻击）"""
    # 规范化路径并检查是否包含路径遍历
    parts = filepath.replace("\\", "/").split("/")
    for part in parts:
        if part == "..":
            return False
    return True


def extract_zip_cards(file_storage) -> list[tuple[dict, str]]:
    raw = file_storage.read(MAX_ZIP_BYTES + 1)
    if len(raw) > MAX_ZIP_BYTES:
        raise ValueError("ZIP cannot exceed 64MB")

    def _parent_dir(filepath: str) -> str:
        d = str(Path(filepath).parent).replace("\\", "/")
        return "" if d == "." else d

    imported: list[tuple[dict, str]] = []
    with zipfile.ZipFile(io.BytesIO(raw), "r") as zf:
        # 过滤掉不安全的文件名（防止 Zip Slip）
        all_names = zf.namelist()
        safe_names = [name for name in all_names if not name.endswith("/") and _is_safe_zip_path(name)]

        json_names = [name for name in safe_names if name.lower().endswith("character.json")]
        if not json_names:
            raise ValueError("ZIP must contain character.json")

        for json_name in json_names:
            if zf.getinfo(json_name).file_size > MAX_CARD_BYTES:
                raise ValueError(f"{json_name} is larger than 256KB")
            data = json.loads(zf.read(json_name).decode("utf-8-sig"))
            if not isinstance(data, dict):
                raise ValueError(f"{json_name} is not a JSON object")

            folder = _parent_dir(json_name)
            avatar_path = ""
            for name in safe_names:
                if _parent_dir(name) == folder and Path(name).stem.lower() == "portrait":
                    # 使用安全的文件名保存头像
                    safe_name = Path(name).name
                    avatar_path = save_avatar_bytes(safe_name, zf.read(name))
                    break
            imported.append((normalize_role_card_data(data), avatar_path))
    return imported


def insert_card(card: dict, avatar_path: str = "", user_id: int | None = None) -> dict:
    validate_card(card)
    # 登录用户上传时，自动填充作者为用户名
    if not card.get("creator") and user_id:
        with get_db() as db:
            user_row = db.execute("SELECT username, display_name FROM users WHERE id = ?", (user_id,)).fetchone()
        if user_row:
            card["creator"] = user_row["display_name"] or user_row["username"]
    now = datetime.now().isoformat(timespec="seconds")
    with get_db() as db:
        slug = unique_slug(db, card["name"])
        db.execute(
            """
            INSERT INTO role_cards (
                name, slug, avatar_path, description, personality, scenario,
                first_message, system_prompt, tags_json, creator, visibility,
                source_format, raw_json, user_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                card.get("source_format", "platform"),
                json.dumps(card.get("raw_json") or {}, ensure_ascii=False),
                user_id,
                now,
                now,
            ),
        )
        db.commit()
        row = db.execute("SELECT * FROM role_cards WHERE slug = ?", (slug,)).fetchone()
    return row_to_card(row)


def row_to_card(row) -> dict:
    card = dict(row)
    try:
        card["tags"] = json.loads(card.pop("tags_json") or "[]")
    except Exception:
        card["tags"] = []
    # 查询角色卡所属用户的用户名
    user_id = card.get("user_id")
    if user_id:
        with get_db() as db:
            user_row = db.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
        if user_row:
            card["owner_username"] = user_row["username"]
    return card


def fetch_card_or_404(identifier):
    with get_db() as db:
        if str(identifier).isdigit():
            row = db.execute("SELECT * FROM role_cards WHERE id = ?", (identifier,)).fetchone()
        else:
            row = db.execute("SELECT * FROM role_cards WHERE slug = ?", (identifier,)).fetchone()
    if not row:
        abort(404)
    return row_to_card(row)


def to_export_json(card: dict) -> dict:
    """导出角色卡数据，只返回安全的字段"""
    exported = {
        "name": card["name"],
        "avatar": url_for("asset_file", filename=card["avatar_path"], _external=True)
        if card.get("avatar_path")
        else "",
        "description": card.get("description", ""),
        "personality": card.get("personality", ""),
        "scenario": card.get("scenario", ""),
        "first_message": card.get("first_message", ""),
        "system_prompt": card.get("system_prompt", ""),
        "tags": card.get("tags", []),
        "creator": card.get("creator", ""),
        "visibility": card.get("visibility", "public"),
        "version": "1.0.0",
    }
    return exported


def generate_user_api_token() -> str:
    """生成唯一的用户 API Token"""
    token = secrets.token_urlsafe(32)
    with get_db() as db:
        # 确保全局唯一
        while db.execute("SELECT 1 FROM users WHERE api_token = ?", (token,)).fetchone():
            token = secrets.token_urlsafe(32)
    return token


def resolve_api_user() -> int | None:
    """验证 API Token，返回对应的 user_id（None 表示无效）"""
    provided = (
        request.headers.get("X-Role-Card-Token", "")
        or request.args.get("token", "")
        or request.form.get("token", "")
    ).strip()
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        provided = auth.split(" ", 1)[1].strip()
    if not provided:
        return None
    # 优先匹配用户级 Token（精确匹配，防时序攻击）
    with get_db() as db:
        urow = db.execute("SELECT id FROM users WHERE api_token = ?", (provided,)).fetchone()
    if urow:
        return urow["id"]
    # 兼容全局管理员 Token
    configured = os.getenv("ROLE_CARD_API_TOKEN", "").strip()
    if configured and secrets.compare_digest(provided, configured):
        return 0  # 特殊值：管理员 Token，无具体用户
    return None


def api_token_valid() -> bool:
    configured = os.getenv("ROLE_CARD_API_TOKEN", "").strip()
    provided = (
        request.headers.get("X-Role-Card-Token", "")
        or request.args.get("token", "")
        or request.form.get("token", "")
    ).strip()
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        provided = auth.split(" ", 1)[1].strip()
    required = configured or os.getenv("ROLE_CARD_REQUIRE_TOKEN", "").lower() in {"1", "true", "yes", "on"}
    if not required:
        return not bool(provided)
    return bool(configured and secrets.compare_digest(provided, configured))


def admin_token() -> str:
    token = os.getenv("ROLE_CARD_ADMIN_TOKEN", "").strip()
    if token:
        return token
    token_path = DATA_DIR / "admin_token.txt"
    if token_path.exists():
        return token_path.read_text(encoding="utf-8").strip()
    ensure_dirs()
    generated = secrets.token_urlsafe(18)
    token_path.write_text(generated, encoding="utf-8")
    return generated


def get_current_user():
    """从 session 中获取当前登录用户信息，未登录返回 None"""
    user_id = session.get("user_id")
    if not user_id:
        return None
    with get_db() as db:
        row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        session.pop("user_id", None)
        return None
    return dict(row)


def login_required(f):
    """要求登录才能访问的装饰器"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            flash("请先登录后再执行此操作", "error")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


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
    with get_db() as db:
        user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if not user:
            abort(404)
        # 自己的主页显示所有角色卡，别人的主页只显示公开的
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
    cards = [row_to_card(row) for row in rows]
    return render_template("user_profile.html", profile_user=dict(user), cards=cards, is_self=is_self)


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

    target = (BASE_DIR / safe_filename).resolve()
    # 确保解析后的路径仍在 BASE_DIR 内
    try:
        target.relative_to(BASE_DIR.resolve())
    except ValueError:
        abort(404)

    if not target.exists() or not target.is_file():
        abort(404)
    return send_file(target, max_age=0)


@server.route("/")
def index():
    query = request.args.get("q", "").strip()
    tag = request.args.get("tag", "").strip()
    sort = request.args.get("sort", "latest")

    where = ["visibility = 'public'"]
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
            "SELECT tags_json FROM role_cards WHERE visibility = 'public'"
        ).fetchall()

    cards = [row_to_card(row) for row in rows]
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
    card = fetch_card_or_404(identifier)
    if card["visibility"] != "public" and request.args.get("admin") != admin_token():
        abort(404)
    
    user_id = session.get("user_id")
    user_liked = False
    
    with get_db() as db:
        rows = db.execute(
            "SELECT c.*, u.username, u.display_name FROM comments c "
            "JOIN users u ON c.user_id = u.id "
            "WHERE c.card_id = ? ORDER BY c.created_at ASC",
            (card["id"],),
        ).fetchall()
        
        # 检查当前用户是否已经喜欢过该角色
        if user_id:
            like_row = db.execute(
                "SELECT id FROM user_likes WHERE user_id = ? AND card_id = ?",
                (user_id, card["id"])
            ).fetchone()
            user_liked = like_row is not None
    
    comments = []
    for row in rows:
        item = dict(row)
        item["can_delete"] = user_id == row["user_id"]
        comments.append(item)
    
    return render_template("detail.html", card=card, comments=comments, user_liked=user_liked, current_user_id=user_id)


@server.route("/card/<int:card_id>/comment", methods=["POST"])
@login_required
def post_comment(card_id):
    fetch_card_or_404(card_id)
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
    flash("评论已发布")
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
    card = row_to_card(row)
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
                saved_cards = [insert_card(card, avatar_path, user_id=user_id) for card, avatar_path in imported_cards]
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
                    user_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                ),
            )
            db.commit()
        flash("角色卡已发布")
        return redirect(url_for("card_detail", identifier=slug))
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
        saved = insert_card(card, avatar_path, user_id=user_id)
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
    card = fetch_card_or_404(card_id)
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
    card = fetch_card_or_404(card_id)
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


@server.route("/admin")
def admin():
    token = request.args.get("token", "")
    if token != admin_token():
        return render_template("admin_login.html")
    tab = request.args.get("tab", "cards")
    with get_db() as db:
        if tab == "users":
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
            return render_template("admin.html", users=users, token=token, tab=tab)
        else:
            rows = db.execute("SELECT * FROM role_cards ORDER BY created_at DESC").fetchall()
            return render_template("admin.html", cards=[row_to_card(row) for row in rows], token=token, tab=tab)


@server.route("/admin/card/<int:card_id>/<action>", methods=["POST"])
def admin_action(card_id, action):
    token = request.form.get("token", "")
    if token != admin_token():
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
    return redirect(url_for("admin", token=token))


@server.route("/admin/batch", methods=["POST"])
def admin_batch():
    token = request.form.get("token", "")
    if token != admin_token():
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


@server.context_processor
def inject_globals():
    return {"admin_token": admin_token, "current_user": get_current_user()}


if __name__ == "__main__":
    init_db()
    port = int(os.getenv("ROLE_CARD_PORT", "7861"))
    debug = os.getenv("ROLE_CARD_DEBUG", "").lower() in {"1", "true", "yes", "on"}
    server.run(host="0.0.0.0", port=port, debug=debug, use_reloader=False)
