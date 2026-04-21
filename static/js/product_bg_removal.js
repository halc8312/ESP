(function () {
    "use strict";

    var grid = null;
    var productId = null;
    // Map image_url -> { jobId, status, resultUrl }
    var jobsByImageUrl = {};
    var pollTimer = null;
    var pollDeadline = 0;
    var POLL_INTERVAL_MS = 2500;
    var POLL_MAX_SECONDS = 180;

    function getCsrfToken() {
        var meta = document.querySelector('meta[name="csrf-token"]');
        return meta ? meta.getAttribute("content") : "";
    }

    function findCard(imageUrl) {
        if (!grid) return null;
        var cards = grid.querySelectorAll(".product-image-card");
        for (var i = 0; i < cards.length; i++) {
            if (cards[i].getAttribute("data-image-url") === imageUrl) {
                return cards[i];
            }
        }
        return null;
    }

    function setCardStatus(card, text, kind) {
        if (!card) return;
        var el = card.querySelector("[data-bg-status]");
        if (!el) return;
        if (!text) {
            el.hidden = true;
            el.textContent = "";
            el.removeAttribute("data-kind");
            return;
        }
        el.hidden = false;
        el.textContent = text;
        if (kind) {
            el.setAttribute("data-kind", kind);
        } else {
            el.removeAttribute("data-kind");
        }
    }

    function setCardActionsVisible(card, visible) {
        if (!card) return;
        var el = card.querySelector("[data-bg-actions]");
        if (!el) return;
        el.hidden = !visible;
    }

    function setButtonBusy(card, busy) {
        if (!card) return;
        var btn = card.querySelector("[data-bg-remove-btn]");
        if (!btn) return;
        if (busy) {
            btn.setAttribute("disabled", "disabled");
            btn.dataset.originalLabel = btn.dataset.originalLabel || btn.textContent;
            btn.textContent = "処理中";
        } else {
            btn.removeAttribute("disabled");
            if (btn.dataset.originalLabel) {
                btn.textContent = btn.dataset.originalLabel;
            }
        }
    }

    function setThumbPreview(card, resultUrl) {
        if (!card || !resultUrl) return;
        var thumb = card.querySelector("[data-bg-thumb]");
        if (!thumb) return;
        if (!thumb.dataset.originalSrc) {
            thumb.dataset.originalSrc = thumb.getAttribute("src") || "";
        }
        thumb.setAttribute("src", resultUrl);
    }

    function clearThumbPreview(card) {
        if (!card) return;
        var thumb = card.querySelector("[data-bg-thumb]");
        if (!thumb) return;
        if (thumb.dataset.originalSrc) {
            thumb.setAttribute("src", thumb.dataset.originalSrc);
            delete thumb.dataset.originalSrc;
        }
    }

    function schedulePoll() {
        if (pollTimer) return;
        pollDeadline = Date.now() + POLL_MAX_SECONDS * 1000;
        pollTimer = window.setInterval(pollOnce, POLL_INTERVAL_MS);
    }

    function stopPollIfIdle() {
        var anyPending = false;
        Object.keys(jobsByImageUrl).forEach(function (imageUrl) {
            var info = jobsByImageUrl[imageUrl];
            if (info && (info.status === "queued" || info.status === "running")) {
                anyPending = true;
            }
        });
        if (!anyPending && pollTimer) {
            window.clearInterval(pollTimer);
            pollTimer = null;
        }
    }

    function pollOnce() {
        if (!productId) return;
        if (Date.now() > pollDeadline) {
            window.clearInterval(pollTimer);
            pollTimer = null;
            return;
        }

        fetch("/api/products/" + productId + "/image-processing-jobs", {
            method: "GET",
            credentials: "same-origin",
            headers: { Accept: "application/json" },
        })
            .then(function (res) {
                if (!res.ok) throw new Error("HTTP " + res.status);
                return res.json();
            })
            .then(function (data) {
                var items = (data && data.items) || [];
                // Build a map of most-recent job per source_image_url.
                var latestByUrl = {};
                items.forEach(function (item) {
                    if (!item || !item.source_image_url) return;
                    var existing = latestByUrl[item.source_image_url];
                    if (!existing || existing.created_at < item.created_at) {
                        latestByUrl[item.source_image_url] = item;
                    }
                });

                Object.keys(jobsByImageUrl).forEach(function (imageUrl) {
                    var info = jobsByImageUrl[imageUrl];
                    if (!info || !info.jobId) return;
                    var job = latestByUrl[imageUrl];
                    if (!job || job.job_id !== info.jobId) return;
                    info.status = job.status;
                    info.resultUrl = job.result_image_url || info.resultUrl;
                    renderCardFromJob(imageUrl, job);
                });

                stopPollIfIdle();
            })
            .catch(function () {
                // Silently ignore transient errors; the next tick will retry.
            });
    }

    function renderCardFromJob(imageUrl, job) {
        var card = findCard(imageUrl);
        if (!card) return;

        if (job.status === "queued") {
            setCardStatus(card, "待機中", "pending");
            setCardActionsVisible(card, false);
            setButtonBusy(card, true);
        } else if (job.status === "running") {
            setCardStatus(card, "白抜き処理中", "pending");
            setCardActionsVisible(card, false);
            setButtonBusy(card, true);
        } else if (job.status === "succeeded") {
            setCardStatus(card, "プレビュー: 背景を白抜きしました", "success");
            if (job.result_image_url) {
                setThumbPreview(card, job.result_image_url);
            }
            setCardActionsVisible(card, true);
            setButtonBusy(card, false);
            refreshBulkApplyVisibility();
        } else if (job.status === "failed") {
            setCardStatus(
                card,
                "失敗: " + (job.error_message || "再試行してください"),
                "error"
            );
            clearThumbPreview(card);
            setCardActionsVisible(card, false);
            setButtonBusy(card, false);
        } else if (job.status === "applied") {
            setCardStatus(card, "反映済み", "success");
            clearThumbPreview(card);
            setCardActionsVisible(card, false);
            setButtonBusy(card, false);
        } else if (job.status === "rejected") {
            setCardStatus(card, "破棄しました", "muted");
            clearThumbPreview(card);
            setCardActionsVisible(card, false);
            setButtonBusy(card, false);
        }
    }

    function handleRemoveClick(card) {
        if (!card || !productId) return;
        var imageUrl = card.getAttribute("data-image-url");
        if (!imageUrl) return;

        setCardStatus(card, "ジョブ登録中...", "pending");
        setButtonBusy(card, true);

        fetch("/api/products/" + productId + "/images/remove-background", {
            method: "POST",
            credentials: "same-origin",
            headers: {
                "Content-Type": "application/json",
                "X-CSRF-Token": getCsrfToken(),
                Accept: "application/json",
            },
            body: JSON.stringify({ image_url: imageUrl }),
        })
            .then(function (res) {
                return res.json().then(function (payload) {
                    return { ok: res.ok, status: res.status, payload: payload };
                });
            })
            .then(function (result) {
                if (!result.ok) {
                    var err =
                        (result.payload && result.payload.error) ||
                        "request_failed";
                    setCardStatus(card, "エラー: " + err, "error");
                    setButtonBusy(card, false);
                    return;
                }
                var job = result.payload.job || {};
                jobsByImageUrl[imageUrl] = {
                    jobId: job.job_id,
                    status: job.status,
                    resultUrl: job.result_image_url || null,
                };
                renderCardFromJob(imageUrl, job);
                if (job.status === "queued" || job.status === "running") {
                    schedulePoll();
                }
            })
            .catch(function () {
                setCardStatus(card, "ネットワークエラー", "error");
                setButtonBusy(card, false);
            });
    }

    function handleApplyClick(card) {
        if (!card) return;
        var imageUrl = card.getAttribute("data-image-url");
        var info = jobsByImageUrl[imageUrl];
        if (!info || !info.jobId) return;

        setCardStatus(card, "反映中...", "pending");

        fetch("/api/image-processing-jobs/" + info.jobId + "/apply", {
            method: "POST",
            credentials: "same-origin",
            headers: {
                "X-CSRF-Token": getCsrfToken(),
                Accept: "application/json",
            },
        })
            .then(function (res) {
                return res.json().then(function (payload) {
                    return { ok: res.ok, status: res.status, payload: payload };
                });
            })
            .then(function (result) {
                if (!result.ok) {
                    var err =
                        (result.payload && result.payload.error) ||
                        "apply_failed";
                    setCardStatus(card, "反映失敗: " + err, "error");
                    return;
                }
                setCardStatus(card, "反映済み。保存ボタンで確定してください。", "success");
                setCardActionsVisible(card, false);

                // Update the card's stored image URL so subsequent re-runs
                // point at the new result URL, and sync the hidden form
                // field via the legacy helper if available.
                var newUrl = info.resultUrl;
                if (newUrl) {
                    card.setAttribute("data-image-url", newUrl);
                    var urlNode = card.querySelector(".product-image-url");
                    if (urlNode) urlNode.textContent = newUrl;
                    var thumb = card.querySelector("[data-bg-thumb]");
                    if (thumb) {
                        thumb.setAttribute("src", newUrl);
                        delete thumb.dataset.originalSrc;
                    }
                    if (typeof window.replaceImageUrl === "function") {
                        try {
                            window.replaceImageUrl(imageUrl, newUrl);
                        } catch (err) {
                            // Legacy helper may not be present yet.
                        }
                    }
                    delete jobsByImageUrl[imageUrl];
                }
                refreshBulkApplyVisibility();
            })
            .catch(function () {
                setCardStatus(card, "ネットワークエラー", "error");
            });
    }

    function handleRejectClick(card) {
        if (!card) return;
        var imageUrl = card.getAttribute("data-image-url");
        var info = jobsByImageUrl[imageUrl];
        if (!info || !info.jobId) {
            setCardStatus(card, "");
            setCardActionsVisible(card, false);
            clearThumbPreview(card);
            return;
        }

        fetch("/api/image-processing-jobs/" + info.jobId + "/reject", {
            method: "POST",
            credentials: "same-origin",
            headers: {
                "X-CSRF-Token": getCsrfToken(),
                Accept: "application/json",
            },
        })
            .finally(function () {
                setCardStatus(card, "破棄しました", "muted");
                setCardActionsVisible(card, false);
                clearThumbPreview(card);
                delete jobsByImageUrl[imageUrl];
                refreshBulkApplyVisibility();
            });
    }

    function onGridClick(event) {
        var target = event.target;
        if (!(target instanceof Element)) return;
        var card = target.closest(".product-image-card");
        if (!card) return;

        if (target.hasAttribute("data-bg-remove-btn")) {
            event.preventDefault();
            handleRemoveClick(card);
        } else if (target.hasAttribute("data-bg-apply")) {
            event.preventDefault();
            handleApplyClick(card);
        } else if (target.hasAttribute("data-bg-reject")) {
            event.preventDefault();
            handleRejectClick(card);
        }
    }

    function onBulkClick() {
        if (!grid || !productId) return;
        var cards = grid.querySelectorAll(".product-image-card");
        cards.forEach(function (card) {
            var status = card.querySelector("[data-bg-status]");
            if (status && !status.hidden) {
                var kind = status.getAttribute("data-kind");
                if (kind === "pending" || kind === "success") return;
            }
            handleRemoveClick(card);
        });
    }

    function collectApplyableJobIds() {
        var ids = [];
        Object.keys(jobsByImageUrl).forEach(function (imageUrl) {
            var info = jobsByImageUrl[imageUrl];
            if (info && info.jobId && info.status === "succeeded") {
                ids.push({ imageUrl: imageUrl, jobId: info.jobId });
            }
        });
        return ids;
    }

    function refreshBulkApplyVisibility() {
        var btn = document.querySelector("[data-bg-apply-bulk-btn]");
        if (!btn) return;
        var hasApplyable = collectApplyableJobIds().length > 0;
        btn.hidden = !hasApplyable;
    }

    function onBulkApplyClick() {
        if (!grid || !productId) return;
        var applyable = collectApplyableJobIds();
        if (applyable.length === 0) return;

        var bulkBtn = document.querySelector("[data-bg-apply-bulk-btn]");
        if (bulkBtn) {
            bulkBtn.setAttribute("disabled", "disabled");
            bulkBtn.dataset.originalLabel =
                bulkBtn.dataset.originalLabel || bulkBtn.textContent;
            bulkBtn.textContent = "反映中...";
        }

        applyable.forEach(function (entry) {
            var card = findCard(entry.imageUrl);
            setCardStatus(card, "反映中...", "pending");
        });

        var jobIds = applyable.map(function (entry) {
            return entry.jobId;
        });

        fetch(
            "/api/products/" + productId + "/image-processing-jobs/apply-all",
            {
                method: "POST",
                credentials: "same-origin",
                headers: {
                    "Content-Type": "application/json",
                    "X-CSRF-Token": getCsrfToken(),
                    Accept: "application/json",
                },
                body: JSON.stringify({ job_ids: jobIds }),
            }
        )
            .then(function (res) {
                return res.json().then(function (payload) {
                    return { ok: res.ok, status: res.status, payload: payload };
                });
            })
            .then(function (result) {
                if (!result.ok) {
                    var err =
                        (result.payload && result.payload.error) ||
                        "apply_failed";
                    applyable.forEach(function (entry) {
                        var card = findCard(entry.imageUrl);
                        setCardStatus(card, "反映失敗: " + err, "error");
                    });
                    return;
                }

                var applied = (result.payload && result.payload.applied) || [];
                applied.forEach(function (job) {
                    var originalUrl = job.source_image_url;
                    var card = findCard(originalUrl);
                    var info = jobsByImageUrl[originalUrl] || {};
                    var newUrl = job.result_image_url || info.resultUrl;
                    setCardStatus(
                        card,
                        "反映済み。保存ボタンで確定してください。",
                        "success"
                    );
                    setCardActionsVisible(card, false);

                    if (card && newUrl) {
                        card.setAttribute("data-image-url", newUrl);
                        var urlNode = card.querySelector(".product-image-url");
                        if (urlNode) urlNode.textContent = newUrl;
                        var thumb = card.querySelector("[data-bg-thumb]");
                        if (thumb) {
                            thumb.setAttribute("src", newUrl);
                            delete thumb.dataset.originalSrc;
                        }
                        if (typeof window.replaceImageUrl === "function") {
                            try {
                                window.replaceImageUrl(originalUrl, newUrl);
                            } catch (err) {
                                // Legacy helper may not be present.
                            }
                        }
                    }
                    delete jobsByImageUrl[originalUrl];
                });

                var skipped = (result.payload && result.payload.skipped) || [];
                skipped.forEach(function (item) {
                    // Best-effort surface: find any card still tracking this jobId.
                    Object.keys(jobsByImageUrl).forEach(function (imageUrl) {
                        var info = jobsByImageUrl[imageUrl];
                        if (info && info.jobId === item.job_id) {
                            var card = findCard(imageUrl);
                            setCardStatus(
                                card,
                                "スキップ: " +
                                    (item.reason || "apply_failed"),
                                "error"
                            );
                        }
                    });
                });

                refreshBulkApplyVisibility();
            })
            .catch(function () {
                applyable.forEach(function (entry) {
                    var card = findCard(entry.imageUrl);
                    setCardStatus(card, "ネットワークエラー", "error");
                });
            })
            .finally(function () {
                if (bulkBtn) {
                    bulkBtn.removeAttribute("disabled");
                    if (bulkBtn.dataset.originalLabel) {
                        bulkBtn.textContent = bulkBtn.dataset.originalLabel;
                    }
                }
            });
    }

    function init() {
        grid = document.getElementById("imageSortGrid");
        if (!grid) return;
        var raw = grid.getAttribute("data-product-id");
        productId = raw ? parseInt(raw, 10) : null;
        if (!productId) return;

        grid.addEventListener("click", onGridClick);

        var bulkBtn = document.querySelector("[data-bg-remove-bulk-btn]");
        if (bulkBtn) {
            bulkBtn.addEventListener("click", function (e) {
                e.preventDefault();
                onBulkClick();
            });
        }

        var bulkApplyBtn = document.querySelector("[data-bg-apply-bulk-btn]");
        if (bulkApplyBtn) {
            bulkApplyBtn.addEventListener("click", function (e) {
                e.preventDefault();
                onBulkApplyClick();
            });
        }
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
