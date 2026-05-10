"""
配置文件 - 包含所有配置常量和路径设置
"""
import os
from pathlib import Path

# 基础路径
BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent
DATA_DIR = PROJECT_ROOT / "data"
UPLOAD_DIR = PROJECT_ROOT / "uploads"
AVATAR_DIR = UPLOAD_DIR / "avatars"
CARD_DIR = UPLOAD_DIR / "cards"
DB_PATH = DATA_DIR / "role_cards.db"

# 文件大小限制
MAX_CARD_BYTES = 256 * 1024  # 256KB
MAX_AVATAR_BYTES = 40 * 1024 * 1024  # 40MB
MAX_ZIP_BYTES = 64 * 1024 * 1024  # 64MB

# 允许的文件扩展名
ALLOWED_AVATAR_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

# 图片文件头魔数（用于验证文件类型）
IMAGE_SIGNATURES = {
    b'\x89PNG\r\n\x1a\n': '.png',
    b'\xff\xd8\xff': '.jpg',
    b'RIFF': '.webp',
    b'GIF87a': '.gif',
    b'GIF89a': '.gif',
}


def load_dotenv() -> None:
    """加载 .env 文件中的环境变量"""
    env_path = PROJECT_ROOT / ".env"
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


# 加载环境变量
load_dotenv()


class Config:
    """Flask 配置类"""
    # SECRET_KEY：生产环境必须从环境变量获取
    SECRET_KEY = os.getenv("ROLE_CARD_SECRET_KEY")
    if not SECRET_KEY:
        import secrets
        SECRET_KEY = secrets.token_hex(32)
        import warnings
        warnings.warn(
            "警告: ROLE_CARD_SECRET_KEY 未设置，使用临时生成的密钥。"
            "生产环境请务必设置 ROLE_CARD_SECRET_KEY 环境变量！",
            RuntimeWarning
        )
    
    MAX_CONTENT_LENGTH = MAX_ZIP_BYTES

    # 会话安全设置
    # 生产环境应该启用Secure Cookie（需要HTTPS）
    SESSION_COOKIE_SECURE = os.getenv("ROLE_CARD_SECURE_COOKIE", "false").lower() in {"1", "true", "yes", "on"}
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"

    # CSRF保护设置
    WTF_CSRF_ENABLED = True
    WTF_CSRF_TIME_LIMIT = 3600  # CSRF token有效期1小时
    WTF_CSRF_SSL_STRICT = os.getenv("ROLE_CARD_CSRF_SSL_STRICT", "false").lower() in {"1", "true", "yes", "on"}

    # 速率限制设置
    # 生产环境建议使用Redis存储
    RATELIMIT_STORAGE_URI = os.getenv("ROLE_CARD_RATELIMIT_STORAGE", "memory://")
    RATELIMIT_DEFAULT_LIMITS = ["200 per day", "50 per hour"]
    
    # 安全响应头
    @staticmethod
    def init_app(app):
        """初始化应用安全设置"""
        # 添加安全响应头
        @app.after_request
        def add_security_headers(response):
            # 防止MIME类型嗅探
            response.headers['X-Content-Type-Options'] = 'nosniff'
            # 防止点击劫持
            response.headers['X-Frame-Options'] = 'SAMEORIGIN'
            # XSS保护
            response.headers['X-XSS-Protection'] = '1; mode=block'
            # Referrer策略
            response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
            # 内容安全策略（CSP）- 基础配置
            response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self'; connect-src 'self'; media-src 'self'; object-src 'none'; frame-ancestors 'self';"
            return response
