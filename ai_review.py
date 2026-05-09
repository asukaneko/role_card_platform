"""
AI 审核模块 - 使用 AI 进行内容审核
"""
import json
import re
from typing import Optional

from models import AIReviewConfig


class AIReviewer:
    """AI 审核器"""

    # 审核提示词模板
    CARD_REVIEW_PROMPT = """你是一位内容审核专家。请审核以下角色卡内容，判断其是否含有色情、暴力、反动、违法或违背社会主义核心价值观的内容。

角色卡信息：
名称：{name}
描述：{description}
性格设定：{personality}
场景设定：{scenario}
系统提示词：{system_prompt}
第一条消息：{first_message}
标签：{tags}

请严格按照以下格式返回审核结果：
RESULT: [PASS/REJECT]
REASON: [审核意见说明]

PASS 表示内容通过审核，REJECT 表示内容违规需要拒绝。
如果拒绝，请说明具体违规原因。"""

    COMMENT_REVIEW_PROMPT = """你是一位内容审核专家。请审核以下评论内容，判断其是否含有色情、暴力、反动、违法、广告 spam 或违背社会主义核心价值观的内容。

评论内容：
{content}

请严格按照以下格式返回审核结果：
RESULT: [PASS/REJECT]
REASON: [审核意见说明]

PASS 表示内容通过审核，REJECT 表示内容违规需要拒绝。
如果拒绝，请说明具体违规原因。"""

    @staticmethod
    def is_enabled() -> bool:
        """检查 AI 审核是否已启用"""
        config = AIReviewConfig.get()
        return bool(config.get("enabled")) and bool(config.get("api_key"))

    @staticmethod
    def review_card(card_data: dict) -> tuple:
        """
        审核角色卡
        
        Returns:
            (is_approved: bool, result_message: str)
        """
        config = AIReviewConfig.get()
        if not AIReviewer.is_enabled():
            return True, "AI审核未启用，自动通过"

        try:
            prompt = AIReviewer.CARD_REVIEW_PROMPT.format(
                name=card_data.get("name", ""),
                description=card_data.get("description", ""),
                personality=card_data.get("personality", ""),
                scenario=card_data.get("scenario", ""),
                system_prompt=card_data.get("system_prompt", ""),
                first_message=card_data.get("first_message", ""),
                tags=", ".join(card_data.get("tags", [])),
            )

            response = AIReviewer._call_api(prompt, config)
            return AIReviewer._parse_response(response)

        except Exception as e:
            return True, f"AI审核出错: {str(e)}，自动通过"

    @staticmethod
    def review_comment(content: str) -> tuple:
        """
        审核评论
        
        Returns:
            (is_approved: bool, result_message: str)
        """
        config = AIReviewConfig.get()
        if not AIReviewer.is_enabled():
            return True, "AI审核未启用，自动通过"

        try:
            prompt = AIReviewer.COMMENT_REVIEW_PROMPT.format(content=content)
            response = AIReviewer._call_api(prompt, config)
            return AIReviewer._parse_response(response)

        except Exception as e:
            return True, f"AI审核出错: {str(e)}，自动通过"

    @staticmethod
    def _call_api(prompt: str, config: dict) -> str:
        """调用 AI API"""
        import urllib.request
        import urllib.error

        api_url = config.get("api_url", "")
        api_key = config.get("api_key", "")
        model = config.get("model", "")

        if not api_url or not api_key:
            raise ValueError("API配置不完整")

        # 支持 OpenAI 格式的 API
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": "你是一位专业的内容审核专家，负责审核用户提交的内容是否符合社区规范。"},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.3,
            "max_tokens": 500,
        }

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(api_url, data=data, headers=headers, method="POST")

        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
            return result["choices"][0]["message"]["content"]

    @staticmethod
    def _parse_response(response: str) -> tuple:
        """解析 AI 审核响应"""
        response = response.strip()

        # 提取 RESULT
        result_match = re.search(r'RESULT:\s*(\w+)', response, re.IGNORECASE)
        if not result_match:
            # 如果没有明确的 RESULT，尝试从内容中判断
            if "通过" in response or "PASS" in response.upper():
                return True, response
            elif "拒绝" in response or "REJECT" in response.upper() or "违规" in response:
                return False, response
            return True, f"无法解析审核结果，默认通过。原始响应: {response[:200]}"

        result = result_match.group(1).upper()

        # 提取 REASON
        reason_match = re.search(r'REASON:\s*(.+?)(?=\n|$)', response, re.IGNORECASE | re.DOTALL)
        reason = reason_match.group(1).strip() if reason_match else response

        if result == "PASS":
            return True, reason
        elif result == "REJECT":
            return False, reason
        else:
            return True, f"未知的审核结果: {result}，默认通过。原始响应: {response[:200]}"
