"""
用户路由 - 包含用户注册、登录、个人资料等
"""
from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for

from ..auth import AuthService, get_current_user, login_required
from ..models import RoleCard, User

bp = Blueprint('users', __name__)


@bp.route("/register", methods=["GET", "POST"])
def register():
    """用户注册"""
    if request.method == "GET":
        return render_template("register.html")

    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    confirm = request.form.get("confirm") or ""

    if not username or not password:
        flash("用户名和密码不能为空", "error")
        return redirect(url_for("users.register"))

    if password != confirm:
        flash("两次输入的密码不一致", "error")
        return redirect(url_for("users.register"))

    user, error = AuthService.register(username, password)
    if error:
        flash(error, "error")
        return redirect(url_for("users.register"))

    session["user_id"] = user["id"]
    flash("注册成功，欢迎加入！")
    return redirect(url_for("main.index"))


@bp.route("/login", methods=["GET", "POST"])
def login():
    """用户登录"""
    if request.method == "GET":
        return render_template("login.html")

    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""

    if not username or not password:
        flash("请输入用户名和密码", "error")
        return redirect(url_for("users.login"))

    user, error = AuthService.login(username, password)
    if error:
        flash(error, "error")
        return redirect(url_for("users.login"))

    session["user_id"] = user["id"]
    flash(f"欢迎回来，{user['display_name'] or user['username']}！")
    return redirect(url_for("main.index"))


@bp.route("/logout", methods=["POST"])
def logout():
    """用户退出"""
    AuthService.logout()
    flash("已退出登录")
    return redirect(url_for("main.index"))


@bp.route("/user/<username>")
def user_profile(username):
    """用户个人主页"""
    user = User.get_by_username(username)
    if not user:
        abort(404)

    # 自己的主页显示所有角色卡，别人的主页只显示公开的
    is_self = session.get("user_id") == user["id"]
    cards = RoleCard.get_by_user(user["id"], include_private=is_self)

    return render_template("user_profile.html", profile_user=user, cards=cards, is_self=is_self)


@bp.route("/user/<username>/regen-token", methods=["POST"])
@login_required
def regen_api_token(username):
    """重新生成 API Token"""
    current_user = get_current_user()
    if not current_user or current_user["username"] != username:
        abort(403)

    AuthService.regenerate_api_token(current_user["id"])
    flash("API Token 已重新生成", "success")
    return redirect(url_for("users.user_profile", username=username))


@bp.route("/user/<username>/edit", methods=["GET", "POST"])
@login_required
def edit_profile(username):
    """编辑个人资料"""
    current_user = get_current_user()
    if not current_user or current_user["username"] != username:
        abort(403)

    if request.method == "POST":
        display_name = (request.form.get("display_name") or "").strip()
        bio = (request.form.get("bio") or "").strip()

        # 处理头像上传
        avatar_path = current_user["avatar_path"]
        avatar_file = request.files.get("avatar")
        if avatar_file and avatar_file.filename:
            try:
                from ..utils import save_avatar
                avatar_path = save_avatar(avatar_file)
            except ValueError as e:
                flash(str(e), "error")
                return redirect(url_for("users.edit_profile", username=username))

        User.update_profile(current_user["id"], display_name, bio, avatar_path)
        flash("资料已更新")
        return redirect(url_for("users.user_profile", username=username))

    return render_template("edit_profile.html", user=current_user)
