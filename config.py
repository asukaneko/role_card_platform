"""
配置文件 - 包含所有配置常量和路径设置
"""
import os
from pathlib import Path

# 基础路径
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = BASE_DIR / "uploads"
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


# 加载环境变量
load_dotenv()


class Config:
    """Flask 配置类"""
    SECRET_KEY = os.getenv("ROLE_CARD_SECRET_KEY", os.urandom(24).hex())
    MAX_CONTENT_LENGTH = MAX_ZIP_BYTES

    # 会话安全设置
    SESSION_COOKIE_SECURE = os.getenv("ROLE_CARD_SECURE_COOKIE", "false").lower() in {"1", "true", "yes", "on"}
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"

    # 速率限制
    RATELIMIT_STORAGE_URI = "memory://"
    RATELIMIT_DEFAULT_LIMITS = ["200 per day", "50 per hour"]
