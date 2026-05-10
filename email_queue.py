"""
邮件队列系统 - 异步发送邮件，避免阻塞用户请求
使用后台线程处理邮件发送队列
"""
import json
import sqlite3
import threading
import time
import traceback
from datetime import datetime, timedelta
from enum import Enum
from html import escape

from models import get_db


class EmailType(str, Enum):
    """邮件类型"""
    VERIFICATION = "verification"      # 验证码
    REGISTER_SUCCESS = "register"      # 注册成功
    LOGIN_ALERT = "login"              # 登录提醒


class EmailQueue:
    """邮件队列管理器"""

    _instance = None
    _lock = threading.Lock()
    _worker_thread = None
    _running = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    @classmethod
    def init_db(cls) -> None:
        """初始化邮件队列表"""
        try:
            with get_db() as db:
                db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS email_queue (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        to_email TEXT NOT NULL,
                        email_type TEXT NOT NULL,
                        payload TEXT DEFAULT '{}',
                        subject TEXT NOT NULL,
                        body TEXT NOT NULL,
                        status TEXT DEFAULT 'pending',
                        error_msg TEXT DEFAULT '',
                        retry_count INTEGER DEFAULT 0,
                        created_at TEXT NOT NULL,
                        sent_at TEXT DEFAULT NULL
                    )
                    """
                )
                db.commit()
        except sqlite3.OperationalError:
            pass

    @classmethod
    def enqueue(cls, to_email: str, email_type: EmailType, subject: str, body: str, payload: dict = None) -> bool:
        """
        将邮件加入发送队列

        Args:
            to_email: 收件人邮箱
            email_type: 邮件类型
            subject: 邮件主题
            body: 邮件内容（HTML）
            payload: 额外数据（JSON序列化存储，发送后可删除body）

        Returns:
            是否成功加入队列
        """
        try:
            now = datetime.now().isoformat(timespec="seconds")
            payload_json = json.dumps(payload or {}, ensure_ascii=False)
            with get_db() as db:
                db.execute(
                    """
                    INSERT INTO email_queue (to_email, email_type, payload, subject, body, status, created_at)
                    VALUES (?, ?, ?, ?, ?, 'pending', ?)
                    """,
                    (to_email, email_type.value, payload_json, subject, body, now)
                )
                db.commit()
            return True
        except Exception as e:
            print(f"[邮件队列] 加入队列失败: {e}")
            return False

    @classmethod
    def claim_pending(cls, limit: int = 5) -> list:
        """
        原子性获取待发送邮件并标记为sending（防止多worker重复发送）

        使用事务确保同一封邮件只被一个worker领取
        """
        try:
            with get_db() as db:
                # 查找可发送的邮件（pending或可重试的failed）
                now = datetime.now().isoformat(timespec="seconds")
                retry_cutoff = (datetime.now() - timedelta(minutes=5)).isoformat(timespec="seconds")
                rows = db.execute(
                    """
                    SELECT * FROM email_queue
                    WHERE (status = 'pending' OR (status = 'failed' AND retry_count < 3 AND created_at < ?))
                    ORDER BY created_at ASC
                    LIMIT ?
                    """,
                    (retry_cutoff, limit)
                ).fetchall()

                claimed = []
                for row in rows:
                    # 原子性更新状态为sending
                    cursor = db.execute(
                        "UPDATE email_queue SET status = 'sending' WHERE id = ? AND status IN ('pending', 'failed')",
                        (row["id"],)
                    )
                    if cursor.rowcount > 0:
                        claimed.append(dict(row))

                db.commit()
            return claimed
        except sqlite3.OperationalError:
            return []

    @classmethod
    def mark_sent(cls, email_id: int) -> None:
        """标记邮件为已发送，并清除验证码类邮件的body"""
        now = datetime.now().isoformat(timespec="seconds")
        try:
            with get_db() as db:
                # 验证码类邮件发送成功后清除body（脱敏）
                row = db.execute("SELECT email_type FROM email_queue WHERE id = ?", (email_id,)).fetchone()
                if row and row["email_type"] == EmailType.VERIFICATION.value:
                    db.execute(
                        "UPDATE email_queue SET status = 'sent', body = '', sent_at = ? WHERE id = ?",
                        (now, email_id)
                    )
                else:
                    db.execute(
                        "UPDATE email_queue SET status = 'sent', sent_at = ? WHERE id = ?",
                        (now, email_id)
                    )
                db.commit()
        except sqlite3.OperationalError:
            pass

    @classmethod
    def mark_failed(cls, email_id: int, error_msg: str) -> None:
        """标记邮件发送失败，保持pending状态以便重试"""
        try:
            with get_db() as db:
                db.execute(
                    """
                    UPDATE email_queue
                    SET status = 'failed', error_msg = ?, retry_count = retry_count + 1
                    WHERE id = ?
                    """,
                    (error_msg[:500], email_id)
                )
                db.commit()
        except sqlite3.OperationalError:
            pass

    @classmethod
    def cleanup_old(cls, days: int = 7) -> None:
        """清理超过指定天数的已发送和失败邮件"""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        try:
            with get_db() as db:
                db.execute(
                    "DELETE FROM email_queue WHERE status IN ('sent', 'failed') AND created_at < ?",
                    (cutoff,)
                )
                db.commit()
        except sqlite3.OperationalError:
            pass

    @classmethod
    def start_worker(cls) -> None:
        """启动邮件发送工作线程"""
        if cls._running:
            return

        cls._running = True
        cls._worker_thread = threading.Thread(target=cls._worker_loop, daemon=True)
        cls._worker_thread.start()
        print("[邮件队列] 工作线程已启动")

    @classmethod
    def stop_worker(cls) -> None:
        """停止邮件发送工作线程"""
        cls._running = False
        if cls._worker_thread:
            cls._worker_thread.join(timeout=5)
        print("[邮件队列] 工作线程已停止")

    @classmethod
    def _worker_loop(cls) -> None:
        """工作线程主循环"""
        from email_service import _send_email_raw

        while cls._running:
            try:
                pending_emails = cls.claim_pending(limit=5)

                if not pending_emails:
                    time.sleep(1)
                    continue

                for email in pending_emails:
                    if not cls._running:
                        break

                    success, error = _send_email_raw(
                        to_email=email["to_email"],
                        subject=email["subject"],
                        body=email["body"]
                    )

                    if success:
                        cls.mark_sent(email["id"])
                        print(f"[邮件队列] 发送成功: {email['to_email']} - {email['email_type']}")
                    else:
                        cls.mark_failed(email["id"], error)
                        print(f"[邮件队列] 发送失败: {email['to_email']} - {error}")

                    # 每封邮件间隔0.5秒，避免触发限流
                    time.sleep(0.5)

                # 每处理一批后清理旧邮件
                if datetime.now().minute == 0:
                    cls.cleanup_old(days=7)

            except Exception as e:
                print(f"[邮件队列] 工作线程异常: {e}\n{traceback.format_exc()}")
                time.sleep(5)


# 便捷函数：快速将邮件加入队列
def queue_verification_email(to_email: str, code: str) -> bool:
    """将验证码邮件加入队列"""
    subject = "【角色卡平台】邮箱验证码"
    safe_code = escape(code)
    body = f"""
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
    return EmailQueue.enqueue(to_email, EmailType.VERIFICATION, subject, body, payload={"type": "verification"})


