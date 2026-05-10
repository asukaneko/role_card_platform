"""
数据库模型 - 包含所有数据库操作和模型函数
"""
import json
import sqlite3
from datetime import datetime
from typing import Optional

from flask import abort, session

from .config import DATA_DIR, DB_PATH


def ensure_dirs() -> None:
    """确保必要的目录存在"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def get_db():
    """获取数据库连接"""
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """初始化数据库表结构"""
    with get_db() as db:
        # 用户表
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                display_name TEXT DEFAULT '',
                bio TEXT DEFAULT '',
                api_token TEXT NOT NULL DEFAULT '',
                avatar_path TEXT DEFAULT '',
                created_at TEXT NOT NULL
            )
            """
        )

        # 角色卡表
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
                views INTEGER DEFAULT 0,
                source_format TEXT DEFAULT 'platform',
                raw_json TEXT DEFAULT '{}',
                user_id INTEGER DEFAULT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                basic_info TEXT DEFAULT '',
                example_dialogues TEXT DEFAULT '',
                response_format TEXT DEFAULT '',
                rules_json TEXT DEFAULT '[]',
                state_json TEXT DEFAULT '{}',
                status TEXT DEFAULT 'pending',
                reviewed_by INTEGER DEFAULT NULL,
                reviewed_at TEXT DEFAULT NULL,
                review_result TEXT DEFAULT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (reviewed_by) REFERENCES users(id)
            )
            """
        )

        # 迁移：添加缺失的列
        columns = {row["name"] for row in db.execute("PRAGMA table_info(role_cards)").fetchall()}
        migrations = [
            ("source_format", "ALTER TABLE role_cards ADD COLUMN source_format TEXT DEFAULT 'platform'"),
            ("raw_json", "ALTER TABLE role_cards ADD COLUMN raw_json TEXT DEFAULT '{}'"),
            ("user_id", "ALTER TABLE role_cards ADD COLUMN user_id INTEGER DEFAULT NULL"),
            ("basic_info", "ALTER TABLE role_cards ADD COLUMN basic_info TEXT DEFAULT ''"),
            ("example_dialogues", "ALTER TABLE role_cards ADD COLUMN example_dialogues TEXT DEFAULT ''"),
            ("response_format", "ALTER TABLE role_cards ADD COLUMN response_format TEXT DEFAULT ''"),
            ("rules_json", "ALTER TABLE role_cards ADD COLUMN rules_json TEXT DEFAULT '[]'"),
            ("state_json", "ALTER TABLE role_cards ADD COLUMN state_json TEXT DEFAULT '{}'"),
            ("status", "ALTER TABLE role_cards ADD COLUMN status TEXT DEFAULT 'pending'"),
            ("reviewed_by", "ALTER TABLE role_cards ADD COLUMN reviewed_by INTEGER DEFAULT NULL"),
            ("reviewed_at", "ALTER TABLE role_cards ADD COLUMN reviewed_at TEXT DEFAULT NULL"),
            ("review_result", "ALTER TABLE role_cards ADD COLUMN review_result TEXT DEFAULT NULL"),
            ("views", "ALTER TABLE role_cards ADD COLUMN views INTEGER DEFAULT 0"),
        ]
        for col, sql in migrations:
            if col not in columns:
                db.execute(sql)

        # 用户表迁移
        user_columns = {row["name"] for row in db.execute("PRAGMA table_info(users)").fetchall()}
        # api_token_hash 用于存储 API Token 的 hash 值（安全存储）
        if "api_token_hash" not in user_columns:
            db.execute("ALTER TABLE users ADD COLUMN api_token_hash TEXT DEFAULT ''")
        # 保留 api_token 字段用于向后兼容，新用户不再使用
        if "api_token" not in user_columns:
            db.execute("ALTER TABLE users ADD COLUMN api_token TEXT NOT NULL DEFAULT ''")
        if "avatar_path" not in user_columns:
            db.execute("ALTER TABLE users ADD COLUMN avatar_path TEXT DEFAULT ''")
        if "is_admin" not in user_columns:
            db.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0")
        if "email" not in user_columns:
            db.execute("ALTER TABLE users ADD COLUMN email TEXT DEFAULT ''")
        if "email_verified" not in user_columns:
            db.execute("ALTER TABLE users ADD COLUMN email_verified INTEGER DEFAULT 0")
        if "level" not in user_columns:
            db.execute("ALTER TABLE users ADD COLUMN level INTEGER DEFAULT 1")
        if "exp" not in user_columns:
            db.execute("ALTER TABLE users ADD COLUMN exp INTEGER DEFAULT 0")

        # 评论表
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                card_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                reviewed_by INTEGER DEFAULT NULL,
                reviewed_at TEXT DEFAULT NULL,
                review_result TEXT DEFAULT NULL,
                FOREIGN KEY (card_id) REFERENCES role_cards(id) ON DELETE CASCADE,
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (reviewed_by) REFERENCES users(id)
            )
            """
        )

        # 评论表迁移：添加审核相关字段
        comment_columns = {row["name"] for row in db.execute("PRAGMA table_info(comments)").fetchall()}
        comment_migrations = [
            ("status", "ALTER TABLE comments ADD COLUMN status TEXT DEFAULT 'pending'"),
            ("reviewed_by", "ALTER TABLE comments ADD COLUMN reviewed_by INTEGER DEFAULT NULL"),
            ("reviewed_at", "ALTER TABLE comments ADD COLUMN reviewed_at TEXT DEFAULT NULL"),
            ("review_result", "ALTER TABLE comments ADD COLUMN review_result TEXT DEFAULT NULL"),
        ]
        for col, sql in comment_migrations:
            if col not in comment_columns:
                db.execute(sql)
        # 更新已存在的评论状态为 approved（兼容旧数据）
        if "status" in comment_columns:
            db.execute("UPDATE comments SET status = 'approved' WHERE status IS NULL")

        # 审核员表
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS reviewers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL UNIQUE,
                created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (created_by) REFERENCES users(id)
            )
            """
        )

        # AI审核配置表
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_review_config (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                api_key TEXT DEFAULT '',
                api_url TEXT DEFAULT '',
                model TEXT DEFAULT '',
                enabled INTEGER DEFAULT 0,
                updated_at TEXT NOT NULL
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

        # 用户收藏记录表
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS user_favorites (
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

        # 邮件配置表
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS email_config (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                smtp_server TEXT DEFAULT '',
                smtp_port INTEGER DEFAULT 587,
                smtp_username TEXT DEFAULT '',
                smtp_password TEXT DEFAULT '',
                sender_email TEXT DEFAULT '',
                sender_name TEXT DEFAULT '',
                use_tls INTEGER DEFAULT 1,
                enabled INTEGER DEFAULT 0,
                updated_at TEXT NOT NULL
            )
            """
        )

        # 验证码记录表
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS verification_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                code TEXT NOT NULL,
                ip_address TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used INTEGER DEFAULT 0
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS card_relations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                card_id INTEGER NOT NULL,
                related_card_id INTEGER NOT NULL,
                relation_type TEXT DEFAULT 'related',
                created_at TEXT NOT NULL,
                FOREIGN KEY (card_id) REFERENCES role_cards(id) ON DELETE CASCADE,
                FOREIGN KEY (related_card_id) REFERENCES role_cards(id) ON DELETE CASCADE,
                UNIQUE(card_id, related_card_id)
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS user_follows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                follower_id INTEGER NOT NULL,
                following_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (follower_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (following_id) REFERENCES users(id) ON DELETE CASCADE,
                UNIQUE(follower_id, following_id)
            )
            """
        )
        db.commit()


class User:
    """用户模型"""

    @staticmethod
    def get_by_id(user_id: int) -> Optional[dict]:
        """通过 ID 获取用户"""
        with get_db() as db:
            row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def get_by_username(username: str) -> Optional[dict]:
        """通过用户名获取用户"""
        with get_db() as db:
            row = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def get_by_api_token(token: str) -> Optional[dict]:
        """通过 API Token 获取用户"""
        with get_db() as db:
            row = db.execute("SELECT * FROM users WHERE api_token = ?", (token,)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def create(username: str, password_hash: str, display_name: str = "", api_token: str = "") -> dict:
        """创建新用户"""
        now = datetime.now().isoformat(timespec="seconds")
        # 计算 api_token 的 hash
        from .auth import hash_api_token
        api_token_hash = hash_api_token(api_token) if api_token else ""
        
        with get_db() as db:
            db.execute(
                "INSERT INTO users (username, password_hash, display_name, bio, api_token, api_token_hash, created_at) VALUES (?, ?, ?, '', ?, ?, ?)",
                (username, password_hash, display_name or username, api_token, api_token_hash, now),
            )
            db.commit()
            row = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return dict(row)

    @staticmethod
    def update_api_token(user_id: int, token: str) -> None:
        """更新用户 API Token（存储 hash）"""
        from .auth import hash_api_token
        token_hash = hash_api_token(token) if token else ""
        with get_db() as db:
            db.execute(
                "UPDATE users SET api_token = ?, api_token_hash = ? WHERE id = ?",
                (token, token_hash, user_id)
            )
            db.commit()

    @staticmethod
    def update_profile(user_id: int, display_name: str, bio: str, avatar_path: str = None) -> None:
        """更新用户资料"""
        with get_db() as db:
            if avatar_path is not None:
                db.execute(
                    "UPDATE users SET display_name = ?, bio = ?, avatar_path = ? WHERE id = ?",
                    (display_name, bio, avatar_path, user_id)
                )
            else:
                db.execute(
                    "UPDATE users SET display_name = ?, bio = ? WHERE id = ?",
                    (display_name, bio, user_id)
                )
            db.commit()

    @staticmethod
    def delete(user_id: int) -> None:
        """删除用户"""
        with get_db() as db:
            db.execute("DELETE FROM users WHERE id = ?", (user_id,))
            db.commit()

    @staticmethod
    def list_all() -> list:
        """获取所有用户列表（带统计信息）"""
        with get_db() as db:
            rows = db.execute(
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
        return [dict(row) for row in rows]

    @staticmethod
    def add_exp(user_id: int, exp: int) -> dict:
        """增加用户经验值，并自动升级
        
        等级规则：
        - Lv1: 0-99 EXP
        - Lv2: 100-299 EXP  
        - Lv3: 300-599 EXP
        - Lv4: 600-999 EXP
        - Lv5: 1000-1499 EXP
        - Lv6+: 每500 EXP升一级
        
        Returns:
            包含升级信息的字典
        """
        user = User.get_by_id(user_id)
        if not user:
            return None
            
        old_level = user.get("level", 1)
        new_exp = user.get("exp", 0) + exp
        
        # 计算新等级
        if new_exp < 100:
            new_level = 1
        elif new_exp < 300:
            new_level = 2
        elif new_exp < 600:
            new_level = 3
        elif new_exp < 1000:
            new_level = 4
        elif new_exp < 1500:
            new_level = 5
        else:
            new_level = 5 + (new_exp - 1500) // 500 + 1
        
        with get_db() as db:
            db.execute(
                "UPDATE users SET exp = ?, level = ? WHERE id = ?",
                (new_exp, new_level, user_id)
            )
            db.commit()
        
        return {
            "old_level": old_level,
            "new_level": new_level,
            "exp_gained": exp,
            "total_exp": new_exp,
            "level_up": new_level > old_level
        }

    @staticmethod
    def get_level_title(level: int) -> str:
        """获取等级称号"""
        titles = {
            1: "新手",
            2: "学徒",
            3: "创作者",
            4: "资深创作者",
            5: "大师",
            6: "传说",
            7: "神话",
            8: "半神",
            9: "神明",
            10: "创世神"
        }
        return titles.get(level, f"Lv{level}")

    @staticmethod
    def get_user_stats(user_id: int) -> dict:
        """获取用户详细统计数据"""
        with get_db() as db:
            # 基础信息
            user = User.get_by_id(user_id)
            if not user:
                return None
            
            # 统计角色卡数据
            card_stats = db.execute(
                """
                SELECT 
                    COUNT(*) as total_cards,
                    SUM(likes) as total_likes,
                    SUM(views) as total_views,
                    SUM(downloads) as total_downloads
                FROM role_cards
                WHERE user_id = ? AND status = 'approved'
                """,
                (user_id,)
            ).fetchone()
            
            # 统计评论数
            comment_count = db.execute(
                "SELECT COUNT(*) FROM comments WHERE user_id = ?",
                (user_id,)
            ).fetchone()[0]
            
            # 获得的总点赞数（角色卡点赞）
            total_likes = card_stats["total_likes"] or 0
            
            # 计算排名
            rank = db.execute(
                """
                SELECT COUNT(*) + 1 FROM users
                WHERE exp > ?
                """,
                (user.get("exp", 0),)
            ).fetchone()[0]
            
        return {
            "user": user,
            "level_title": User.get_level_title(user.get("level", 1)),
            "cards": {
                "total": card_stats["total_cards"] or 0,
                "likes": total_likes,
                "views": card_stats["total_views"] or 0,
                "downloads": card_stats["total_downloads"] or 0,
            },
            "comments": comment_count,
            "rank": rank,
            "next_level_exp": User.get_next_level_exp(user.get("level", 1))
        }

    @staticmethod
    def get_next_level_exp(level: int) -> int:
        """获取下一级所需经验值"""
        if level == 1:
            return 100
        elif level == 2:
            return 300
        elif level == 3:
            return 600
        elif level == 4:
            return 1000
        elif level == 5:
            return 1500
        else:
            return 1500 + (level - 5) * 500

    @staticmethod
    def get_leaderboard(limit: int = 10) -> list:
        """获取用户排行榜（按经验值），排除管理员"""
        with get_db() as db:
            rows = db.execute(
                """
                SELECT u.*,
                       COUNT(DISTINCT rc.id) as card_count
                FROM users u
                LEFT JOIN role_cards rc ON u.id = rc.user_id AND rc.status = 'approved'
                WHERE u.is_admin = 0 OR u.is_admin IS NULL
                GROUP BY u.id
                ORDER BY u.exp DESC, u.level DESC
                LIMIT ?
                """,
                (limit,)
            ).fetchall()
        
        result = []
        for i, row in enumerate(rows, 1):
            user = dict(row)
            user["rank"] = i
            user["level_title"] = User.get_level_title(user.get("level", 1))
            result.append(user)
        return result


class RoleCard:
    """角色卡模型"""

    @staticmethod
    def row_to_card(row) -> dict:
        """将数据库行转换为角色卡字典"""
        card = dict(row)
        # 解析 JSON 字段
        json_fields = [
            ("tags_json", "tags", []),
            ("rules_json", "rules", []),
            ("state_json", "state", {}),
        ]
        for json_col, py_key, default in json_fields:
            try:
                card[py_key] = json.loads(card.pop(json_col) or json.dumps(default))
            except Exception:
                card[py_key] = default

        # 查询角色卡所属用户的用户名
        user_id = card.get("user_id")
        if user_id:
            with get_db() as db:
                user_row = db.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
            if user_row:
                card["owner_username"] = user_row["username"]
        return card

    @staticmethod
    def get_by_id(card_id: int, include_pending: bool = False) -> Optional[dict]:
        """通过 ID 获取角色卡"""
        with get_db() as db:
            if include_pending:
                row = db.execute("SELECT * FROM role_cards WHERE id = ?", (card_id,)).fetchone()
            else:
                row = db.execute(
                    "SELECT * FROM role_cards WHERE id = ? AND status = 'approved'",
                    (card_id,)
                ).fetchone()
        return RoleCard.row_to_card(row) if row else None

    @staticmethod
    def get_by_slug(slug: str, include_pending: bool = False) -> Optional[dict]:
        """通过 slug 获取角色卡"""
        with get_db() as db:
            if include_pending:
                row = db.execute("SELECT * FROM role_cards WHERE slug = ?", (slug,)).fetchone()
            else:
                row = db.execute(
                    "SELECT * FROM role_cards WHERE slug = ? AND status = 'approved'",
                    (slug,)
                ).fetchone()
        return RoleCard.row_to_card(row) if row else None

    @staticmethod
    def get_or_404(identifier, include_pending: bool = False):
        """获取角色卡，不存在则返回 404"""
        with get_db() as db:
            if str(identifier).isdigit():
                if include_pending:
                    row = db.execute("SELECT * FROM role_cards WHERE id = ?", (identifier,)).fetchone()
                else:
                    row = db.execute(
                        "SELECT * FROM role_cards WHERE id = ? AND status = 'approved'",
                        (identifier,)
                    ).fetchone()
            else:
                if include_pending:
                    row = db.execute("SELECT * FROM role_cards WHERE slug = ?", (identifier,)).fetchone()
                else:
                    row = db.execute(
                        "SELECT * FROM role_cards WHERE slug = ? AND status = 'approved'",
                        (identifier,)
                    ).fetchone()
        if not row:
            abort(404)
        return RoleCard.row_to_card(row)

    @staticmethod
    def create(card_data: dict, avatar_path: str = "", user_id: int = None, status: str = "pending") -> dict:
        """创建新角色卡

        Args:
            card_data: 角色卡数据
            avatar_path: 头像路径
            user_id: 上传用户ID
            status: 初始状态，默认pending（网页上传需审核），api上传可指定approved
        """
        now = datetime.now().isoformat(timespec="seconds")
        with get_db() as db:
            from .utils import unique_slug
            slug = unique_slug(db, card_data["name"])
            db.execute(
                """
                INSERT INTO role_cards (
                    name, slug, avatar_path, description, personality, scenario,
                    first_message, system_prompt, tags_json, creator, visibility,
                    source_format, raw_json, user_id, created_at, updated_at,
                    basic_info, example_dialogues, response_format, rules_json, state_json,
                    status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    card_data["name"],
                    slug,
                    avatar_path,
                    card_data.get("description", ""),
                    card_data.get("personality", ""),
                    card_data.get("scenario", ""),
                    card_data.get("first_message", ""),
                    card_data.get("system_prompt", ""),
                    json.dumps(card_data.get("tags", []), ensure_ascii=False),
                    card_data.get("creator", ""),
                    card_data.get("visibility", "public"),
                    card_data.get("source_format", "platform"),
                    json.dumps(card_data.get("raw_json", {}), ensure_ascii=False),
                    user_id,
                    now,
                    now,
                    card_data.get("basic_info", ""),
                    card_data.get("example_dialogues", ""),
                    card_data.get("response_format", ""),
                    json.dumps(card_data.get("rules", []), ensure_ascii=False),
                    json.dumps(card_data.get("state", {}), ensure_ascii=False),
                    status,
                ),
            )
            db.commit()
            row = db.execute("SELECT * FROM role_cards WHERE slug = ?", (slug,)).fetchone()
        return RoleCard.row_to_card(row)

    @staticmethod
    def update(card_id: int, card_data: dict, avatar_path: str = None) -> None:
        """更新角色卡"""
        now = datetime.now().isoformat(timespec="seconds")
        with get_db() as db:
            sets = [
                "name = ?", "description = ?", "personality = ?",
                "scenario = ?", "first_message = ?", "system_prompt = ?",
                "tags_json = ?", "creator = ?", "visibility = ?",
                "updated_at = ?",
                "basic_info = ?", "example_dialogues = ?", "response_format = ?",
                "rules_json = ?", "state_json = ?",
            ]
            params = [
                card_data["name"],
                card_data.get("description", ""),
                card_data.get("personality", ""),
                card_data.get("scenario", ""),
                card_data.get("first_message", ""),
                card_data.get("system_prompt", ""),
                json.dumps(card_data.get("tags", []), ensure_ascii=False),
                card_data.get("creator", ""),
                card_data.get("visibility", "public"),
                now,
                card_data.get("basic_info", ""),
                card_data.get("example_dialogues", ""),
                card_data.get("response_format", ""),
                json.dumps(card_data.get("rules", []), ensure_ascii=False),
                json.dumps(card_data.get("state", {}), ensure_ascii=False),
            ]
            if avatar_path:
                sets.append("avatar_path = ?")
                params.append(avatar_path)

            params.append(card_id)
            db.execute(f"UPDATE role_cards SET {', '.join(sets)} WHERE id = ?", params)
            db.commit()

    @staticmethod
    def delete(card_id: int) -> None:
        """删除角色卡"""
        with get_db() as db:
            db.execute("DELETE FROM role_cards WHERE id = ?", (card_id,))
            db.commit()

    @staticmethod
    def set_visibility(card_id: int, visibility: str) -> None:
        """设置角色卡可见性"""
        now = datetime.now().isoformat(timespec="seconds")
        with get_db() as db:
            db.execute(
                "UPDATE role_cards SET visibility = ?, updated_at = ? WHERE id = ?",
                (visibility, now, card_id)
            )
            db.commit()

    @staticmethod
    def increment_downloads(card_id: int) -> None:
        """增加下载计数"""
        with get_db() as db:
            db.execute("UPDATE role_cards SET downloads = downloads + 1 WHERE id = ?", (card_id,))
            db.commit()

    @staticmethod
    def increment_likes(card_id: int) -> None:
        """增加点赞计数"""
        with get_db() as db:
            db.execute("UPDATE role_cards SET likes = likes + 1 WHERE id = ?", (card_id,))
            db.commit()

    @staticmethod
    def increment_views(card_id: int) -> None:
        """增加浏览计数"""
        with get_db() as db:
            db.execute("UPDATE role_cards SET views = views + 1 WHERE id = ?", (card_id,))
            db.commit()

    @staticmethod
    def get_leaderboard(sort_by: str = "likes", limit: int = 10) -> list:
        """获取排行榜
        
        Args:
            sort_by: 排序方式 - likes(点赞), views(浏览), downloads(下载), newest(最新)
            limit: 返回数量
        """
        order_by = {
            "likes": "likes DESC, views DESC",
            "views": "views DESC, likes DESC",
            "downloads": "downloads DESC, likes DESC",
            "newest": "created_at DESC",
        }.get(sort_by, "likes DESC")
        
        with get_db() as db:
            rows = db.execute(
                f"""
                SELECT rc.*, u.username as owner_username
                FROM role_cards rc
                LEFT JOIN users u ON rc.user_id = u.id
                WHERE rc.status = 'approved' AND rc.visibility = 'public'
                ORDER BY {order_by}
                LIMIT ?
                """,
                (limit,)
            ).fetchall()
        return [RoleCard.row_to_card(row) for row in rows]

    @staticmethod
    def search(query: str = "", tag: str = "", sort: str = "latest", visibility: str = None, include_pending: bool = False) -> list:
        """搜索角色卡"""
        where = []
        params = []

        if visibility:
            # 参数化查询，防止 SQL 注入
            if visibility in {"public", "private"}:
                where.append("visibility = ?")
                params.append(visibility)
            else:
                # 无效的 visibility 值，默认使用 public
                where.append("visibility = 'public'")
        else:
            where.append("visibility = 'public'")

        # 默认只显示已审核的内容
        if not include_pending:
            where.append("status = 'approved'")

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
        return [RoleCard.row_to_card(row) for row in rows]

    @staticmethod
    def get_by_user(user_id: int, include_private: bool = False, include_pending: bool = False) -> list:
        """获取用户的角色卡"""
        with get_db() as db:
            if include_private:
                # 用户自己查看自己的卡片，显示所有状态
                rows = db.execute(
                    "SELECT * FROM role_cards WHERE user_id = ? ORDER BY created_at DESC",
                    (user_id,),
                ).fetchall()
            else:
                # 其他人查看，只显示公开且已审核的
                if include_pending:
                    rows = db.execute(
                        "SELECT * FROM role_cards WHERE user_id = ? AND visibility = 'public' ORDER BY created_at DESC",
                        (user_id,),
                    ).fetchall()
                else:
                    rows = db.execute(
                        "SELECT * FROM role_cards WHERE user_id = ? AND visibility = 'public' AND status = 'approved' ORDER BY created_at DESC",
                        (user_id,),
                    ).fetchall()
        return [RoleCard.row_to_card(row) for row in rows]

    @staticmethod
    def get_all_tags() -> list:
        """获取所有标签"""
        from .utils import normalize_tags
        with get_db() as db:
            rows = db.execute(
                "SELECT tags_json FROM role_cards WHERE visibility = 'public' AND status = 'approved'"
            ).fetchall()

        all_tags = []
        for row in rows:
            for item in normalize_tags(json.loads(row["tags_json"] or "[]")):
                if item not in all_tags:
                    all_tags.append(item)
        return all_tags


