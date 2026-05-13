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

    const followButton = event.target.closest(".follow-button, .follow-profile-btn");
    if (followButton) {
        event.preventDefault();
        const url = followButton.dataset.followUrl;
        if (!url) return;
        followButton.disabled = true;
        try {
            const response = await fetch(url, { method: "POST" });
            const data = await response.json();
            if (!response.ok) {
                alert(data.error || "操作失败");
                followButton.disabled = false;
                return;
            }
            if (data.following) {
                followButton.textContent = "已关注";
                followButton.classList.add("following");
                followButton.dataset.followUrl = followButton.dataset.followUrl.replace("/follow", "/unfollow");
            } else {
                followButton.textContent = followButton.classList.contains("follow-profile-btn") ? "关注" : "关注作者";
                followButton.classList.remove("following");
                followButton.dataset.followUrl = followButton.dataset.followUrl.replace("/unfollow", "/follow");
            }
            followButton.disabled = false;
        } catch (error) {
            followButton.textContent = "操作失败";
            followButton.disabled = false;
        }
        return;
    }

    const confirmForm = event.target.closest("form[data-confirm]");
    if (confirmForm && event.target.closest("button")) {
        event.preventDefault();
        const message = confirmForm.dataset.confirm || "确定执行这个操作吗？";
        
        // 判断是否为危险操作
        const isDanger = confirmForm.querySelector(".danger-btn") || 
                         message.includes("删除") || 
                         message.includes("不可撤销");
        
        showConfirmModal({
            title: isDanger ? "危险操作" : "确认操作",
            message: message,
            type: isDanger ? "danger" : "info",
            onConfirm: () => {
                confirmForm.submit();
            }
        });
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
        showConfirmModal({
            title: "批量删除",
            message: `确定批量删除 ${checked.length} 张角色卡吗？此操作不可撤销。`,
            type: "danger",
            onConfirm: () => {
                const ids = Array.from(checked).map(cb => cb.value).join(",");
                const idsInput = document.getElementById("batch-ids");
                const actionInput = document.getElementById("batch-action");
                if (idsInput) idsInput.value = ids;
                if (actionInput) actionInput.value = action;
                const form = document.querySelector(".batch-form");
                if (form) form.submit();
            }
        });
        return;
    }

    const ids = Array.from(checked).map(cb => cb.value).join(",");
    const idsInput = document.getElementById("batch-ids");
    const actionInput = document.getElementById("batch-action");
    if (idsInput) idsInput.value = ids;
    if (actionInput) actionInput.value = action;

    const form = document.querySelector(".batch-form");
    if (form) form.submit();
});

// 角色卡关联搜索功能
(function initLinkSearch() {
    const searchInput = document.getElementById("link-search-input");
    const searchBtn = document.getElementById("link-search-btn");
    const resultsBox = document.getElementById("link-search-results");
    const selectedList = document.getElementById("link-selected-list");
    if (!searchInput || !resultsBox) return;

    const cardId = searchInput.dataset.cardId;
    let searchTimeout = null;
    let selectedCards = [];

    function renderSelected() {
        if (!selectedList) return;
        selectedList.innerHTML = "";
        selectedCards.forEach(c => {
            const item = document.createElement("div");
            item.className = "link-selected-item";
            item.innerHTML = `<span>${c.name}</span><button type="button" data-id="${c.id}">×</button>`;
            selectedList.appendChild(item);
        });
    }

    async function addRelation(relatedCardId) {
        const formData = new FormData();
        const csrfToken = window.csrfToken || "";
        if (csrfToken) formData.append("csrf_token", csrfToken);
        formData.append("related_card_id", relatedCardId);
        try {
            const resp = await fetch(`/card/${cardId}/relate`, { method: "POST", body: formData });
            const data = await resp.json();
            if (data.success) {
                window.location.reload();
            } else {
                alert(data.error || "关联失败");
            }
        } catch (e) {
            alert("关联失败");
        }
    }

    searchInput.addEventListener("input", () => {
        const q = searchInput.value.trim();
        if (searchTimeout) clearTimeout(searchTimeout);
        if (!q) {
            resultsBox.classList.remove("active");
            resultsBox.innerHTML = "";
            return;
        }
        searchTimeout = setTimeout(async () => {
            try {
                const resp = await fetch(`/api/cards/search?q=${encodeURIComponent(q)}&exclude_id=${cardId}`);
                const data = await resp.json();
                resultsBox.innerHTML = "";
                if (data.cards && data.cards.length > 0) {
                    data.cards.forEach(c => {
                        const div = document.createElement("div");
                        div.className = "link-search-result-item";
                        div.dataset.id = c.id;
                        div.dataset.name = c.name;
                        const avatarHtml = c.avatar_path
                            ? `<img src="/assets/uploads/avatars/${c.avatar_path.split('/').pop()}" alt="${c.name}">`
                            : `<span>${c.name[0]}</span>`;
                        div.innerHTML = `
                            <div class="link-search-result-avatar">${avatarHtml}</div>
                            <div class="link-search-result-info">
                                <strong>${c.name}</strong>
                                <span>${c.description || ''}</span>
                            </div>
                        `;
                        div.addEventListener("click", () => {
                            addRelation(c.id);
                            searchInput.value = "";
                            resultsBox.classList.remove("active");
                            resultsBox.innerHTML = "";
                        });
                        resultsBox.appendChild(div);
                    });
                    resultsBox.classList.add("active");
                } else {
                    resultsBox.classList.remove("active");
                }
            } catch (e) {
                resultsBox.classList.remove("active");
            }
        }, 200);
    });

    searchBtn && searchBtn.addEventListener("click", () => {
        searchInput.focus();
    });

    // 点击外部关闭搜索结果
    document.addEventListener("click", (e) => {
        if (!e.target.closest(".link-card-search")) {
            resultsBox.classList.remove("active");
        }
    });

    // 移除已选
    selectedList && selectedList.addEventListener("click", (e) => {
        const btn = e.target.closest("button[data-id]");
        if (btn) {
            const id = parseInt(btn.dataset.id);
            selectedCards = selectedCards.filter(c => c.id !== id);
            renderSelected();
        }
    });
})();