def queue_register_success_email(to_email: str, username: str) -> bool:
    """将注册成功邮件加入队列"""
    safe_username = escape(username)
    subject = "【角色卡平台】注册成功"
    body = f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; line-height: 1.6; color: #333; }}
        .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
        .header {{ background: #2f6f73; color: white; padding: 20px; text-align: center; border-radius: 8px 8px 0 0; }}
        .content {{ background: #f9fafb; padding: 30px; border-radius: 0 0 8px 8px; }}
        .footer {{ text-align: center; color: #9ca3af; font-size: 12px; margin-top: 20px; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h2>角色卡平台</h2>
        </div>
        <div class="content">
            <p>您好，<strong>{safe_username}</strong>，</p>
            <p>恭喜您！您的账号已成功注册。</p>
            <p>现在您可以：</p>
            <ul>
                <li>上传和分享您的角色卡</li>
                <li>发现其他创作者的作品</li>
                <li>参与社区互动</li>
            </ul>
            <p>如有任何问题，请联系管理员。</p>
        </div>
        <div class="footer">
            <p>此邮件由系统自动发送，请勿回复</p>
        </div>
    </div>
</body>
</html>
"""
    return EmailQueue.enqueue(to_email, EmailType.REGISTER_SUCCESS, subject, body, payload={"username": username})


def queue_login_alert_email(to_email: str, username: str, ip_address: str, login_time: str) -> bool:
    """将登录提醒邮件加入队列"""
    safe_username = escape(username)
    safe_ip = escape(ip_address)
    safe_time = escape(login_time)
    subject = "【角色卡平台】账号登录提醒"
    body = f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; line-height: 1.6; color: #333; }}
        .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
        .header {{ background: #2f6f73; color: white; padding: 20px; text-align: center; border-radius: 8px 8px 0 0; }}
        .content {{ background: #f9fafb; padding: 30px; border-radius: 0 0 8px 8px; }}
        .alert {{ background: #fef3c7; border-left: 4px solid #f59e0b; padding: 15px; margin: 15px 0; }}
        .footer {{ text-align: center; color: #9ca3af; font-size: 12px; margin-top: 20px; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h2>角色卡平台</h2>
        </div>
        <div class="content">
            <p>您好，<strong>{safe_username}</strong>，</p>
            <p>您的账号刚刚发生了登录行为：</p>
            <div class="alert">
                <p><strong>登录时间：</strong>{safe_time}</p>
                <p><strong>登录IP：</strong>{safe_ip}</p>
            </div>
            <p>如果这是您本人的操作，请忽略此邮件。</p>
            <p>如果不是您本人登录，请立即修改密码以确保账号安全。</p>
        </div>
        <div class="footer">
            <p>此邮件由系统自动发送，请勿回复</p>
        </div>
    </div>
</body>
</html>
"""
    return EmailQueue.enqueue(to_email, EmailType.LOGIN_ALERT, subject, body, payload={"username": username, "ip": ip_address})
