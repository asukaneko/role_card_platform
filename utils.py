"""
工具函数 - 包含各种辅助函数
"""
import json
import os
import re
import secrets
import time
import zipfile
import io
from pathlib import Path
from typing import List, Tuple

from flask import abort
from werkzeug.utils import secure_filename

from config import (
    AVATAR_DIR, BASE_DIR, CARD_DIR, MAX_AVATAR_BYTES, MAX_CARD_BYTES,
    MAX_ZIP_BYTES, ALLOWED_AVATAR_EXTENSIONS, IMAGE_SIGNATURES
)


def ensure_dirs() -> None:
    """确保必要的目录存在"""
    from config import DATA_DIR, UPLOAD_DIR, AVATAR_DIR, CARD_DIR
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    CARD_DIR.mkdir(parents=True, exist_ok=True)


def slugify(value: str) -> str:
    """将字符串转换为 URL 友好的 slug"""
    slug = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff]+", "-", value.strip()).strip("-").lower()
    return slug or f"card-{int(time.time())}"


def unique_slug(db, name: str, existing_id: int = None) -> str:
    """生成唯一的 slug"""
    base = slugify(name)
    candidate = base
    suffix = 2
    while True:
        row = db.execute("SELECT id FROM role_cards WHERE slug = ?", (candidate,)).fetchone()
        if not row or (existing_id and row["id"] == existing_id):
            return candidate
        candidate = f"{base}-{suffix}"
        suffix += 1


def normalize_tags(raw_tags) -> List[str]:
    """规范化标签列表"""
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
    """限制文本长度"""
    return str(value or "").strip()[:max_len]


def validate_image_content(content: bytes) -> bool:
    """验证文件内容是否为有效的图片格式（通过文件头魔数）"""
    if len(content) < 8:
        return False
    for signature in IMAGE_SIGNATURES.keys():
        if content.startswith(signature):
            return True
    return False


def save_avatar(file_storage) -> str:
    """保存上传的头像文件"""
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
    """从字节保存头像文件"""
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
    parts = filepath.replace("\\", "/").split("/")
    for part in parts:
        if part == "..":
            return False
    return True


def extract_zip_cards(file_storage) -> List[Tuple[dict, str]]:
    """从 ZIP 文件中提取角色卡"""
    raw = file_storage.read(MAX_ZIP_BYTES + 1)
    if len(raw) > MAX_ZIP_BYTES:
        raise ValueError("ZIP cannot exceed 64MB")

    def _parent_dir(filepath: str) -> str:
        d = str(Path(filepath).parent).replace("\\", "/")
        return "" if d == "." else d

    imported: List[Tuple[dict, str]] = []
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
            from card_utils import normalize_role_card_data
            imported.append((normalize_role_card_data(data), avatar_path))
    return imported


def card_from_json_upload(file_storage) -> dict:
    """从上传的 JSON 文件解析角色卡"""
    raw = file_storage.read(MAX_CARD_BYTES + 1)
    if len(raw) > MAX_CARD_BYTES:
        raise ValueError("角色卡 JSON 不能超过 256KB")
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except Exception as exc:
        raise ValueError("JSON 格式不正确，请上传 UTF-8 编码的角色卡") from exc

    if not isinstance(data, dict):
        raise ValueError("角色卡 JSON 顶层必须是对象")

    from card_utils import normalize_role_card_data
    return normalize_role_card_data(data)


def to_export_json(card: dict) -> dict:
    """导出角色卡数据，使用 NekoBot 格式"""
    from flask import url_for

    # 构建头像 URL
    avatar_url = ""
    if card.get("avatar_path"):
        avatar_url = url_for("asset_file", filename=card["avatar_path"], _external=True)

    # 使用 NekoBot 格式
    exported = {
        "name": card["name"],
        "avatar": "fas fa-user",
        "portrait": avatar_url,
        "description": card.get("description", ""),
        "tags": card.get("tags", []),
        "systemPrompt": card.get("system_prompt", ""),
        "basicInfo": card.get("basic_info", ""),
        "personality": card.get("personality", ""),
        "scenario": card.get("scenario", ""),
        "firstMessage": card.get("first_message", ""),
        "exampleDialogues": card.get("example_dialogues", ""),
        "responseFormat": card.get("response_format", ""),
        "rules": card.get("rules", []),
        "state": card.get("state", {}),
        "creator": card.get("creator", ""),
        "visibility": card.get("visibility", "public"),
        "version": "1.0.0",
    }
    return exported