class Comment:
    """评论模型"""

    @staticmethod
    def get_by_card(card_id: int, include_pending: bool = False) -> list:
        """获取角色卡的所有评论"""
        with get_db() as db:
            if include_pending:
                rows = db.execute(
                    """
                    SELECT c.*, u.username, u.display_name
                    FROM comments c
                    JOIN users u ON c.user_id = u.id
                    WHERE c.card_id = ? ORDER BY c.created_at ASC
                    """,
                    (card_id,),
                ).fetchall()
            else:
                rows = db.execute(
                    """
                    SELECT c.*, u.username, u.display_name
                    FROM comments c
                    JOIN users u ON c.user_id = u.id
                    WHERE c.card_id = ? AND c.status = 'approved' ORDER BY c.created_at ASC
                    """,
                    (card_id,),
                ).fetchall()

        current_user_id = session.get("user_id")
        comments = []
        for row in rows:
            item = dict(row)
            item["can_delete"] = current_user_id == row["user_id"]
            comments.append(item)
        return comments

    @staticmethod
    def create(card_id: int, user_id: int, content: str) -> None:
        """创建评论（默认进入待审核状态）"""
        now = datetime.now().isoformat(timespec="seconds")
        with get_db() as db:
            db.execute(
                "INSERT INTO comments (card_id, user_id, content, created_at, status) VALUES (?, ?, ?, ?, 'pending')",
                (card_id, user_id, content, now),
            )
            db.commit()

    @staticmethod
    def delete(comment_id: int) -> None:
        """删除评论"""
        with get_db() as db:
            db.execute("DELETE FROM comments WHERE id = ?", (comment_id,))
            db.commit()

    @staticmethod
    def get_by_id(comment_id: int) -> Optional[dict]:
        """通过 ID 获取评论"""
        with get_db() as db:
            row = db.execute("SELECT * FROM comments WHERE id = ?", (comment_id,)).fetchone()
        return dict(row) if row else None


