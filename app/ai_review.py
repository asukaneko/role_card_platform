"""
AI 审核模块 - 使用 AI 进行内容审核
"""
import ipaddress
import json
import re
import urllib.parse
from typing import Optional

from .models import AIReviewConfig


# 允许的API域名白名单
ALLOWED_API_HOSTS = {
    "api.openai.com",
    "api.groq.com",
    "api.anthropic.com",
    "api.cohere.com",
    "api.mistral.ai",
    "generativelanguage.googleapis.com",
    "api.deepseek.com",
    "api.moonshot.cn",
    "api.qwen.aliyun.com",
}


def _is_safe_api_url(url: str) -> bool:
    """检查API URL是否安全（防止SSRF攻击）
    
    只允许白名单中的域名，防止管理员配置恶意URL导致SSRF攻击
    """
    import os
    
    if not url:
        return False
    
    parsed = urllib.parse.urlparse(url)
    
    # 只允许HTTPS协议
    if parsed.scheme != "https":
        return False
    
    # 获取主机名
    host = parsed.hostname
    if not host:
        return False
    
    # 检查是否在白名单中
    if host in ALLOWED_API_HOSTS:
        return True
    
    # 如果配置了允许自定义端点，进行额外的安全检查
    allow_custom = os.getenv("ROLE_CARD_ALLOW_CUSTOM_AI_ENDPOINT", "").lower() in {"1", "true", "yes", "on"}
    if allow_custom:
        # 检查是否是IP地址
        try:
            ip = ipaddress.ip_address(host)
            # 禁止内网IP、回环地址、链路本地地址
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False
        except ValueError:
            # 不是IP地址，是域名
            # 解析DNS并检查IP
            try:
                import socket
                resolved_ips = socket.getaddrinfo(host, None)
                for _, _, _, _, sockaddr in resolved_ips:
                    ip_str = sockaddr[0]
                    try:
                        ip = ipaddress.ip_address(ip_str)
                        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                            return False
                    except ValueError:
                        continue
            except socket.gaierror:
                # DNS解析失败
                return False
            
            # 检查是否包含可疑的域名模式
            host_lower = host.lower()
            blocked_suffixes = [
                ".local", ".internal", ".localhost",
                "127.", "0.0.0.0", "::1", "::",
            ]
            for suffix in blocked_suffixes:
                if host_lower.startswith(suffix) or host_lower.endswith(suffix):
                    return False
        
        return True
    
    # 不在白名单中且不允许自定义端点
    return False


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

        # 检查API URL安全性
        api_url = config.get("api_url", "")
        if not _is_safe_api_url(api_url):
            return False, "AI审核配置错误：不安全的API地址"

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
            # AI审核失败时不自动通过，返回错误状态
            return False, f"AI审核出错: {str(e)}"

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

        # 检查API URL安全性
        api_url = config.get("api_url", "")
        if not _is_safe_api_url(api_url):
            return False, "AI审核配置错误：不安全的API地址"

        try:
            prompt = AIReviewer.COMMENT_REVIEW_PROMPT.format(content=content)
            response = AIReviewer._call_api(prompt, config)
            return AIReviewer._parse_response(response)

        except Exception as e:
            # AI审核失败时不自动通过，返回错误状态
            return False, f"AI审核出错: {str(e)}"

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
        """解析 AI 审核响应
        
        注意：解析失败时返回 False（拒绝），需要人工审核，不自动通过
        """
        response = response.strip()

        # 提取 RESULT
        result_match = re.search(r'RESULT:\s*(\w+)', response, re.IGNORECASE)
        if not result_match:
            # 如果没有明确的 RESULT，尝试从内容中判断
            if "通过" in response or "PASS" in response.upper():
                return True, response
            elif "拒绝" in response or "REJECT" in response.upper() or "违规" in response:
                return False, response
            # 无法解析时返回 False（拒绝），需要人工审核
            return False, f"AI审核结果无法解析，进入人工审核。原始响应: {response[:200]}"

        result = result_match.group(1).upper()

        # 提取 REASON
        reason_match = re.search(r'REASON:\s*(.+?)(?=\n|$)', response, re.IGNORECASE | re.DOTALL)
        reason = reason_match.group(1).strip() if reason_match else response

        if result == "PASS":
            return True, reason
        elif result == "REJECT":
            return False, reason
        else:
            # 未知的审核结果，返回 False（拒绝），需要人工审核
            return False, f"AI审核返回未知结果: {result}，进入人工审核。原始响应: {response[:200]}"
