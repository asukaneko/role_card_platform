"""
邮件服务模块 - 处理邮件发送和验证码相关功能
"""
import hashlib
import hmac
import ipaddress
import os
import re
import secrets
import smtplib
import socket
from email.mime.text import MIMEText
from email.utils import formataddr
from html import escape

from flask import request

from models import EmailConfig, VerificationCode

# 验证码hash密钥（优先从环境变量读取，否则使用应用secret_key的派生值）
_CODE_HMAC_KEY = os.getenv("ROLE_CARD_CODE_HMAC_KEY", "role_card_platform_code_hmac_key_v1")

# 允许的SMTP端口
ALLOWED_SMTP_PORTS = {465, 587}

# 禁止连接的SMTP主机名
_BLOCKED_HOSTNAMES = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}


def is_valid_email(email: str) -> bool:
    """验证邮箱格式是否有效"""
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return bool(re.match(pattern, email))


def generate_verification_code(length: int = 6) -> str:
    """生成指定长度的数字验证码（密码学安全随机数）"""
    return ''.join(str(secrets.randbelow(10)) for _ in range(length))


def hash_code(email: str, code: str) -> str:
    """对验证码进行HMAC hash，避免明文存储"""
    return hmac.new(
        _CODE_HMAC_KEY.encode(),
        f"{email}:{code}".encode(),
        hashlib.sha256
    ).hexdigest()


def get_client_ip() -> str:
    """获取客户端IP地址（由ProxyFix统一处理，不自行读取X-Forwarded-For）"""
    return request.remote_addr or 'unknown'


def _is_private_smtp(host: str) -> bool:
    """检查SMTP服务器地址是否为私网/保留地址"""
    if host.lower() in _BLOCKED_HOSTNAMES:
        return True
    if host.endswith('.local') or host.endswith('.internal'):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
    except ValueError:
        # 不是IP地址（是域名），允许通过
        return False


def can_send_code(ip_address: str, max_per_minute: int = 5) -> bool:
    """检查指定IP是否还可以发送验证码（限流检查）"""
    count = VerificationCode.count_recent_by_ip(ip_address, minutes=1)
    return count < max_per_minute


def _send_email_raw(to_email: str, subject: str, body: str) -> tuple[bool, str]:
    """
    底层邮件发送函数（被队列工作线程调用）

    Returns:
        (success: bool, error_message: str)
    """
    config = EmailConfig.get()

    if not config.get("enabled"):
        return False, "邮件服务未启用"

    smtp_server = config.get("smtp_server", "").strip()
    smtp_port = int(config.get("smtp_port", 587))
    smtp_username = config.get("smtp_username", "").strip()
    smtp_password = config.get("smtp_password", "").strip()
    sender_email = config.get("sender_email", "").strip()
    sender_name = config.get("sender_name", "角色卡平台").strip()

    if not all([smtp_server, smtp_username, smtp_password, sender_email]):
        return False, "邮件服务器配置不完整"

    # 安全检查：限制SMTP端口
    if smtp_port not in ALLOWED_SMTP_PORTS:
        return False, f"不支持的SMTP端口 {smtp_port}，仅允许 {', '.join(str(p) for p in sorted(ALLOWED_SMTP_PORTS))}"

    # 安全检查：禁止连接私网地址（除非显式允许）
    allow_private = os.getenv("ROLE_CARD_ALLOW_PRIVATE_SMTP", "").lower() == "true"
    if not allow_private and _is_private_smtp(smtp_server):
        return False, "不允许连接私网SMTP服务器"

    msg = MIMEText(body, 'html', 'utf-8')
    msg['From'] = formataddr((sender_name, sender_email))
    msg['To'] = to_email
    msg['Subject'] = subject

    try:
        # 强制加密连接：465用SSL，587用STARTTLS
        if smtp_port == 465:
            server = smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=30)
        elif smtp_port == 587:
            server = smtplib.SMTP(smtp_server, smtp_port, timeout=30)
            server.ehlo()
            server.starttls()
            server.ehlo()
        else:
            return False, f"不支持的SMTP端口 {smtp_port}"

        if smtp_username and smtp_password:
            server.login(smtp_username, smtp_password)

        server.sendmail(sender_email, [to_email], msg.as_string())
        server.quit()
        return True, ""
    except smtplib.SMTPAuthenticationError:
        return False, "邮件服务器认证失败"
    except smtplib.SMTPConnectError:
        return False, "无法连接到邮件服务器"
    except socket.timeout:
        return False, "连接邮件服务器超时"
    except socket.gaierror:
        return False, "无法解析SMTP服务器地址"
    except Exception as e:
        error_msg = str(e)
        if "timed out" in error_msg.lower() or "timeout" in error_msg.lower():
            return False, "连接邮件服务器超时"
        return False, f"邮件发送失败: {error_msg}"


def send_code(to_email: str) -> tuple[bool, str]:
    """
    发送验证码（包含限流检查，使用邮件队列异步发送）

    Returns:
        (success: bool, message: str)
    """
    import traceback

    if not is_valid_email(to_email):
        return False, "邮箱格式不正确"

    ip_address = get_client_ip()

    try:
        if not can_send_code(ip_address, max_per_minute=5):
            return False, "发送过于频繁，请稍后再试"
    except Exception as e:
        print(f"[限流检查错误] {str(e)}\n{traceback.format_exc()}")
        return False, "服务暂时不可用，请稍后再试"

    code = generate_verification_code()

    # 将邮件加入队列（异步发送）
    from email_queue import queue_verification_email
    queued = queue_verification_email(to_email, code)

    if not queued:
        # 队列失败，尝试同步发送
        success, error = _send_email_raw(
            to_email=to_email,
            subject="【角色卡平台】邮箱验证码",
            body=_build_verification_email_body(code)
        )
        if not success:
            return False, error

    # 保存验证码hash记录（不存明文）
    try:
        code_hash = hash_code(to_email, code)
        VerificationCode.create(to_email, code_hash, ip_address, expires_minutes=10)
    except Exception as e:
        print(f"[保存验证码错误] {str(e)}\n{traceback.format_exc()}")

    return True, "验证码已发送，请查收邮件"


def _build_verification_email_body(code: str) -> str:
    """构建验证码邮件内容"""
    safe_code = escape(code)
    return f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; line-height: 1.6; color: #333; }}
        .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
        .header {{ background: #2f6f73; color: white; padding: 20px; text-align: center; border-radius: 8px 8px 0 0; }}
        .content {{ background: #f9fafb; padding: 30px; border-radius: 0 0 8px 8px; }}
        .code {{ font-size: 32px; font-weight: bold; color: #2f6f73; letter-spacing: 8px; text-align: center; margin: 20px 0; }}
        .footer {{ text-align: center; color: #9ca3af; font-size: 12px; margin-top: 20px; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h2>角色卡平台</h2>
        </div>
        <div class="content">
            <p>您好，</p>
            <p>您正在进行账号注册，请使用以下验证码完成验证：</p>
            <div class="code">{safe_code}</div>
            <p>验证码有效期为 10 分钟，请勿泄露给他人。</p>
            <p>如非本人操作，请忽略此邮件。</p>
        </div>
        <div class="footer">
            <p>此邮件由系统自动发送，请勿回复</p>
        </div>
    </div>
</body>
</html>
"""


def verify_code(email: str, code: str) -> bool:
    """验证邮箱验证码（使用hash比较）"""
    if not is_valid_email(email) or not code:
        return False
    code_hash = hash_code(email, code)
    return VerificationCode.verify(email, code_hash)
