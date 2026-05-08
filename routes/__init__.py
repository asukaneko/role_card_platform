"""
路由包 - 包含所有蓝图路由
"""
from .main import bp as main_bp
from .cards import bp as cards_bp
from .users import bp as users_bp
from .admin import bp as admin_bp
from .api import bp as api_bp

__all__ = ['main_bp', 'cards_bp', 'users_bp', 'admin_bp', 'api_bp']