// 确认弹窗功能
(function initConfirmModal() {
    const overlay = document.getElementById("confirm-modal");
    const iconEl = document.getElementById("confirm-icon");
    const titleEl = document.getElementById("confirm-title");
    const messageEl = document.getElementById("confirm-message");
    const headerEl = document.getElementById("confirm-header");
    const cancelBtn = document.getElementById("confirm-cancel-btn");
    const submitBtn = document.getElementById("confirm-submit-btn");

    let currentCallback = null;
    let currentConfirmBtn = null;

    // 图标 SVG 映射
    const icons = {
        danger: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
        warning: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>',
        success: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5"/></svg>',
        info: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>'
    };

    function openModal(options) {
        const { title, message, type = "info", confirmText = "确认", cancelText = "取消", onConfirm } = options;
        currentCallback = onConfirm;

        // 设置内容
        titleEl.textContent = title;
        messageEl.textContent = message;
        iconEl.innerHTML = icons[type] || icons.info;
        iconEl.className = "confirm-modal-icon " + type;
        headerEl.className = "confirm-modal-header" + (type === "danger" ? " danger" : "");
        
        // 设置按钮文字和样式
        cancelBtn.textContent = cancelText;
        submitBtn.textContent = confirmText;
        submitBtn.className = "confirm-btn confirm " + type;

        // 显示弹窗
        overlay.classList.add("active");
        document.body.style.overflow = "hidden";
    }

    function closeModal() {
        overlay.classList.remove("active");
        document.body.style.overflow = "";
        currentCallback = null;
    }

    // 取消按钮
    cancelBtn.addEventListener("click", closeModal);

    // 确认按钮
    submitBtn.addEventListener("click", () => {
        if (currentCallback) {
            currentCallback();
        }
        closeModal();
    });

    // 点击背景关闭
    overlay.addEventListener("click", (e) => {
        if (e.target === overlay) {
            closeModal();
        }
    });

    // ESC 键关闭
    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && overlay.classList.contains("active")) {
            closeModal();
        }
    });

    // 暴露全局函数
    window.showConfirmModal = openModal;
})();