class UserLike:
    """用户点赞模型"""

    @staticmethod
    def exists(user_id: int, card_id: int) -> bool:
        """检查用户是否已点赞"""
        with get_db() as db:
            row = db.execute(
                "SELECT id FROM user_likes WHERE user_id = ? AND card_id = ?",
                (user_id, card_id)
            ).fetchone()
        return row is not None

    @staticmethod
    def create(user_id: int, card_id: int) -> None:
        """创建点赞记录"""
        now = datetime.now().isoformat()
        with get_db() as db:
            db.execute(
                "INSERT INTO user_likes (user_id, card_id, created_at) VALUES (?, ?, ?)",
                (user_id, card_id, now)
            )
            db.commit()

    @staticmethod
    def delete_by_card(card_id: int) -> None:
        """删除角色卡的所有点赞记录"""
        with get_db() as db:
            db.execute("DELETE FROM user_likes WHERE card_id = ?", (card_id,))
            db.commit()


class UserFavorite:
    """用户收藏模型"""

    @staticmethod
    def exists(user_id: int, card_id: int) -> bool:
        """检查用户是否已收藏"""
        with get_db() as db:
            row = db.execute(
                "SELECT id FROM user_favorites WHERE user_id = ? AND card_id = ?",
                (user_id, card_id)
            ).fetchone()
        return row is not None

    @staticmethod
    def add(user_id: int, card_id: int) -> None:
        """添加收藏"""
        now = datetime.now().isoformat()
        with get_db() as db:
            db.execute(
                "INSERT INTO user_favorites (user_id, card_id, created_at) VALUES (?, ?, ?)",
                (user_id, card_id, now)
            )
            db.commit()

    @staticmethod
    def remove(user_id: int, card_id: int) -> None:
        """取消收藏"""
        with get_db() as db:
            db.execute(
                "DELETE FROM user_favorites WHERE user_id = ? AND card_id = ?",
                (user_id, card_id)
            )
            db.commit()

    @staticmethod
    def get_by_user(user_id: int) -> list:
        """获取用户收藏的角色卡列表"""
        with get_db() as db:
            rows = db.execute(
                """
                SELECT rc.* FROM role_cards rc
                JOIN user_favorites uf ON rc.id = uf.card_id
                WHERE uf.user_id = ? AND rc.visibility = 'public' AND rc.status = 'approved'
                ORDER BY uf.created_at DESC
                """,
                (user_id,)
            ).fetchall()
        return [RoleCard.row_to_card(row) for row in rows]

    @staticmethod
    def count_by_user(user_id: int) -> int:
        """获取用户收藏的角色卡数量"""
        with get_db() as db:
            row = db.execute(
                """
                SELECT COUNT(*) as cnt FROM user_favorites uf
                JOIN role_cards rc ON rc.id = uf.card_id
                WHERE uf.user_id = ? AND rc.visibility = 'public' AND rc.status = 'approved'
                """,
                (user_id,)
            ).fetchone()
        return row["cnt"] if row else 0


