(function () {
    "use strict";

    var config = document.getElementById("scrapePageConfig");
    if (!config) {
        return;
    }

    var previewSection = document.getElementById("scrapePreviewSection");
    var previewGrid = document.getElementById("scrapePreviewGrid");
    var previewMeta = document.getElementById("scrapePreviewMeta");
    var previewSummary = document.getElementById("scrapePreviewSummary");
    var previewFlash = document.getElementById("scrapePreviewFlash");
    var selectAllCheckbox = document.getElementById("scrapePreviewSelectAll");
    var registerButton = document.getElementById("registerSelectedButton");
    var registerUrl = config.dataset.registerUrl;
    var statusUrlTemplate = config.dataset.statusUrlTemplate;
    var restoreJobId = config.dataset.restoreJobId;
    var activePreviewJob = null;
    var pollTimer = null;

    function getCsrfToken() {
        var csrfMeta = document.querySelector('meta[name="csrf-token"]');
        return csrfMeta ? String(csrfMeta.content || "").trim() : "";
    }

    function normalizeUrl(rawUrl) {
        var value = String(rawUrl || "").trim();
        if (!value) {
            return "";
        }
        try {
            return new URL(value, window.location.origin).toString();
        } catch (_error) {
            return "";
        }
    }

    function assignUrl(element, propertyName, rawUrl) {
        var normalized = normalizeUrl(rawUrl);
        if (!normalized) {
            return false;
        }
        element[propertyName] = normalized;
        return true;
    }

    function buildResponseErrorMessage(response, responseText, fallbackMessage) {
        var trimmed = String(responseText || "").trim();
        if (trimmed && trimmed.charAt(0) !== "<" && trimmed.length <= 160) {
            return trimmed;
        }
        if (response.status === 400) {
            return "送信内容が不正です。ページを再読み込みして再度お試しください。";
        }
        if (response.status === 401) {
            return "ログイン状態を確認して再度お試しください。";
        }
        if (response.status === 403) {
            return "送信が拒否されました。ページを再読み込みして再度お試しください。";
        }
        if (response.status === 404) {
            return "リクエスト先が見つかりません。";
        }
        if (response.status >= 500) {
            return "サーバーエラーが発生しました。";
        }
        return fallbackMessage;
    }

    function parseJsonResponse(response, fallbackMessage) {
        return response.text().then(function (responseText) {
            var trimmed = String(responseText || "").trim();
            var contentType = String(response.headers.get("content-type") || "").toLowerCase();
            var payload = null;
            var looksLikeJson = contentType.indexOf("application/json") !== -1
                || trimmed.charAt(0) === "{"
                || trimmed.charAt(0) === "[";

            if (trimmed) {
                if (looksLikeJson) {
                    try {
                        payload = JSON.parse(trimmed);
                    } catch (_error) {
                        throw new Error(buildResponseErrorMessage(response, trimmed, fallbackMessage));
                    }
                } else {
                    throw new Error(buildResponseErrorMessage(response, trimmed, fallbackMessage));
                }
            }

            if (!response.ok) {
                throw new Error(
                    (payload && payload.error)
                    || buildResponseErrorMessage(response, trimmed, fallbackMessage)
                );
            }

            return payload || {};
        });
    }

    function clearPollTimer() {
        if (pollTimer) {
            clearTimeout(pollTimer);
            pollTimer = null;
        }
    }

    function buildStatusUrl(jobId) {
        return statusUrlTemplate.replace("__JOB_ID__", encodeURIComponent(jobId));
    }

    function registerTrackerJob(jobData) {
        if (!window.ESPScrapeTracker || !window.ESPScrapeTracker.registerJob) {
            return;
        }
        window.ESPScrapeTracker.registerJob(jobData);
    }

    function refreshTracker() {
        if (!window.ESPScrapeTracker || !window.ESPScrapeTracker.refreshNow) {
            return;
        }
        window.ESPScrapeTracker.refreshNow();
    }

    function showFlash(message, type) {
        previewFlash.hidden = false;
        previewFlash.textContent = message;
        previewFlash.className = "scrape-preview-flash";
        if (type) {
            previewFlash.classList.add(type);
        }
    }

    function clearFlash() {
        previewFlash.hidden = true;
        previewFlash.textContent = "";
        previewFlash.className = "scrape-preview-flash";
    }

    function getItemImageUrl(item) {
        if (Array.isArray(item.image_urls) && item.image_urls.length > 0) {
            return item.image_urls[0];
        }
        return "";
    }

    function formatPrice(value) {
        if (value === null || value === undefined || value === "") {
            return "価格未取得";
        }
        return "¥" + Number(value).toLocaleString("ja-JP");
    }

    function updateRegisterButtonState() {
        var checkedCount = previewGrid.querySelectorAll(".scrape-preview-checkbox:checked").length;
        registerButton.disabled = !activePreviewJob || !activePreviewJob.jobId || checkedCount === 0;
    }

    function renderPreview(result) {
        var items = result.items || [];
        activePreviewJob = {
            jobId: result.job_id || (activePreviewJob && activePreviewJob.jobId) || null,
            registerUrl: registerUrl,
            items: items
        };

        previewSection.hidden = false;
        clearFlash();
        previewSummary.textContent = "抽出 " + items.length + "件 / 除外 " + (result.excluded_count || 0) + "件";
        previewMeta.innerHTML = "";
        previewGrid.innerHTML = "";
        selectAllCheckbox.checked = true;

        if (result.search_url) {
            var metaLink = document.createElement("a");
            if (assignUrl(metaLink, "href", result.search_url)) {
                metaLink.target = "_blank";
                metaLink.rel = "noopener";
                metaLink.textContent = "検索URLを開く";
                previewMeta.appendChild(metaLink);
            }
        }

        if (!items.length) {
            var empty = document.createElement("p");
            empty.className = "text-muted";
            empty.textContent = "条件に一致する商品はありませんでした。";
            previewGrid.appendChild(empty);
            updateRegisterButtonState();
            previewSection.scrollIntoView({ behavior: "smooth", block: "start" });
            return;
        }

        items.forEach(function (item, index) {
            var card = document.createElement("label");
            card.className = "scrape-preview-card";

            var checkbox = document.createElement("input");
            checkbox.type = "checkbox";
            checkbox.className = "scrape-preview-checkbox";
            checkbox.value = String(index);
            checkbox.checked = true;
            checkbox.addEventListener("change", function () {
                updateRegisterButtonState();
                if (!checkbox.checked) {
                    selectAllCheckbox.checked = false;
                }
            });

            var imageWrap = document.createElement("div");
            imageWrap.className = "scrape-preview-image";
            var imageUrl = getItemImageUrl(item);
            if (imageUrl) {
                var img = document.createElement("img");
                if (assignUrl(img, "src", imageUrl)) {
                    img.alt = item.title || "preview image";
                    imageWrap.appendChild(img);
                } else {
                    imageWrap.textContent = "No Image";
                    imageWrap.classList.add("is-empty");
                }
            } else {
                imageWrap.textContent = "No Image";
                imageWrap.classList.add("is-empty");
            }

            var body = document.createElement("div");
            body.className = "scrape-preview-card-body";

            var title = document.createElement("div");
            title.className = "scrape-preview-title";
            title.textContent = item.title || "(タイトルなし)";

            var price = document.createElement("div");
            price.className = "scrape-preview-price";
            price.textContent = formatPrice(item.price);

            var status = document.createElement("div");
            status.className = "scrape-preview-status";
            status.textContent = item.status || "ステータス不明";

            body.appendChild(title);
            body.appendChild(price);
            body.appendChild(status);

            if (item.url) {
                var link = document.createElement("a");
                if (assignUrl(link, "href", item.url)) {
                    link.target = "_blank";
                    link.rel = "noopener";
                    link.textContent = "商品ページを開く";
                    link.className = "scrape-preview-link";
                    body.appendChild(link);
                }
            }

            card.appendChild(checkbox);
            card.appendChild(imageWrap);
            card.appendChild(body);
            previewGrid.appendChild(card);
        });

        updateRegisterButtonState();
        previewSection.scrollIntoView({ behavior: "smooth", block: "start" });
    }

    function pollPreviewStatus(statusUrl) {
        clearPollTimer();
        fetch(statusUrl)
            .then(function (response) {
                return parseJsonResponse(response, "ステータス取得に失敗しました");
            })
            .then(function (data) {
                registerTrackerJob(data);

                if (data.status === "queued" || data.status === "running") {
                    pollTimer = setTimeout(function () {
                        pollPreviewStatus(statusUrl);
                    }, 2000);
                    return;
                }

                if (data.status === "failed") {
                    previewSection.hidden = false;
                    showFlash("商品抽出に失敗しました: " + (data.error || "不明なエラー"), "error");
                    refreshTracker();
                    return;
                }

                var result = data.result || {};
                result.job_id = data.job_id;
                renderPreview(result);
                refreshTracker();
            })
            .catch(function (error) {
                previewSection.hidden = false;
                showFlash(error.message || "通信エラーが発生しました", "error");
            });
    }

    function submitPreviewForm(form) {
        clearFlash();
        clearPollTimer();

        var formData = new FormData(form);
        formData.append("response_mode", "preview");
        var csrfToken = getCsrfToken();
        var requestOptions = {
            method: "POST",
            body: formData
        };
        if (csrfToken) {
            requestOptions.headers = {
                "X-CSRFToken": csrfToken
            };
        }

        fetch(form.action, requestOptions)
            .then(function (response) {
                return parseJsonResponse(response, "商品抽出ジョブの作成に失敗しました");
            })
            .then(function (data) {
                activePreviewJob = {
                    jobId: data.job_id,
                    registerUrl: data.register_url,
                    items: []
                };
                registerTrackerJob(data);
                pollPreviewStatus(data.status_url);
            })
            .catch(function (error) {
                previewSection.hidden = false;
                showFlash(error.message || "送信に失敗しました", "error");
            });
    }

    function restorePreviewJob(jobId) {
        if (!jobId) {
            return;
        }

        var statusUrl = buildStatusUrl(jobId);
        fetch(statusUrl)
            .then(function (response) {
                return parseJsonResponse(response, "抽出ジョブの復元に失敗しました");
            })
            .then(function (data) {
                registerTrackerJob(data);
                activePreviewJob = {
                    jobId: data.job_id,
                    registerUrl: registerUrl,
                    items: []
                };

                if (data.context && data.context.persist_to_db !== false && data.result_url) {
                    window.location.href = data.result_url;
                    return;
                }

                if (data.status === "completed") {
                    var result = data.result || {};
                    result.job_id = data.job_id;
                    renderPreview(result);
                    return;
                }

                if (data.status === "failed") {
                    previewSection.hidden = false;
                    showFlash("商品抽出に失敗しました: " + (data.error || "不明なエラー"), "error");
                    return;
                }

                pollPreviewStatus(statusUrl);
            })
            .catch(function (error) {
                previewSection.hidden = false;
                showFlash(error.message || "抽出ジョブの復元に失敗しました", "error");
            });
    }

    function getSelectedIndices() {
        return Array.from(previewGrid.querySelectorAll(".scrape-preview-checkbox:checked")).map(function (checkbox) {
            return Number(checkbox.value);
        });
    }

    registerButton.addEventListener("click", function () {
        if (!activePreviewJob || !activePreviewJob.jobId) {
            return;
        }

        var selectedIndices = getSelectedIndices();
        if (!selectedIndices.length) {
            showFlash("登録する商品を選択してください。", "error");
            return;
        }

        registerButton.disabled = true;
        var csrfToken = getCsrfToken();
        var headers = {
            "Content-Type": "application/json"
        };
        if (csrfToken) {
            headers["X-CSRFToken"] = csrfToken;
        }
        fetch(activePreviewJob.registerUrl, {
            method: "POST",
            headers: headers,
            body: JSON.stringify({
                job_id: activePreviewJob.jobId,
                selected_indices: selectedIndices
            })
        })
            .then(function (response) {
                return parseJsonResponse(response, "登録に失敗しました");
            })
            .then(function (data) {
                showFlash(
                    "登録完了: " + data.registered_count + "件（新規 " + data.new_count + " / 更新 " + data.updated_count + "）",
                    "success"
                );
            })
            .catch(function (error) {
                showFlash(error.message || "登録に失敗しました", "error");
            })
            .finally(function () {
                updateRegisterButtonState();
            });
    });

    selectAllCheckbox.addEventListener("change", function () {
        previewGrid.querySelectorAll(".scrape-preview-checkbox").forEach(function (checkbox) {
            checkbox.checked = selectAllCheckbox.checked;
        });
        updateRegisterButtonState();
    });

    document.querySelectorAll(".scrape-form-compact").forEach(function (form) {
        form.addEventListener("submit", function (event) {
            if (!window.fetch || !window.FormData) {
                return;
            }
            event.preventDefault();
            submitPreviewForm(form);
        });
    });

    restorePreviewJob(restoreJobId);
})();