// 分享弹窗功能
(function initShareModal() {
    const shareBtn = document.getElementById("share-card-btn");
    const shareModal = document.getElementById("share-modal");
    if (!shareBtn || !shareModal) return;

    const shareOverlay = shareModal.querySelector(".share-modal-overlay");
    const shareClose = document.getElementById("share-modal-close");
    const shareImagePreview = document.getElementById("share-image-preview");
    const shareOpenImage = document.getElementById("share-open-image");
    const shareCopyLink = document.getElementById("share-copy-link");
    const shareDownloadImg = document.getElementById("share-download-img");

    const shareUrl = shareBtn.dataset.shareUrl;
    const cardUrl = shareBtn.dataset.cardUrl;

    function openModal() {
        const freshShareUrl = `${shareUrl}${shareUrl.includes("?") ? "&" : "?"}t=${Date.now()}`;
        shareImagePreview.src = freshShareUrl;
        shareOpenImage.href = freshShareUrl;
        shareModal.classList.add("active");
        document.body.style.overflow = "hidden";
    }

    function closeModal() {
        shareModal.classList.remove("active");
        document.body.style.overflow = "";
    }

    shareBtn.addEventListener("click", (e) => {
        e.preventDefault();
        openModal();
    });

    shareClose && shareClose.addEventListener("click", closeModal);
    shareOverlay && shareOverlay.addEventListener("click", closeModal);

    // ESC关闭
    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && shareModal.classList.contains("active")) {
            closeModal();
        }
    });

    // 复制链接
    shareCopyLink && shareCopyLink.addEventListener("click", async () => {
        try {
            await navigator.clipboard.writeText(cardUrl);
            const originalText = shareCopyLink.querySelector("span:last-child").textContent;
            shareCopyLink.querySelector("span:last-child").textContent = "已复制";
            setTimeout(() => {
                shareCopyLink.querySelector("span:last-child").textContent = originalText;
            }, 1500);
        } catch (err) {
            // 降级方案
            const input = document.createElement("input");
            input.value = cardUrl;
            document.body.appendChild(input);
            input.select();
            document.execCommand("copy");
            document.body.removeChild(input);
            const originalText = shareCopyLink.querySelector("span:last-child").textContent;
            shareCopyLink.querySelector("span:last-child").textContent = "已复制";
            setTimeout(() => {
                shareCopyLink.querySelector("span:last-child").textContent = originalText;
            }, 1500);
        }
    });

    // 下载图片
    shareDownloadImg && shareDownloadImg.addEventListener("click", async () => {
        let svgUrl = null;
        try {
            const response = await fetch(shareImagePreview.src || shareUrl);
            const blob = await response.blob();
            svgUrl = URL.createObjectURL(blob);
            const img = new Image();
            img.decoding = "async";
            img.src = svgUrl;
            if (img.decode) {
                await img.decode();
            } else {
                await new Promise((resolve, reject) => {
                    img.onload = resolve;
                    img.onerror = reject;
                });
            }

            const canvas = document.createElement("canvas");
            canvas.width = img.naturalWidth || 640;
            canvas.height = img.naturalHeight || 360;
            const ctx = canvas.getContext("2d");
            ctx.drawImage(img, 0, 0, canvas.width, canvas.height);

            const pngBlob = await new Promise((resolve, reject) => {
                canvas.toBlob((result) => {
                    result ? resolve(result) : reject(new Error("PNG export failed"));
                }, "image/png");
            });
            const pngUrl = URL.createObjectURL(pngBlob);
            const a = document.createElement("a");
            a.href = pngUrl;
            a.download = "share-card.png";
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(pngUrl);
        } catch (e) {
            alert("下载失败，请尝试查看大图后手动保存");
        } finally {
            if (svgUrl) URL.revokeObjectURL(svgUrl);
        }
    });

    // 复制预览链接
    const sharePreviewLink = document.getElementById("share-preview-link");
    sharePreviewLink && sharePreviewLink.addEventListener("click", async () => {
        const previewUrl = sharePreviewLink.dataset.previewUrl || "";
        if (!previewUrl) return;
        try {
            await navigator.clipboard.writeText(previewUrl);
            const originalText = sharePreviewLink.querySelector("span:last-child").textContent;
            sharePreviewLink.querySelector("span:last-child").textContent = "已复制";
            setTimeout(() => {
                sharePreviewLink.querySelector("span:last-child").textContent = originalText;
            }, 1500);
        } catch (err) {
            // 降级方案
            const input = document.createElement("input");
            input.value = previewUrl;
            document.body.appendChild(input);
            input.select();
            document.execCommand("copy");
            document.body.removeChild(input);
            const originalText = sharePreviewLink.querySelector("span:last-child").textContent;
            sharePreviewLink.querySelector("span:last-child").textContent = "已复制";
            setTimeout(() => {
                sharePreviewLink.querySelector("span:last-child").textContent = originalText;
            }, 1500);
        }
    });
})();