class Reviewer:
    """审核员模型"""

    @staticmethod
    def is_reviewer(user_id: int) -> bool:
        """检查用户是否为审核员（admin 用户默认是审核员）"""
        # 先检查是否是 admin 用户
        with get_db() as db:
            user_row = db.execute(
                "SELECT is_admin FROM users WHERE id = ?",
                (user_id,)
            ).fetchone()
            if user_row and user_row["is_admin"]:
                return True
            
            # 再检查是否在审核员表中
            row = db.execute(
                "SELECT id FROM reviewers WHERE user_id = ?",
                (user_id,)
            ).fetchone()
        return row is not None

    @staticmethod
    def add(user_id: int, created_by: int) -> None:
        """添加审核员"""
        now = datetime.now().isoformat(timespec="seconds")
        with get_db() as db:
            db.execute(
                "INSERT INTO reviewers (user_id, created_by, created_at) VALUES (?, ?, ?)",
                (user_id, created_by, now)
            )
            db.commit()

    @staticmethod
    def remove(user_id: int) -> None:
        """移除审核员"""
        with get_db() as db:
            db.execute("DELETE FROM reviewers WHERE user_id = ?", (user_id,))
            db.commit()

    @staticmethod
    def list_all() -> list:
        """获取所有审核员列表"""
        with get_db() as db:
            rows = db.execute(
                """
                SELECT r.*, u.username, u.display_name, u.avatar_path,
                       creator.username as creator_username
                FROM reviewers r
                JOIN users u ON r.user_id = u.id
                JOIN users creator ON r.created_by = creator.id
                ORDER BY r.created_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]


class ReviewQueue:
    """审核队列模型"""

    @staticmethod
    def get_pending_cards(limit: int = 50) -> list:
        """获取待审核的角色卡列表"""
        with get_db() as db:
            rows = db.execute(
                """
                SELECT rc.*, u.username as owner_username
                FROM role_cards rc
                LEFT JOIN users u ON rc.user_id = u.id
                WHERE rc.status = 'pending'
                ORDER BY rc.created_at ASC
                LIMIT ?
                """,
                (limit,)
            ).fetchall()
        return [RoleCard.row_to_card(row) for row in rows]

    @staticmethod
    def get_pending_cards_paginated(page: int = 1, per_page: int = 10) -> tuple:
        """获取待审核的角色卡列表（分页）
        
        Returns:
            (items, total_count)
        """
        offset = (page - 1) * per_page
        with get_db() as db:
            # 获取总数
            total_row = db.execute(
                "SELECT COUNT(*) as count FROM role_cards WHERE status = 'pending'"
            ).fetchone()
            total = total_row["count"]
            
            # 获取分页数据
            rows = db.execute(
                """
                SELECT rc.*, u.username as owner_username
                FROM role_cards rc
                LEFT JOIN users u ON rc.user_id = u.id
                WHERE rc.status = 'pending'
                ORDER BY rc.created_at ASC
                LIMIT ? OFFSET ?
                """,
                (per_page, offset)
            ).fetchall()
        return [RoleCard.row_to_card(row) for row in rows], total

    @staticmethod
    def get_pending_comments_paginated(page: int = 1, per_page: int = 10) -> tuple:
        """获取待审核的评论列表（分页）
        
        Returns:
            (items, total_count)
        """
        offset = (page - 1) * per_page
        with get_db() as db:
            # 获取总数
            total_row = db.execute(
                "SELECT COUNT(*) as count FROM comments WHERE status = 'pending'"
            ).fetchone()
            total = total_row["count"]
            
            # 获取分页数据
            rows = db.execute(
                """
                SELECT c.*, u.username, u.display_name, rc.name as card_name, rc.slug as card_slug
                FROM comments c
                JOIN users u ON c.user_id = u.id
                JOIN role_cards rc ON c.card_id = rc.id
                WHERE c.status = 'pending'
                ORDER BY c.created_at ASC
                LIMIT ? OFFSET ?
                """,
                (per_page, offset)
            ).fetchall()
        return [dict(row) for row in rows], total

    @staticmethod
    def get_pending_comments(limit: int = 50) -> list:
        """获取待审核的评论列表"""
        with get_db() as db:
            rows = db.execute(
                """
                SELECT c.*, u.username, u.display_name, rc.name as card_name, rc.slug as card_slug
                FROM comments c
                JOIN users u ON c.user_id = u.id
                JOIN role_cards rc ON c.card_id = rc.id
                WHERE c.status = 'pending'
                ORDER BY c.created_at ASC
                LIMIT ?
                """,
                (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def approve_card(card_id: int, reviewer_id: int, result: str = None) -> None:
        """批准角色卡"""
        now = datetime.now().isoformat(timespec="seconds")
        with get_db() as db:
            db.execute(
                """
                UPDATE role_cards 
                SET status = 'approved', reviewed_by = ?, reviewed_at = ?, review_result = ?
                WHERE id = ?
                """,
                (reviewer_id, now, result, card_id)
            )
            db.commit()

    @staticmethod
    def reject_card(card_id: int, reviewer_id: int, result: str = None) -> None:
        """拒绝角色卡"""
        now = datetime.now().isoformat(timespec="seconds")
        with get_db() as db:
            db.execute(
                """
                UPDATE role_cards 
                SET status = 'rejected', reviewed_by = ?, reviewed_at = ?, review_result = ?
                WHERE id = ?
                """,
                (reviewer_id, now, result, card_id)
            )
            db.commit()

    @staticmethod
    def approve_comment(comment_id: int, reviewer_id: int, result: str = None) -> None:
        """批准评论"""
        now = datetime.now().isoformat(timespec="seconds")
        with get_db() as db:
            db.execute(
                """
                UPDATE comments 
                SET status = 'approved', reviewed_by = ?, reviewed_at = ?, review_result = ?
                WHERE id = ?
                """,
                (reviewer_id, now, result, comment_id)
            )
            db.commit()

    @staticmethod
    def reject_comment(comment_id: int, reviewer_id: int, result: str = None) -> None:
        """拒绝评论"""
        now = datetime.now().isoformat(timespec="seconds")
        with get_db() as db:
            db.execute(
                """
                UPDATE comments 
                SET status = 'rejected', reviewed_by = ?, reviewed_at = ?, review_result = ?
                WHERE id = ?
                """,
                (reviewer_id, now, result, comment_id)
            )
            db.commit()

    @staticmethod
    def get_stats() -> dict:
        """获取审核统计信息"""
        with get_db() as db:
            card_stats = db.execute(
                """
                SELECT 
                    COUNT(CASE WHEN status = 'pending' THEN 1 END) as pending_cards,
                    COUNT(CASE WHEN status = 'approved' THEN 1 END) as approved_cards,
                    COUNT(CASE WHEN status = 'rejected' THEN 1 END) as rejected_cards
                FROM role_cards
                """
            ).fetchone()

            comment_stats = db.execute(
                """
                SELECT 
                    COUNT(CASE WHEN status = 'pending' THEN 1 END) as pending_comments,
                    COUNT(CASE WHEN status = 'approved' THEN 1 END) as approved_comments,
                    COUNT(CASE WHEN status = 'rejected' THEN 1 END) as rejected_comments
                FROM comments
                """
            ).fetchone()

        return {
            "pending_cards": card_stats["pending_cards"],
            "approved_cards": card_stats["approved_cards"],
            "rejected_cards": card_stats["rejected_cards"],
            "pending_comments": comment_stats["pending_comments"],
            "approved_comments": comment_stats["approved_comments"],
            "rejected_comments": comment_stats["rejected_comments"],
        }


class AIReviewConfig:
    """AI审核配置模型"""

    @staticmethod
    def get() -> dict:
        """获取AI审核配置"""
        with get_db() as db:
            row = db.execute("SELECT * FROM ai_review_config LIMIT 1").fetchone()
            if not row:
                # 初始化默认配置
                now = datetime.now().isoformat(timespec="seconds")
                db.execute(
                    """
                    INSERT INTO ai_review_config (api_key, api_url, model, enabled, updated_at)
                    VALUES ('', '', '', 0, ?)
                    """,
                    (now,)
                )
                db.commit()
                row = db.execute("SELECT * FROM ai_review_config LIMIT 1").fetchone()
        return dict(row)

    @staticmethod
    def update(api_key: str, api_url: str, model: str, enabled: bool) -> None:
        """更新AI审核配置"""
        now = datetime.now().isoformat(timespec="seconds")
        with get_db() as db:
            db.execute(
                """
                UPDATE ai_review_config
                SET api_key = ?, api_url = ?, model = ?, enabled = ?, updated_at = ?
                WHERE id = 1
                """,
                (api_key, api_url, model, 1 if enabled else 0, now)
            )
            db.commit()


class EmailConfig:
    """邮件配置模型"""

    @staticmethod
    def get() -> dict:
        """获取邮件配置"""
        try:
            with get_db() as db:
                row = db.execute("SELECT * FROM email_config LIMIT 1").fetchone()
                if not row:
                    now = datetime.now().isoformat(timespec="seconds")
                    db.execute(
                        """
                        INSERT INTO email_config (smtp_server, smtp_port, smtp_username, smtp_password, sender_email, sender_name, use_tls, enabled, updated_at)
                        VALUES ('', 587, '', '', '', '', 1, 0, ?)
                        """,
                        (now,)
                    )
                    db.commit()
                    row = db.execute("SELECT * FROM email_config LIMIT 1").fetchone()
            return dict(row) if row else {
                "smtp_server": "", "smtp_port": 587, "smtp_username": "",
                "smtp_password": "", "sender_email": "", "sender_name": "",
                "use_tls": 1, "enabled": 0
            }
        except sqlite3.OperationalError:
            # 表不存在时返回默认配置
            return {
                "smtp_server": "", "smtp_port": 587, "smtp_username": "",
                "smtp_password": "", "sender_email": "", "sender_name": "",
                "use_tls": 1, "enabled": 0
            }

    @staticmethod
    def update(smtp_server: str, smtp_port: int, smtp_username: str, smtp_password: str,
               sender_email: str, sender_name: str, use_tls: bool, enabled: bool) -> None:
        """更新邮件配置"""
        now = datetime.now().isoformat(timespec="seconds")
        with get_db() as db:
            db.execute(
                """
                UPDATE email_config
                SET smtp_server = ?, smtp_port = ?, smtp_username = ?, smtp_password = ?,
                    sender_email = ?, sender_name = ?, use_tls = ?, enabled = ?, updated_at = ?
                WHERE id = 1
                """,
                (smtp_server, smtp_port, smtp_username, smtp_password,
                 sender_email, sender_name, 1 if use_tls else 0, 1 if enabled else 0, now)
            )
            db.commit()


class VerificationCode:
    """验证码模型（code字段存储HMAC hash，不存明文）"""

    @staticmethod
    def create(email: str, code_hash: str, ip_address: str = "", expires_minutes: int = 10) -> None:
        """创建验证码记录（code_hash为HMAC hash值）"""
        from datetime import timedelta
        now = datetime.now()
        created_at = now.isoformat(timespec="seconds")
        expires_at = (now + timedelta(minutes=expires_minutes)).isoformat(timespec="seconds")
        try:
            with get_db() as db:
                db.execute(
                    """
                    INSERT INTO verification_codes (email, code, ip_address, created_at, expires_at, used)
                    VALUES (?, ?, ?, ?, ?, 0)
                    """,
                    (email, code_hash, ip_address, created_at, expires_at)
                )
                db.commit()
        except sqlite3.OperationalError:
            pass

    @staticmethod
    def verify(email: str, code_hash: str) -> bool:
        """验证验证码hash是否正确且未过期未使用（含尝试次数限制）"""
        now = datetime.now().isoformat(timespec="seconds")
        try:
            with get_db() as db:
                # 检查最近10分钟内该邮箱的失败尝试次数
                attempt_cutoff = (datetime.now() - timedelta(minutes=10)).isoformat(timespec="seconds")
                attempt_row = db.execute(
                    """
                    SELECT COUNT(*) as cnt FROM verification_codes
                    WHERE email = ? AND used = 0 AND created_at > ? AND code != ?
                    """,
                    (email, attempt_cutoff, code_hash)
                ).fetchone()
                # 最多允许5次验证尝试
                if attempt_row and attempt_row["cnt"] >= 5:
                    return False

                row = db.execute(
                    """
                    SELECT id FROM verification_codes
                    WHERE email = ? AND code = ? AND used = 0 AND expires_at > ?
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (email, code_hash, now)
                ).fetchone()
                if row:
                    db.execute("UPDATE verification_codes SET used = 1 WHERE id = ?", (row["id"],))
                    db.commit()
                    return True
        except sqlite3.OperationalError:
            pass
        return False

    @staticmethod
    def count_recent_by_ip(ip_address: str, minutes: int = 1) -> int:
        """统计指定IP在最近几分钟内发送的验证码数量"""
        from datetime import timedelta
        cutoff = (datetime.now() - timedelta(minutes=minutes)).isoformat(timespec="seconds")
        try:
            with get_db() as db:
                row = db.execute(
                    "SELECT COUNT(*) as cnt FROM verification_codes WHERE ip_address = ? AND created_at > ?",
                    (ip_address, cutoff)
                ).fetchone()
            return row["cnt"] if row else 0
        except sqlite3.OperationalError:
            return 0

    @staticmethod
    def cleanup_expired() -> None:
        """清理过期的验证码记录"""
        now = datetime.now().isoformat(timespec="seconds")
        try:
            with get_db() as db:
                db.execute("DELETE FROM verification_codes WHERE expires_at < ?", (now,))
                db.commit()
        except sqlite3.OperationalError:
            pass


