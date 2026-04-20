(function () {
    "use strict";

    var panel = null;
    var statusNode = null;
    var suggestionsNode = null;
    var pollTimer = null;
    var pollAttempts = 0;
    var maxPollAttempts = 60;
    var pollingJobId = null;

    function getCsrfToken() {
        var meta = document.querySelector('meta[name="csrf-token"]');
        return meta ? meta.getAttribute("content") : "";
    }

    function setStatus(text, kind) {
        if (!statusNode) return;
        statusNode.textContent = text || "";
        statusNode.dataset.kind = kind || "";
    }

    function escapeHtml(value) {
        if (value === null || value === undefined) return "";
        return String(value)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#039;");
    }

    function formatTimestamp(iso) {
        if (!iso) return "";
        try {
            var d = new Date(iso);
            if (isNaN(d.getTime())) return iso;
            return d.toLocaleString();
        } catch (err) {
            return iso;
        }
    }

    function statusLabel(status) {
        switch (status) {
            case "queued":
                return "待機中";
            case "running":
                return "翻訳中";
            case "succeeded":
                return "確認待ち";
            case "failed":
                return "失敗";
            case "applied":
                return "反映済み";
            case "rejected":
                return "破棄済み";
            default:
                return status || "";
        }
    }

    function renderSuggestionCard(item) {
        var translatedTitle = item.translated_title || "";
        var translatedDescription = item.translated_description || "";
        var status = item.status || "queued";
        var errorMessage = item.error_message || "";
        var scope = item.scope || "full";
        var showApply =
            status === "succeeded" && (translatedTitle || translatedDescription);
        var showReject =
            status === "queued" || status === "running" || status === "succeeded";

        var titleBlock = "";
        if (scope === "title" || scope === "full") {
            titleBlock =
                '<div class="translation-field">' +
                    '<span class="translation-field-label">タイトル (英)</span>' +
                    '<div class="translation-field-value">' +
                        (translatedTitle
                            ? escapeHtml(translatedTitle)
                            : '<span class="translation-field-empty">—</span>') +
                    "</div>" +
                "</div>";
        }

        var descriptionBlock = "";
        if (scope === "description" || scope === "full") {
            descriptionBlock =
                '<div class="translation-field">' +
                    '<span class="translation-field-label">説明文 (英)</span>' +
                    '<div class="translation-field-value translation-field-html">' +
                        (translatedDescription
                            ? translatedDescription
                            : '<span class="translation-field-empty">—</span>') +
                    "</div>" +
                "</div>";
        }

        var actions = "";
        if (showApply) {
            actions +=
                '<button type="button" class="translation-action-btn translation-action-apply" data-action="apply" data-job-id="' +
                escapeHtml(item.job_id) +
                '">英語欄に反映</button>';
        }
        if (showReject) {
            actions +=
                '<button type="button" class="translation-action-btn translation-action-reject" data-action="reject" data-job-id="' +
                escapeHtml(item.job_id) +
                '">破棄</button>';
        }

        var errorBlock = errorMessage
            ? '<div class="translation-error">' + escapeHtml(errorMessage) + "</div>"
            : "";

        return (
            '<article class="translation-suggestion translation-suggestion-' +
            escapeHtml(status) +
            '" data-job-id="' +
            escapeHtml(item.job_id) +
            '">' +
                '<header class="translation-suggestion-header">' +
                    '<span class="translation-status-badge translation-status-' +
                    escapeHtml(status) +
                    '">' +
                    escapeHtml(statusLabel(status)) +
                    "</span>" +
                    '<span class="translation-scope-badge">' +
                    escapeHtml(scope) +
                    "</span>" +
                    '<span class="translation-timestamp">' +
                    escapeHtml(formatTimestamp(item.created_at)) +
                    "</span>" +
                "</header>" +
                '<div class="translation-suggestion-body">' +
                    titleBlock +
                    descriptionBlock +
                    errorBlock +
                "</div>" +
                (actions
                    ? '<footer class="translation-suggestion-actions">' +
                          actions +
                          "</footer>"
                    : "") +
            "</article>"
        );
    }

    function renderSuggestions(items) {
        if (!suggestionsNode) return;
        if (!items || !items.length) {
            suggestionsNode.innerHTML = "";
            return;
        }
        suggestionsNode.innerHTML = items.map(renderSuggestionCard).join("");
    }

    function fetchSuggestions() {
        var url = panel.dataset.suggestionsUrl;
        return fetch(url, {
            method: "GET",
            credentials: "same-origin",
            headers: {Accept: "application/json"},
        })
            .then(function (res) {
                if (!res.ok) throw new Error("HTTP " + res.status);
                return res.json();
            })
            .then(function (payload) {
                renderSuggestions(payload.items || []);
                return payload.items || [];
            });
    }

    function stopPolling() {
        if (pollTimer) {
            clearTimeout(pollTimer);
            pollTimer = null;
        }
        pollAttempts = 0;
        pollingJobId = null;
    }

    function schedulePoll(jobId) {
        pollingJobId = jobId;
        pollAttempts += 1;
        if (pollAttempts > maxPollAttempts) {
            setStatus("翻訳が完了しません。しばらくしてから再度お試しください。", "warning");
            stopPolling();
            return;
        }
        pollTimer = setTimeout(function () {
            fetchSuggestions()
                .then(function (items) {
                    var target = null;
                    for (var i = 0; i < items.length; i += 1) {
                        if (items[i].job_id === jobId) {
                            target = items[i];
                            break;
                        }
                    }
                    if (!target) {
                        stopPolling();
                        return;
                    }
                    if (target.status === "queued" || target.status === "running") {
                        schedulePoll(jobId);
                        return;
                    }
                    stopPolling();
                    if (target.status === "succeeded") {
                        setStatus("翻訳が完了しました。内容を確認して反映してください。", "success");
                    } else if (target.status === "failed") {
                        setStatus(
                            "翻訳に失敗しました: " + (target.error_message || "不明なエラー"),
                            "error"
                        );
                    }
                })
                .catch(function (err) {
                    schedulePoll(jobId);
                });
        }, 1500);
    }

    function requestTranslation(scope) {
        var url = panel.dataset.translateUrl;
        setStatus("翻訳をリクエスト中…", "info");
        disableButtons(true);
        return fetch(url, {
            method: "POST",
            credentials: "same-origin",
            headers: {
                "Content-Type": "application/json",
                Accept: "application/json",
                "X-CSRFToken": getCsrfToken(),
            },
            body: JSON.stringify({scope: scope}),
        })
            .then(function (res) {
                return res.json().then(function (body) {
                    return {ok: res.ok, status: res.status, body: body};
                });
            })
            .then(function (result) {
                disableButtons(false);
                if (!result.ok) {
                    var message = "翻訳リクエストに失敗しました。";
                    if (result.body && result.body.error === "empty_source") {
                        message = "翻訳対象の日本語テキストがありません。";
                    }
                    setStatus(message, "error");
                    return;
                }
                var suggestion = result.body.suggestion || {};
                renderSuggestions([suggestion]);
                if (suggestion.status === "succeeded") {
                    setStatus("翻訳が完了しました。内容を確認して反映してください。", "success");
                } else if (suggestion.status === "failed") {
                    setStatus(
                        "翻訳に失敗しました: " + (suggestion.error_message || "不明なエラー"),
                        "error"
                    );
                } else {
                    setStatus("翻訳をリクエストしました。しばらくお待ちください。", "info");
                    stopPolling();
                    schedulePoll(result.body.job_id);
                }
            })
            .catch(function (err) {
                disableButtons(false);
                setStatus("翻訳リクエスト中にエラーが発生しました。", "error");
            });
    }

    function applySuggestion(jobId) {
        var template = panel.dataset.applyUrlTemplate;
        var url = template.replace("{job_id}", encodeURIComponent(jobId));
        return fetch(url, {
            method: "POST",
            credentials: "same-origin",
            headers: {
                "Content-Type": "application/json",
                Accept: "application/json",
                "X-CSRFToken": getCsrfToken(),
            },
            body: JSON.stringify({apply_title: true, apply_description: true}),
        })
            .then(function (res) {
                return res.json().then(function (body) {
                    return {ok: res.ok, body: body};
                });
            })
            .then(function (result) {
                if (!result.ok) {
                    setStatus("英訳の反映に失敗しました。", "error");
                    return;
                }
                var suggestion = result.body.suggestion || {};
                applySuggestionToFormFields(suggestion);
                setStatus("英訳を反映しました。保存ボタンで確定してください。", "success");
                fetchSuggestions();
            })
            .catch(function () {
                setStatus("英訳の反映中にエラーが発生しました。", "error");
            });
    }

    function applySuggestionToFormFields(suggestion) {
        if (!suggestion) return;

        if (suggestion.translated_title) {
            var titleInput = document.getElementById("title_en");
            if (titleInput) {
                titleInput.value = suggestion.translated_title;
                titleInput.dispatchEvent(new Event("change", {bubbles: true}));
            }
        }

        if (suggestion.translated_description) {
            var editorId = "description_en";
            var textarea = document.getElementById(editorId);
            if (typeof tinymce !== "undefined") {
                var editor = tinymce.get(editorId);
                if (editor) {
                    editor.setContent(suggestion.translated_description);
                    editor.save();
                    return;
                }
            }
            if (textarea) {
                textarea.value = suggestion.translated_description;
                textarea.dispatchEvent(new Event("change", {bubbles: true}));
            }
        }
    }

    function rejectSuggestion(jobId) {
        var template = panel.dataset.rejectUrlTemplate;
        var url = template.replace("{job_id}", encodeURIComponent(jobId));
        return fetch(url, {
            method: "POST",
            credentials: "same-origin",
            headers: {
                "Content-Type": "application/json",
                Accept: "application/json",
                "X-CSRFToken": getCsrfToken(),
            },
        })
            .then(function () {
                if (pollingJobId === jobId) stopPolling();
                fetchSuggestions();
            })
            .catch(function () {
                setStatus("破棄の更新に失敗しました。", "error");
            });
    }

    function disableButtons(disabled) {
        var buttons = document.querySelectorAll("[data-translate-scope]");
        for (var i = 0; i < buttons.length; i += 1) {
            buttons[i].disabled = Boolean(disabled);
        }
    }

    function bindTriggerButtons() {
        var buttons = document.querySelectorAll("[data-translate-scope]");
        for (var i = 0; i < buttons.length; i += 1) {
            (function (btn) {
                btn.addEventListener("click", function (evt) {
                    evt.preventDefault();
                    var scope = btn.getAttribute("data-translate-scope") || "full";
                    requestTranslation(scope);
                });
            })(buttons[i]);
        }
    }

    function bindSuggestionActions() {
        if (!suggestionsNode) return;
        suggestionsNode.addEventListener("click", function (evt) {
            var target = evt.target;
            if (!target || !target.matches("[data-action]")) return;
            var action = target.getAttribute("data-action");
            var jobId = target.getAttribute("data-job-id");
            if (!jobId) return;
            evt.preventDefault();
            if (action === "apply") {
                applySuggestion(jobId);
            } else if (action === "reject") {
                rejectSuggestion(jobId);
            }
        });
    }

    function init() {
        panel = document.getElementById("translationPanel");
        if (!panel) return;
        statusNode = panel.querySelector("[data-translation-status]");
        suggestionsNode = panel.querySelector("[data-translation-suggestions]");
        bindTriggerButtons();
        bindSuggestionActions();
        fetchSuggestions().catch(function () {});
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
