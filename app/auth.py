"""
认证模块 - 包含用户认证、API Token 验证等
"""
import hashlib
import hmac
import os
import secrets
from functools import wraps
from pathlib import Path

from flask import flash, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from .config import DATA_DIR
from .models import User, get_db


# API Token 加密用的 pepper（环境变量配置）
def get_token_pepper() -> str:
    """获取 API Token 加密用的 pepper"""
    pepper = os.getenv("ROLE_CARD_TOKEN_PEPPER", "").strip()
    if not pepper:
        # 如果没有配置，使用 SECRET_KEY 作为 fallback
        from flask import current_app
        return current_app.config.get("SECRET_KEY", "default-pepper")
    return pepper


def hash_api_token(token: str) -> str:
    """对 API Token 进行 hash"""
    pepper = get_token_pepper()
    return hmac.new(
        pepper.encode(),
        token.encode(),
        hashlib.sha256
    ).hexdigest()


def generate_user_api_token() -> str:
    """生成唯一的用户 API Token，返回原始 token（仅显示一次）"""
    # 生成原始 token
    raw_token = secrets.token_urlsafe(32)
    # 计算 hash
    token_hash = hash_api_token(raw_token)
    
    with get_db() as db:
        # 确保 hash 全局唯一
        while db.execute("SELECT 1 FROM users WHERE api_token_hash = ?", (token_hash,)).fetchone():
            raw_token = secrets.token_urlsafe(32)
            token_hash = hash_api_token(raw_token)
    
    # 返回原始 token（只显示这一次）
    return raw_token


def resolve_api_user() -> int | None:
    """验证 API Token，返回对应的 user_id（None 表示无效）

    接受以下方式的token：
    - Header: X-Role-Card-Token: <token>
    - Header: Authorization: Bearer <token>
    - URL查询参数: ?api_token=<token>
    """
    # 优先从Header获取
    provided = request.headers.get("X-Role-Card-Token", "").strip()

    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        provided = auth.split(" ", 1)[1].strip()

    # 如果Header没有，尝试从URL查询参数获取（nekobot浏览器跳转场景）
    if not provided:
        provided = request.args.get("api_token", "").strip()

    if not provided:
        return None

    # 计算提供的 token 的 hash
    provided_hash = hash_api_token(provided)

    # 优先匹配用户级 Token（使用 hash 比较，防时序攻击）
    with get_db() as db:
        urow = db.execute("SELECT id FROM users WHERE api_token_hash = ?", (provided_hash,)).fetchone()
    if urow:
        return urow["id"]

    # 兼容旧用户：如果 api_token_hash 为空，但 api_token 匹配（明文比较，向后兼容）
    with get_db() as db:
        urow = db.execute("SELECT id FROM users WHERE api_token = ? AND (api_token_hash = '' OR api_token_hash IS NULL)", (provided,)).fetchone()
    if urow:
        return urow["id"]

    # 兼容全局管理员 Token（仍然使用明文比较，因为管理员token是配置在环境变量中的）
    configured = os.getenv("ROLE_CARD_API_TOKEN", "").strip()
    if configured and secrets.compare_digest(provided, configured):
        return 0  # 特殊值：管理员 Token，无具体用户

    return None


def api_token_valid() -> bool:
    """验证 API Token 是否有效
    
    只接受Header中的token：
    - X-Role-Card-Token: <token>
    - Authorization: Bearer <token>
    """
    configured = os.getenv("ROLE_CARD_API_TOKEN", "").strip()
    
    # 只从Header获取token
    provided = request.headers.get("X-Role-Card-Token", "").strip()
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        provided = auth.split(" ", 1)[1].strip()
    
    required = configured or os.getenv("ROLE_CARD_REQUIRE_TOKEN", "").lower() in {"1", "true", "yes", "on"}
    if not required:
        return not bool(provided)
    return bool(configured and secrets.compare_digest(provided, configured))


