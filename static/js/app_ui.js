(function () {
    "use strict";

    var activeDialog = null;
    var dialogCloseHandler = null;
    var dialogKeyHandler = null;
    var dialogPreviouslyFocused = null;

    function byId(id) {
        return document.getElementById(id);
    }

    function createElement(tagName, className, text) {
        var element = document.createElement(tagName);
        if (className) {
            element.className = className;
        }
        if (text !== undefined && text !== null) {
            element.textContent = text;
        }
        return element;
    }

    function getFocusableElements(container) {
        if (!container) {
            return [];
        }
        return Array.prototype.filter.call(
            container.querySelectorAll(
                'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
            ),
            function (node) {
                return !node.disabled && !node.hidden && node.offsetParent !== null;
            }
        );
    }

    function ensureToastViewport() {
        var viewport = byId("espToastViewport");
        if (viewport) {
            return viewport;
        }

        viewport = createElement("div", "esp-toast-viewport");
        viewport.id = "espToastViewport";
        viewport.setAttribute("aria-live", "polite");
        viewport.setAttribute("aria-atomic", "true");
        document.body.appendChild(viewport);
        return viewport;
    }

    function ensureLoadingOverlay() {
        var overlay = byId("espLoadingOverlay");
        if (overlay) {
            return overlay;
        }

        overlay = createElement("div", "loading-overlay");
        overlay.id = "espLoadingOverlay";
        overlay.hidden = true;

        var content = createElement("div", "loading-content");
        content.setAttribute("role", "status");
        content.setAttribute("aria-live", "assertive");

        content.appendChild(createElement("div", "loading-spinner"));

        var message = createElement("p", "loading-message", "処理中...");
        message.id = "espLoadingMessage";
        content.appendChild(message);

        var sub = createElement("p", "loading-sub", "しばらくお待ちください。");
        sub.id = "espLoadingSub";
        content.appendChild(sub);

        overlay.appendChild(content);
        document.body.appendChild(overlay);
        return overlay;
    }

    function ensureDialog() {
        var overlay = byId("espDialogOverlay");
        if (overlay) {
            return overlay;
        }

        overlay = createElement("div", "esp-dialog-overlay");
        overlay.id = "espDialogOverlay";
        overlay.hidden = true;

        var shell = createElement("div", "esp-dialog-shell");
        shell.setAttribute("role", "dialog");
        shell.setAttribute("aria-modal", "true");
        shell.setAttribute("aria-labelledby", "espDialogTitle");
        shell.setAttribute("aria-describedby", "espDialogDescription");

        var header = createElement("div", "esp-dialog-header");
        var heading = createElement("div", "esp-dialog-heading");
        var title = createElement("h2", "", "確認");
        title.id = "espDialogTitle";
        var description = createElement("p");
        description.id = "espDialogDescription";
        description.hidden = true;
        heading.appendChild(title);
        heading.appendChild(description);
        header.appendChild(heading);

        var closeButton = createElement("button", "esp-dialog-close", "×");
        closeButton.type = "button";
        closeButton.setAttribute("aria-label", "ダイアログを閉じる");
        header.appendChild(closeButton);

        shell.appendChild(header);
        shell.appendChild(createElement("div", "esp-dialog-body"));
        shell.appendChild(createElement("div", "esp-dialog-footer"));
        overlay.appendChild(shell);
        document.body.appendChild(overlay);
        return overlay;
    }

    function removeNode(node) {
        if (node && node.parentNode) {
            node.parentNode.removeChild(node);
        }
    }

    function toast(message, options) {
        options = options || {};
        var viewport = ensureToastViewport();
        var type = options.type || "info";
        var duration = options.duration === 0 ? 0 : (options.duration || 4200);

        var node = createElement("div", "esp-toast is-" + type);
        if (options.title) {
            var heading = createElement("div", "esp-toast-title", options.title);
            node.appendChild(heading);
        }

        node.appendChild(createElement("div", "esp-toast-body", message));

        var closeButton = createElement("button", "esp-toast-close", "×");
        closeButton.type = "button";
        closeButton.setAttribute("aria-label", "通知を閉じる");
        closeButton.addEventListener("click", function () {
            dismissToast(node);
        });
        node.appendChild(closeButton);

        viewport.appendChild(node);
        window.requestAnimationFrame(function () {
            node.classList.add("is-visible");
        });

        if (duration > 0) {
            window.setTimeout(function () {
                dismissToast(node);
            }, duration);
        }

        return node;
    }

    function dismissToast(node) {
        if (!node) {
            return;
        }
        node.classList.remove("is-visible");
        window.setTimeout(function () {
            removeNode(node);
        }, 180);
    }

    function setButtonBusy(button, busy, busyLabel) {
        if (!button) {
            return;
        }

        if (busy) {
            if (!button.dataset.originalLabel) {
                button.dataset.originalLabel = button.textContent;
            }
            button.disabled = true;
            button.setAttribute("aria-busy", "true");
            button.classList.add("is-busy");
            button.textContent = busyLabel || button.dataset.loadingLabel || "処理中...";
            return;
        }

        if (button.dataset.originalLabel) {
            button.textContent = button.dataset.originalLabel;
            delete button.dataset.originalLabel;
        }
        button.disabled = false;
        button.removeAttribute("aria-busy");
        button.classList.remove("is-busy");
    }

    function showLoading(options) {
        options = options || {};
        var overlay = ensureLoadingOverlay();
        var message = byId("espLoadingMessage");
        var sub = byId("espLoadingSub");

        if (message) {
            message.textContent = options.message || "処理中...";
        }
        if (sub) {
            sub.textContent = options.sub || "しばらくお待ちください。";
            sub.hidden = !sub.textContent;
        }

        overlay.hidden = false;
        document.body.classList.add("loading-open");
    }

    function hideLoading() {
        var overlay = ensureLoadingOverlay();
        overlay.hidden = true;
        document.body.classList.remove("loading-open");
    }

    function closeActiveDialog(result) {
        var overlay = ensureDialog();
        var body = overlay.querySelector(".esp-dialog-body");
        var footer = overlay.querySelector(".esp-dialog-footer");

        if (dialogCloseHandler) {
            overlay.removeEventListener("click", dialogCloseHandler);
            dialogCloseHandler = null;
        }
        if (dialogKeyHandler) {
            document.removeEventListener("keydown", dialogKeyHandler, true);
            dialogKeyHandler = null;
        }

        overlay.hidden = true;
        overlay.classList.remove("is-open");
        document.body.classList.remove("dialog-open");
        if (body) {
            body.innerHTML = "";
        }
        if (footer) {
            footer.innerHTML = "";
        }

        if (dialogPreviouslyFocused && typeof dialogPreviouslyFocused.focus === "function") {
            dialogPreviouslyFocused.focus();
        }
        dialogPreviouslyFocused = null;

        if (activeDialog && typeof activeDialog.resolve === "function") {
            activeDialog.resolve(result);
        }
        activeDialog = null;
    }

    function trapDialogFocus(event) {
        var overlay = ensureDialog();
        var shell = overlay.querySelector(".esp-dialog-shell");
        if (!shell || event.key !== "Tab") {
            return;
        }

        var focusable = getFocusableElements(shell);
        if (!focusable.length) {
            event.preventDefault();
            shell.focus();
            return;
        }

        var first = focusable[0];
        var last = focusable[focusable.length - 1];

        if (event.shiftKey && document.activeElement === first) {
            event.preventDefault();
            last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
            event.preventDefault();
            first.focus();
        }
    }

    function showDialog(options) {
        options = options || {};

        if (activeDialog) {
            closeActiveDialog(null);
        }

        var overlay = ensureDialog();
        var shell = overlay.querySelector(".esp-dialog-shell");
        var title = byId("espDialogTitle");
        var description = byId("espDialogDescription");
        var body = overlay.querySelector(".esp-dialog-body");
        var footer = overlay.querySelector(".esp-dialog-footer");
        var closeButton = overlay.querySelector(".esp-dialog-close");

        dialogPreviouslyFocused = document.activeElement;
        overlay.hidden = false;
        overlay.classList.add("is-open");
        document.body.classList.add("dialog-open");
        shell.classList.toggle("is-wide", !!options.wide);

        title.textContent = options.title || "確認";
        if (options.description) {
            description.hidden = false;
            description.textContent = options.description;
        } else {
            description.hidden = true;
            description.textContent = "";
        }

        body.innerHTML = "";
        if (options.body instanceof HTMLElement) {
            body.appendChild(options.body);
        } else if (options.body) {
            body.innerHTML = String(options.body);
        }

        footer.innerHTML = "";
        (options.actions || []).forEach(function (action, index) {
            var button = createElement(
                "button",
                "btn " + (action.className || action.variantClass || (action.variant === "danger" ? "btn-danger" : action.variant === "primary" ? "btn-primary" : "btn-secondary")),
                action.label
            );
            button.type = "button";
            if (action.autofocus || (!options.actions[index - 1] && !options.autoFocusSelector)) {
                button.dataset.dialogAutofocus = "true";
            }
            button.addEventListener("click", function () {
                closeActiveDialog(action.value);
            });
            footer.appendChild(button);
        });

        dialogCloseHandler = function (event) {
            if (event.target === overlay && options.closeOnBackdrop !== false) {
                closeActiveDialog(null);
            }
        };
        overlay.addEventListener("click", dialogCloseHandler);

        dialogKeyHandler = function (event) {
            if (event.key === "Escape") {
                event.preventDefault();
                closeActiveDialog(null);
                return;
            }
            trapDialogFocus(event);
        };
        document.addEventListener("keydown", dialogKeyHandler, true);

        closeButton.onclick = function () {
            closeActiveDialog(null);
        };

        activeDialog = {};
        var promise = new Promise(function (resolve) {
            activeDialog.resolve = resolve;
        });

        window.requestAnimationFrame(function () {
            var focusTarget = null;
            if (options.autoFocusSelector) {
                focusTarget = shell.querySelector(options.autoFocusSelector);
            }
            if (!focusTarget) {
                focusTarget = shell.querySelector("[data-dialog-autofocus='true']");
            }
            if (!focusTarget) {
                var focusable = getFocusableElements(shell);
                focusTarget = focusable[0] || shell;
            }
            if (focusTarget && typeof focusTarget.focus === "function") {
                focusTarget.focus();
            }
        });

        return promise;
    }

    function alertDialog(message, options) {
        options = options || {};
        return showDialog({
            title: options.title || "お知らせ",
            description: options.description || "",
            body: createElement("div", "esp-dialog-copy", message),
            actions: [{ label: options.okLabel || "閉じる", value: true, variant: "primary", autofocus: true }]
        });
    }

    function confirmDialog(options) {
        options = options || {};
        return showDialog({
            title: options.title || "確認",
            description: options.description || "",
            body: createElement("div", "esp-dialog-copy", options.message || ""),
            actions: [
                { label: options.cancelLabel || "キャンセル", value: false, variant: "secondary" },
                { label: options.confirmLabel || "実行する", value: true, variant: options.variant === "danger" ? "danger" : "primary", autofocus: true }
            ]
        }).then(function (value) {
            return value === true;
        });
    }

    function promptDialog(options) {
        options = options || {};
        var wrapper = createElement("form", "esp-dialog-form");
        var field = createElement("div", "esp-dialog-field");
        var label = createElement("label", "", options.label || options.message || "入力してください");
        label.setAttribute("for", "espPromptInput");
        var input = createElement("input");
        input.id = "espPromptInput";
        input.type = options.inputType || "text";
        input.value = options.value || "";
        input.placeholder = options.placeholder || "";
        if (options.required !== false) {
            input.required = true;
        }
        field.appendChild(label);
        field.appendChild(input);
        wrapper.appendChild(field);

        wrapper.addEventListener("submit", function (event) {
            event.preventDefault();
            closeActiveDialog("confirm");
        });

        return showDialog({
            title: options.title || "入力",
            description: options.description || "",
            body: wrapper,
            autoFocusSelector: "#espPromptInput",
            actions: [
                { label: options.cancelLabel || "キャンセル", value: "cancel", variant: "secondary" },
                { label: options.confirmLabel || "決定", value: "confirm", variant: "primary", autofocus: true }
            ]
        }).then(function (action) {
            if (action !== "confirm") {
                return null;
            }
            var value = String(input.value || "").trim();
            if (!value && options.required !== false) {
                toast(options.requiredMessage || "入力してください。", { type: "warning" });
                return null;
            }
            return value;
        });
    }

    function fallbackCopyDialog(text, label) {
        var wrapper = createElement("div", "esp-dialog-form");
        var field = createElement("div", "esp-dialog-field");
        var inputLabel = createElement("label", "", label || "コピー対象");
        inputLabel.setAttribute("for", "espCopyFallback");
        var input = createElement("input");
        input.id = "espCopyFallback";
        input.type = "text";
        input.readOnly = true;
        input.value = text;
        field.appendChild(inputLabel);
        field.appendChild(input);
        wrapper.appendChild(field);

        return showDialog({
            title: "コピーしてください",
            description: "クリップボードへ直接書き込めなかったため、手動コピー用のテキストを表示しています。",
            body: wrapper,
            autoFocusSelector: "#espCopyFallback",
            actions: [{ label: "閉じる", value: true, variant: "primary", autofocus: true }]
        }).then(function () {
            input.select();
        });
    }

    function copyText(text, options) {
        options = options || {};
        if (!text) {
            return Promise.resolve(false);
        }

        if (navigator.clipboard && navigator.clipboard.writeText) {
            return navigator.clipboard.writeText(text).then(function () {
                toast(options.successMessage || "コピーしました。", { type: "success" });
                return true;
            }).catch(function () {
                return fallbackCopyDialog(text, options.label).then(function () {
                    return false;
                });
            });
        }

        return fallbackCopyDialog(text, options.label).then(function () {
            return false;
        });
    }

    function handleFlashToasts() {
        ensureToastViewport().querySelectorAll(".esp-toast").forEach(function (toastNode) {
            var closeButton = toastNode.querySelector(".esp-toast-close");
            if (closeButton) {
                closeButton.addEventListener("click", function () {
                    dismissToast(toastNode);
                });
            }
            if (toastNode.dataset.autodismiss === "true") {
                window.setTimeout(function () {
                    dismissToast(toastNode);
                }, 4200);
            }
        });
    }

    function bindCopyButtons() {
        document.addEventListener("click", function (event) {
            var button = event.target.closest("[data-copy-text]");
            if (!button) {
                return;
            }
            event.preventDefault();
            copyText(button.getAttribute("data-copy-text"), {
                successMessage: button.getAttribute("data-copy-success") || "コピーしました。",
                label: button.getAttribute("data-copy-label") || "コピー対象"
            });
        });
    }

    function bindPasswordToggles() {
        document.addEventListener("click", function (event) {
            var button = event.target.closest("[data-toggle-password]");
            if (!button) {
                return;
            }
            event.preventDefault();
            var target = document.querySelector(button.getAttribute("data-toggle-password"));
            if (!target) {
                return;
            }
            var nextType = target.type === "password" ? "text" : "password";
            target.type = nextType;
            button.setAttribute("aria-pressed", nextType === "text" ? "true" : "false");
            button.textContent = nextType === "text" ? (button.dataset.hideLabel || "非表示") : (button.dataset.showLabel || "表示");
        });
    }

    function bindConfirmForms() {
        document.addEventListener("submit", function (event) {
            var form = event.target;
            if (!(form instanceof HTMLFormElement)) {
                return;
            }

            var submitter = event.submitter || null;
            var confirmMessage = (submitter && submitter.dataset.confirmMessage) || form.dataset.confirmMessage;
            var confirmTitle = (submitter && submitter.dataset.confirmTitle) || form.dataset.confirmTitle;
            var confirmDescription = (submitter && submitter.dataset.confirmDescription) || form.dataset.confirmDescription;
            var feedbackLabel = (submitter && submitter.dataset.loadingLabel) || form.dataset.submitFeedback;

            if (form.dataset.espConfirmed === "true") {
                delete form.dataset.espConfirmed;
                if (submitter && feedbackLabel) {
                    setButtonBusy(submitter, true, feedbackLabel);
                }
                return;
            }

            if (!confirmMessage) {
                if (submitter && feedbackLabel) {
                    setButtonBusy(submitter, true, feedbackLabel);
                }
                return;
            }

            event.preventDefault();
            confirmDialog({
                title: confirmTitle || "確認",
                description: confirmDescription || "",
                message: confirmMessage,
                confirmLabel: (submitter && submitter.dataset.confirmLabel) || form.dataset.confirmLabel || "実行する",
                cancelLabel: (submitter && submitter.dataset.cancelLabel) || form.dataset.cancelLabel || "キャンセル",
                variant: (submitter && submitter.dataset.confirmVariant) || form.dataset.confirmVariant || "danger"
            }).then(function (confirmed) {
                if (!confirmed) {
                    return;
                }
                form.dataset.espConfirmed = "true";
                if (submitter && typeof form.requestSubmit === "function") {
                    form.requestSubmit(submitter);
                } else {
                    form.submit();
                }
            });
        });
    }

    document.addEventListener("DOMContentLoaded", function () {
        handleFlashToasts();
        bindCopyButtons();
        bindPasswordToggles();
        bindConfirmForms();
    });

    window.ESPUI = {
        alert: alertDialog,
        confirm: confirmDialog,
        copyText: copyText,
        hideLoading: hideLoading,
        prompt: promptDialog,
        setButtonBusy: setButtonBusy,
        showDialog: showDialog,
        showLoading: showLoading,
        toast: toast
    };
})();