class CardRelation:
    """角色卡关联模型"""

    @staticmethod
    def add(card_id: int, related_card_id: int, relation_type: str = "related") -> None:
        """添加角色卡关联（双向关联）"""
        if card_id == related_card_id:
            return
        now = datetime.now().isoformat(timespec="seconds")
        with get_db() as db:
            # 建立双向关联：A->B 和 B->A
            db.execute(
                "INSERT OR IGNORE INTO card_relations (card_id, related_card_id, relation_type, created_at) VALUES (?, ?, ?, ?)",
                (card_id, related_card_id, relation_type, now)
            )
            db.execute(
                "INSERT OR IGNORE INTO card_relations (card_id, related_card_id, relation_type, created_at) VALUES (?, ?, ?, ?)",
                (related_card_id, card_id, relation_type, now)
            )
            db.commit()

    @staticmethod
    def remove(card_id: int, related_card_id: int) -> None:
        """移除角色卡关联（双向删除）"""
        with get_db() as db:
            # 删除双向关联：A->B 和 B->A
            db.execute(
                "DELETE FROM card_relations WHERE card_id = ? AND related_card_id = ?",
                (card_id, related_card_id)
            )
            db.execute(
                "DELETE FROM card_relations WHERE card_id = ? AND related_card_id = ?",
                (related_card_id, card_id)
            )
            db.commit()

    @staticmethod
    def get_related_cards(card_id: int) -> list:
        """获取角色卡关联的其他角色卡"""
        with get_db() as db:
            rows = db.execute(
                """
                SELECT rc.* FROM role_cards rc
                JOIN card_relations cr ON rc.id = cr.related_card_id
                WHERE cr.card_id = ? AND rc.visibility = 'public' AND rc.status = 'approved'
                ORDER BY cr.created_at DESC
                """,
                (card_id,)
            ).fetchall()
        return [RoleCard.row_to_card(row) for row in rows]

    @staticmethod
    def is_related(card_id: int, related_card_id: int) -> bool:
        """检查两个角色卡是否已关联"""
        with get_db() as db:
            row = db.execute(
                "SELECT id FROM card_relations WHERE card_id = ? AND related_card_id = ?",
                (card_id, related_card_id)
            ).fetchone()
        return row is not None

    @staticmethod
    def get_linked_by_cards(card_id: int) -> list:
        """获取关联了当前角色卡的其他角色卡（反向关联）"""
        with get_db() as db:
            rows = db.execute(
                """
                SELECT rc.* FROM role_cards rc
                JOIN card_relations cr ON rc.id = cr.card_id
                WHERE cr.related_card_id = ? AND rc.visibility = 'public' AND rc.status = 'approved'
                ORDER BY cr.created_at DESC
                """,
                (card_id,)
            ).fetchall()
        return [RoleCard.row_to_card(row) for row in rows]


