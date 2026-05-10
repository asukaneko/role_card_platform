"""
角色卡工具函数 - 处理角色卡数据的解析和验证
"""
from utils import limit_text, normalize_tags


def normalize_role_card_data(data: dict, visibility: str = "public") -> dict:
    """标准化角色卡数据，支持多种格式（平台格式、通用格式、NekoBot格式）"""
    raw = dict(data or {})

    # 检测是否为 NekoBot 格式（同时检查驼峰命名和下划线命名）
    is_nekobot = bool(
        raw.get("basicInfo") or raw.get("basic_info") or
        raw.get("firstMessage") or raw.get("first_message") or
        raw.get("systemPrompt") or raw.get("system_prompt") or
        raw.get("exampleDialogues") or raw.get("example_dialogues") or
        raw.get("responseFormat") or raw.get("response_format")
    )
    source_format = "nekobot" if is_nekobot else (raw.get("source_format") or raw.get("source") or "platform")

    # 基础字段映射（同时支持驼峰命名和下划线命名）
    description = raw.get("description") or raw.get("summary") or raw.get("basicInfo") or raw.get("basic_info", "")
    first_message = raw.get("first_message") or raw.get("first_mes") or raw.get("firstMessage", "")
    system_prompt = raw.get("system_prompt") or raw.get("prompt") or raw.get("systemPrompt", "")

    # NekoBot 特有字段（同时支持驼峰命名和下划线命名）
    basic_info = raw.get("basicInfo") or raw.get("basic_info", "")
    example_dialogues = raw.get("exampleDialogues") or raw.get("example_dialogues", "")
    response_format = raw.get("responseFormat") or raw.get("response_format", "")
    rules = raw.get("rules", [])
    state = raw.get("state", {})

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
        # NekoBot 扩展字段
        "basic_info": limit_text(basic_info, 1000),
        "example_dialogues": limit_text(example_dialogues, 5000),
        "response_format": limit_text(response_format, 500),
        "rules": rules if isinstance(rules, list) else [],
        "state": state if isinstance(state, dict) else {},
        "raw_json": raw,
    }


def card_from_form(form) -> dict:
    """从表单数据创建角色卡字典"""
    card = {
        "name": limit_text(form.get("name"), 80),
        "description": limit_text(form.get("description"), 500),
        "personality": limit_text(form.get("personality"), 3000),
        "scenario": limit_text(form.get("scenario"), 3000),
        "first_message": limit_text(form.get("first_message"), 1200),
        "system_prompt": limit_text(form.get("system_prompt"), 6000),
        "tags": normalize_tags(form.get("tags")),
        "creator": limit_text(form.get("creator"), 80),
        "visibility": "public" if form.get("visibility") == "public" else "private",
        # NekoBot 扩展字段
        "basic_info": limit_text(form.get("basic_info"), 1000),
        "example_dialogues": limit_text(form.get("example_dialogues"), 5000),
        "response_format": limit_text(form.get("response_format"), 500),
        "rules": normalize_tags(form.get("rules")),
        "state": {},
    }
    return card


def validate_card(card: dict) -> None:
    """验证角色卡数据"""
    if not card.get("name"):
        raise ValueError("请填写角色名")
    if not card.get("description") and not card.get("personality"):
        raise ValueError("请至少填写简介或性格设定")