def admin_token() -> str:
    """获取管理员密码（从环境变量或文件）"""
    # 优先从环境变量获取
    token = os.getenv("ROLE_CARD_ADMIN_TOKEN", "").strip()
    if token:
        return token
    # 从文件读取
    token_path = DATA_DIR / "admin_token.txt"
    if token_path.exists():
        return token_path.read_text(encoding="utf-8").strip()
    # 生成新密码并保存
    from .config import ensure_dirs
    ensure_dirs()
    generated = secrets.token_urlsafe(18)
    token_path.write_text(generated, encoding="utf-8")
    return generated


def get_or_create_admin_user() -> dict:
    """获取或创建 admin 用户"""
    from .models import get_db
    
    admin_user = User.get_by_username("admin")
    if not admin_user:
        # 创建 admin 用户
        password = admin_token()
        password_hash = generate_password_hash(password)
        api_token = generate_user_api_token()
        admin_user = User.create(
            username="admin",
            password_hash=password_hash,
            display_name="管理员",
            api_token=api_token
        )
        # 设置 is_admin = 1
        with get_db() as db:
            db.execute("UPDATE users SET is_admin = 1 WHERE username = 'admin'")
            db.commit()
    else:
        # 确保 is_admin = 1
        with get_db() as db:
            db.execute("UPDATE users SET is_admin = 1 WHERE username = 'admin'")
            db.commit()
    
    return admin_user


def is_admin_user(user_id: int) -> bool:
    """检查用户是否为 admin 账号（使用 is_admin 字段）"""
    if not user_id:
        return False
    user = User.get_by_id(user_id)
    return user and user.get("is_admin", 0) == 1


def get_current_user():
    """从 session 中获取当前登录用户信息，未登录返回 None"""
    user_id = session.get("user_id")
    if not user_id:
        return None
    user = User.get_by_id(user_id)
    if not user:
        session.pop("user_id", None)
        return None
    return user


def login_required(f):
    """要求登录才能访问的装饰器"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            flash("请先登录后再执行此操作", "error")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def owner_required(card_id: int) -> dict:
    """验证当前用户是卡片所有者，返回卡片数据（包含草稿等所有状态），否则 403"""
    from .models import RoleCard, get_db
    with get_db() as db:
        row = db.execute("SELECT * FROM role_cards WHERE id = ?", (card_id,)).fetchone()
    if not row:
        from flask import abort
        abort(404)
    card = RoleCard.row_to_card(row)
    user_id = session.get("user_id")
    if not user_id or card.get("user_id") != user_id:
        from flask import abort
        abort(403)
    return card


class AuthService:
    """认证服务类"""

    @staticmethod
    def register(username: str, password: str, display_name: str = "") -> tuple:
        """用户注册
        
        Returns:
            (user_dict, error_message)
        """
        # 验证用户名
        if len(username) < 3 or len(username) > 24:
            return None, "用户名长度需在 3-24 个字符之间"

        import re
        if not re.match(r"^[a-zA-Z0-9_\u4e00-\u9fff]+$", username):
            return None, "用户名只能包含字母、数字、下划线和中文"

        # 验证密码
        if len(password) < 6:
            return None, "密码长度至少 6 个字符"

        # 检查用户名是否已存在
        if User.get_by_username(username):
            return None, "该用户名已被注册"

        # 创建用户
        password_hash = generate_password_hash(password)
        api_token = generate_user_api_token()
        user = User.create(username, password_hash, display_name or username, api_token)

        return user, None

    @staticmethod
    def login(username: str, password: str) -> tuple:
        """用户登录
        
        Returns:
            (user_dict, error_message)
        """
        user = User.get_by_username(username)

        if not user or not check_password_hash(user["password_hash"], password):
            return None, "用户名或密码错误"

        # 老用户兼容：登录时自动补全 API Token
        if not user["api_token"]:
            new_token = generate_user_api_token()
            User.update_api_token(user["id"], new_token)
            user["api_token"] = new_token

        return user, None

    @staticmethod
    def logout() -> None:
        """用户退出"""
        session.pop("user_id", None)

    @staticmethod
    def regenerate_api_token(user_id: int) -> str:
        """重新生成用户 API Token"""
        new_token = generate_user_api_token()
        User.update_api_token(user_id, new_token)
        return new_token
