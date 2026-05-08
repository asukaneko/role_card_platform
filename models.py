"""
数据库模型 - 包含所有数据库操作和模型函数
"""
import json
import sqlite3
from datetime import datetime
from typing import Optional

from flask import abort, session

from config import DATA_DIR, DB_PATH


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
                FOREIGN KEY (user_id) REFERENCES users(id)
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
        ]
        for col, sql in migrations:
            if col not in columns:
                db.execute(sql)

        # 用户表迁移
        user_columns = {row["name"] for row in db.execute("PRAGMA table_info(users)").fetchall()}
        if "api_token" not in user_columns:
            db.execute("ALTER TABLE users ADD COLUMN api_token TEXT NOT NULL DEFAULT ''")
        if "avatar_path" not in user_columns:
            db.execute("ALTER TABLE users ADD COLUMN avatar_path TEXT DEFAULT ''")

        # 评论表
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                card_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (card_id) REFERENCES role_cards(id) ON DELETE CASCADE,
                FOREIGN KEY (user_id) REFERENCES users(id)
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
        with get_db() as db:
            db.execute(
                "INSERT INTO users (username, password_hash, display_name, bio, api_token, created_at) VALUES (?, ?, ?, '', ?, ?)",
                (username, password_hash, display_name or username, api_token, now),
            )
            db.commit()
            row = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return dict(row)

    @staticmethod
    def update_api_token(user_id: int, token: str) -> None:
        """更新用户 API Token"""
        with get_db() as db:
            db.execute("UPDATE users SET api_token = ? WHERE id = ?", (token, user_id))
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
    def get_by_id(card_id: int) -> Optional[dict]:
        """通过 ID 获取角色卡"""
        with get_db() as db:
            row = db.execute("SELECT * FROM role_cards WHERE id = ?", (card_id,)).fetchone()
        return RoleCard.row_to_card(row) if row else None

    @staticmethod
    def get_by_slug(slug: str) -> Optional[dict]:
        """通过 slug 获取角色卡"""
        with get_db() as db:
            row = db.execute("SELECT * FROM role_cards WHERE slug = ?", (slug,)).fetchone()
        return RoleCard.row_to_card(row) if row else None

    @staticmethod
    def get_or_404(identifier):
        """获取角色卡，不存在则返回 404"""
        with get_db() as db:
            if str(identifier).isdigit():
                row = db.execute("SELECT * FROM role_cards WHERE id = ?", (identifier,)).fetchone()
            else:
                row = db.execute("SELECT * FROM role_cards WHERE slug = ?", (identifier,)).fetchone()
        if not row:
            abort(404)
        return RoleCard.row_to_card(row)

    @staticmethod
    def create(card_data: dict, avatar_path: str = "", user_id: int = None) -> dict:
        """创建新角色卡"""
        now = datetime.now().isoformat(timespec="seconds")
        with get_db() as db:
            from utils import unique_slug
            slug = unique_slug(db, card_data["name"])
            db.execute(
                """
                INSERT INTO role_cards (
                    name, slug, avatar_path, description, personality, scenario,
                    first_message, system_prompt, tags_json, creator, visibility,
                    source_format, raw_json, user_id, created_at, updated_at,
                    basic_info, example_dialogues, response_format, rules_json, state_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
    def search(query: str = "", tag: str = "", sort: str = "latest", visibility: str = None) -> list:
        """搜索角色卡"""
        where = []
        params = []

        if visibility:
            where.append(f"visibility = '{visibility}'")
        else:
            where.append("visibility = 'public'")

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
    def get_by_user(user_id: int, include_private: bool = False) -> list:
        """获取用户的角色卡"""
        with get_db() as db:
            if include_private:
                rows = db.execute(
                    "SELECT * FROM role_cards WHERE user_id = ? ORDER BY created_at DESC",
                    (user_id,),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM role_cards WHERE user_id = ? AND visibility = 'public' ORDER BY created_at DESC",
                    (user_id,),
                ).fetchall()
        return [RoleCard.row_to_card(row) for row in rows]

    @staticmethod
    def get_all_tags() -> list:
        """获取所有标签"""
        from utils import normalize_tags
        with get_db() as db:
            rows = db.execute("SELECT tags_json FROM role_cards WHERE visibility = 'public'").fetchall()

        all_tags = []
        for row in rows:
            for item in normalize_tags(json.loads(row["tags_json"] or "[]")):
                if item not in all_tags:
                    all_tags.append(item)
        return all_tags


class Comment:
    """评论模型"""

    @staticmethod
    def get_by_card(card_id: int) -> list:
        """获取角色卡的所有评论"""
        with get_db() as db:
            rows = db.execute(
                """
                SELECT c.*, u.username, u.display_name
                FROM comments c
                JOIN users u ON c.user_id = u.id
                WHERE c.card_id = ? ORDER BY c.created_at ASC
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
        """创建评论"""
        now = datetime.now().isoformat(timespec="seconds")
        with get_db() as db:
            db.execute(
                "INSERT INTO comments (card_id, user_id, content, created_at) VALUES (?, ?, ?, ?)",
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