class UserFollow:
    """用户关注模型"""

    @staticmethod
    def follow(follower_id: int, following_id: int) -> None:
        """关注用户"""
        if follower_id == following_id:
            return
        now = datetime.now().isoformat(timespec="seconds")
        with get_db() as db:
            db.execute(
                "INSERT OR IGNORE INTO user_follows (follower_id, following_id, created_at) VALUES (?, ?, ?)",
                (follower_id, following_id, now)
            )
            db.commit()

    @staticmethod
    def unfollow(follower_id: int, following_id: int) -> None:
        """取消关注"""
        with get_db() as db:
            db.execute(
                "DELETE FROM user_follows WHERE follower_id = ? AND following_id = ?",
                (follower_id, following_id)
            )
            db.commit()

    @staticmethod
    def is_following(follower_id: int, following_id: int) -> bool:
        """检查是否已关注"""
        with get_db() as db:
            row = db.execute(
                "SELECT id FROM user_follows WHERE follower_id = ? AND following_id = ?",
                (follower_id, following_id)
            ).fetchone()
        return row is not None

    @staticmethod
    def get_followers(user_id: int) -> list:
        """获取粉丝列表"""
        with get_db() as db:
            rows = db.execute(
                """
                SELECT u.* FROM users u
                JOIN user_follows uf ON u.id = uf.follower_id
                WHERE uf.following_id = ?
                ORDER BY uf.created_at DESC
                """,
                (user_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def get_following(user_id: int) -> list:
        """获取关注列表"""
        with get_db() as db:
            rows = db.execute(
                """
                SELECT u.* FROM users u
                JOIN user_follows uf ON u.id = uf.following_id
                WHERE uf.follower_id = ?
                ORDER BY uf.created_at DESC
                """,
                (user_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def get_follower_count(user_id: int) -> int:
        """获取粉丝数"""
        with get_db() as db:
            row = db.execute(
                "SELECT COUNT(*) as cnt FROM user_follows WHERE following_id = ?",
                (user_id,)
            ).fetchone()
        return row["cnt"] if row else 0

    @staticmethod
    def get_following_count(user_id: int) -> int:
        """获取关注数"""
        with get_db() as db:
            row = db.execute(
                "SELECT COUNT(*) as cnt FROM user_follows WHERE follower_id = ?",
                (user_id,)
            ).fetchone()
        return row["cnt"] if row else 0
