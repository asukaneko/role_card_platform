function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem("role-card-theme", theme);
    const icon = document.querySelector("[data-theme-icon]");
    if (icon) {
        icon.textContent = theme === "dark" ? "☀" : "☾";
    }
}

function updateBatchBar() {
    const checkboxes = document.querySelectorAll(".card-checkbox");
    const checked = document.querySelectorAll(".card-checkbox:checked");
    const countEl = document.getElementById("selected-count");
    const selectAll = document.getElementById("select-all");
    if (countEl) {
        countEl.textContent = checked.length;
    }
    if (selectAll) {
        selectAll.checked = checkboxes.length > 0 && checked.length === checkboxes.length;
        selectAll.indeterminate = checked.length > 0 && checked.length < checkboxes.length;
    }
}

document.addEventListener("DOMContentLoaded", () => {
    const current = document.documentElement.dataset.theme || "light";
    applyTheme(current);
    updateBatchBar();
});

document.addEventListener("click", async (event) => {
    const themeButton = event.target.closest("[data-theme-toggle]");
    if (themeButton) {
        event.preventDefault();
        const current = document.documentElement.dataset.theme || "light";
        applyTheme(current === "dark" ? "light" : "dark");
        return;
    }

    const likeButton = event.target.closest(".like-button");
    if (likeButton) {
        event.preventDefault();
        const url = likeButton.dataset.likeUrl;
        if (!url) return;
        likeButton.disabled = true;
        try {
            const response = await fetch(url, { method: "POST" });
            const data = await response.json();
            
            if (!response.ok) {
                alert(data.error || "操作失败");
                likeButton.disabled = false;
                return;
            }
            
            const count = document.getElementById("like-count");
            if (count) {
                count.innerHTML = `${data.likes}<small>喜欢</small>`;
            }
            likeButton.textContent = "已喜欢";
        } catch (error) {
            likeButton.textContent = "操作失败";
            likeButton.disabled = false;
        }
        return;
    }

    const favoriteButton = event.target.closest(".favorite-button, .unfavorite-btn");
    if (favoriteButton) {
        event.preventDefault();
        const url = favoriteButton.dataset.favoriteUrl;
        if (!url) return;
        favoriteButton.disabled = true;
        try {
            const response = await fetch(url, { method: "POST" });
            const data = await response.json();

            if (!response.ok) {
                alert(data.error || "操作失败");
                favoriteButton.disabled = false;
                return;
            }

            if (favoriteButton.classList.contains("unfavorite-btn")) {
                const card = favoriteButton.closest(".role-card");
                if (card) card.remove();
                const toolbar = document.querySelector(".toolbar strong");
                if (toolbar) {
                    const count = parseInt(toolbar.textContent) - 1;
                    toolbar.textContent = count;
                }
            } else if (data.favorited) {
                favoriteButton.textContent = "★ 已收藏";
                favoriteButton.classList.add("favorited");
            } else {
                favoriteButton.textContent = "☆ 收藏";
                favoriteButton.classList.remove("favorited");
            }
            favoriteButton.disabled = false;
        } catch (error) {
            favoriteButton.textContent = "操作失败";
            favoriteButton.disabled = false;
        }
        return;
    }

    const confirmForm = event.target.closest("form[data-confirm]");
    if (confirmForm && event.target.closest("button")) {
        const message = confirmForm.dataset.confirm || "确定执行这个操作吗？";
        if (!window.confirm(message)) {
            event.preventDefault();
        }
    }

    // 整张角色卡点击跳转详情页（排除内部链接和按钮）
    const roleCard = event.target.closest(".role-card[data-card-url]");
    if (roleCard) {
        if (event.target.closest("a, button, form, .card-owner-actions")) return;
        window.location.href = roleCard.dataset.cardUrl;
    }
});

document.addEventListener("change", (event) => {
    if (event.target.id === "select-all") {
        const checked = event.target.checked;
        document.querySelectorAll(".card-checkbox").forEach(cb => {
            cb.checked = checked;
        });
        updateBatchBar();
        return;
    }
    if (event.target.classList.contains("card-checkbox")) {
        updateBatchBar();
    }
});

document.addEventListener("click", (event) => {
    const batchBtn = event.target.closest(".batch-btn");
    if (!batchBtn) return;
    event.preventDefault();

    const checked = document.querySelectorAll(".card-checkbox:checked");
    if (checked.length === 0) {
        alert("请先选择角色卡");
        return;
    }

    const action = batchBtn.dataset.action;
    const actionLabel = { hide: "隐藏", publish: "公开", delete: "删除" }[action] || action;
    if (action === "delete") {
        if (!window.confirm(`确定批量删除 ${checked.length} 张角色卡吗？此操作不可撤销。`)) {
            return;
        }
    }

    const ids = Array.from(checked).map(cb => cb.value).join(",");
    const idsInput = document.getElementById("batch-ids");
    const actionInput = document.getElementById("batch-action");
    if (idsInput) idsInput.value = ids;
    if (actionInput) actionInput.value = action;

    const form = document.querySelector(".batch-form");
    if (form) form.submit();
});
