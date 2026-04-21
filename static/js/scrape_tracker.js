(function () {
    "use strict";

    var root = document.getElementById("globalScrapeTracker");
    if (!root || !window.fetch) {
        return;
    }

    var desktopContainer = document.getElementById("globalScrapeTrackerDesktop");
    var toolbarEl = document.getElementById("globalScrapeTrackerToolbar");
    var listEl = document.getElementById("globalScrapeTrackerList");
    var overflowEl = document.getElementById("globalScrapeTrackerOverflow");
    var pillEl = document.getElementById("globalScrapeTrackerPill");
    var pillBadgeEl = document.getElementById("globalScrapeTrackerPillBadge");
    var pillTitleEl = document.getElementById("globalScrapeTrackerPillTitle");
    var pillCountEl = document.getElementById("globalScrapeTrackerPillCount");
    var backdropEl = document.getElementById("globalScrapeTrackerBackdrop");
    var sheetEl = document.getElementById("globalScrapeTrackerSheet");
    var sheetHeaderEl = document.getElementById("globalScrapeTrackerSheetHeader");
    var sheetDismissAllEl = document.getElementById("globalScrapeTrackerSheetDismissAll");
    var sheetCloseEl = document.getElementById("globalScrapeTrackerSheetClose");
    var sheetCountEl = document.getElementById("globalScrapeTrackerSheetCount");
    var mobileListEl = document.getElementById("globalScrapeTrackerMobileList");
    var jobsUrl = root.dataset.jobsUrl;
    var dismissUrlTemplate = root.dataset.dismissUrlTemplate || "";
    var dismissBatchUrl = root.dataset.dismissBatchUrl || "";
    var userId = root.dataset.userId || "anonymous";
    var storageKey = "esp_scrape_tracker_dismissed_" + userId;
    var maxVisibleJobs = 3;
    var pollIntervalMs = 2000;
    var dismissTtlMs = 60 * 60 * 1000;
    var mobileMedia = window.matchMedia("(max-width: 1023px)");

    var state = {
        jobs: new Map(),
        dismissed: loadDismissed(),
        inFlight: false,
        pollTimer: null,
        isMobileViewport: mobileMedia.matches,
        mobileSheetOpen: false,
        visibleJobs: []
    };

    function nowMs() {
        return Date.now();
    }

    function getCsrfToken() {
        var csrfMeta = document.querySelector('meta[name="csrf-token"]');
        return csrfMeta ? String(csrfMeta.content || "").trim() : "";
    }

    function loadDismissed() {
        try {
            var parsed = JSON.parse(window.localStorage.getItem(storageKey) || "{}");
            if (!parsed || typeof parsed !== "object") {
                return {};
            }
            return parsed;
        } catch (error) {
            return {};
        }
    }

    function saveDismissed() {
        try {
            window.localStorage.setItem(storageKey, JSON.stringify(state.dismissed));
        } catch (error) {
            // Ignore storage errors and keep the tracker functional.
        }
    }

    function purgeDismissed() {
        var changed = false;
        var cutoff = nowMs() - dismissTtlMs;
        Object.keys(state.dismissed).forEach(function (jobId) {
            if (Number(state.dismissed[jobId]) < cutoff) {
                delete state.dismissed[jobId];
                changed = true;
            }
        });
        if (changed) {
            saveDismissed();
        }
    }

    function normalizeJob(job) {
        var normalized = Object.assign({}, job || {});
        normalized.context = normalized.context || {};
        normalized.result_summary = normalized.result_summary || null;
        normalized.created_at = Number(normalized.created_at || (nowMs() / 1000));
        normalized.elapsed_seconds = Number(normalized.elapsed_seconds || 0);
        return normalized;
    }

    function isTerminal(job) {
        return job.status === "completed" || job.status === "failed";
    }

    function isActive(job) {
        return job.status === "queued" || job.status === "running";
    }

    function isDismissed(jobId) {
        return Boolean(state.dismissed[jobId]);
    }

    function clearPolling() {
        if (state.pollTimer) {
            clearTimeout(state.pollTimer);
            state.pollTimer = null;
        }
    }

    function schedulePolling() {
        clearPolling();
        var shouldPoll = Array.from(state.jobs.values()).some(isActive);
        if (shouldPoll) {
            state.pollTimer = setTimeout(refreshNow, pollIntervalMs);
        }
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

    function getBadgeText(status) {
        if (status === "queued") {
            return "待機中";
        }
        if (status === "running") {
            return "抽出中";
        }
        if (status === "completed") {
            return "完了";
        }
        return "失敗";
    }

    function getTrackState(status) {
        if (status === "completed") {
            return "success";
        }
        if (status === "failed") {
            return "error";
        }
        return status;
    }

    function getRunningPhase(elapsedSeconds) {
        if (elapsedSeconds < 8) {
            return "検索結果ページから候補商品を集めています。";
        }
        if (elapsedSeconds < 18) {
            return "商品ページを順番に確認しています。";
        }
        if (elapsedSeconds < 35) {
            return "取得した候補を整えて、表示できる形にまとめています。";
        }
        return "最後の候補まで確認しています。別の操作を続けながら待てます。";
    }

    function getTitle(job) {
        if (job.status === "queued") {
            return "キューで順番待ちです";
        }
        if (job.status === "running") {
            return "商品を抽出しています";
        }
        if (job.status === "completed") {
            return "抽出が完了しました";
        }
        return "抽出に失敗しました";
    }

    function getSubtitle(job) {
        return job.context.detail_label || job.context.site_label || "商品抽出";
    }

    function getCompletedSummary(job) {
        if (!job.result_summary) {
            return "結果を確認できます。";
        }
        return "抽出 " + (job.result_summary.items_count || 0) + "件 / 除外 " + (job.result_summary.excluded_count || 0) + "件";
    }

    function getPhase(job) {
        if (job.status === "queued") {
            if (job.queue_position) {
                return job.queue_position + "番目で待機しています。前のジョブが終わり次第、抽出を開始します。";
            }
            return "ジョブをキューへ登録しています。";
        }
        if (job.status === "running") {
            return getRunningPhase(job.elapsed_seconds);
        }
        if (job.status === "completed") {
            return getCompletedSummary(job);
        }
        return job.error || "不明なエラー";
    }

    function getMobilePhase(job) {
        if (job.status === "queued") {
            return job.queue_position
                ? job.queue_position + "番目で待機中です。"
                : "ジョブをキューに登録しています。";
        }
        if (job.status === "running") {
            return "抽出処理を進めています。";
        }
        if (job.status === "completed") {
            return getCompletedSummary(job);
        }
        return job.error || "不明なエラー";
    }

    function removeDismissal(jobId) {
        if (!state.dismissed[jobId]) {
            return;
        }
        delete state.dismissed[jobId];
        saveDismissed();
    }

    function buildDismissUrl(jobId) {
        if (!dismissUrlTemplate) {
            return "";
        }
        return dismissUrlTemplate.replace("__JOB_ID__", encodeURIComponent(jobId));
    }

    function dismissJob(jobId) {
        var job = state.jobs.get(jobId);
        if (!job || !isTerminal(job)) {
            return;
        }

        state.dismissed[jobId] = nowMs();
        saveDismissed();
        state.jobs.delete(jobId);
        render();
        schedulePolling();

        var dismissUrl = buildDismissUrl(jobId);
        if (!dismissUrl) {
            return;
        }

        fetch(dismissUrl, {
            method: "POST",
            headers: {
                "Accept": "application/json",
                "X-CSRFToken": getCsrfToken()
            }
        }).catch(function () {
            // Keep the local dismissal even if the server-side ack fails.
        });
    }

    function getDismissibleJobIds() {
        return state.visibleJobs
            .filter(function (job) {
                return isTerminal(job);
            })
            .map(function (job) {
                return job.job_id;
            });
    }

    function dismissVisibleJobs() {
        var jobIds = getDismissibleJobIds();
        if (!jobIds.length) {
            return;
        }

        jobIds.forEach(function (jobId) {
            state.dismissed[jobId] = nowMs();
            state.jobs.delete(jobId);
        });
        saveDismissed();
        render();
        schedulePolling();

        if (!dismissBatchUrl) {
            return;
        }

        fetch(dismissBatchUrl, {
            method: "POST",
            headers: {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-CSRFToken": getCsrfToken()
            },
            body: JSON.stringify({ job_ids: jobIds })
        }).catch(function () {
            // Keep the local dismissal even if the server-side ack fails.
        });
    }

    function sortJobs(jobs) {
        return jobs.sort(function (left, right) {
            return Number(right.created_at || 0) - Number(left.created_at || 0);
        });
    }

    function getPriorityJob(jobs) {
        var activeJobs = jobs.filter(isActive);
        if (activeJobs.length) {
            return activeJobs[0];
        }
        return jobs[0] || null;
    }

    function updateMobileSheetState(isOpen) {
        state.mobileSheetOpen = Boolean(isOpen && state.isMobileViewport && state.visibleJobs.length);
        root.classList.toggle("is-mobile-open", state.mobileSheetOpen);
        pillEl.setAttribute("aria-expanded", state.mobileSheetOpen ? "true" : "false");
        sheetEl.setAttribute("aria-hidden", state.mobileSheetOpen ? "false" : "true");
        sheetEl.setAttribute("aria-modal", state.mobileSheetOpen ? "true" : "false");
        backdropEl.setAttribute("aria-hidden", state.mobileSheetOpen ? "false" : "true");
        document.body.classList.toggle("scrape-tracker-sheet-open", state.mobileSheetOpen);
    }

    function closeSidebarIfOpen() {
        if (!document.body.classList.contains("sidebar-open")) {
            return;
        }
        if (typeof window.closeSidebar === "function") {
            window.closeSidebar();
        }
    }

    function openMobileSheet() {
        closeSidebarIfOpen();
        updateMobileSheetState(true);
    }

    function closeMobileSheet() {
        updateMobileSheetState(false);
    }

    function buildCard(job, variant) {
        var trackState = getTrackState(job.status);
        var articleClass = "scrape-progress-card scrape-tracker-card";
        if (variant === "mobile") {
            articleClass += " is-mobile-sheet";
        }
        var article = createElement("article", articleClass);
        var shell = createElement("div", "scrape-progress-shell");
        var head = createElement("div", "scrape-progress-head");
        var badge = createElement("span", "scrape-progress-badge is-" + trackState, getBadgeText(job.status));
        var headActions = createElement("div", "scrape-tracker-head-actions");

        if (job.result_url) {
            var resultLink = createElement("a", "scrape-tracker-link", "結果を見る");
            resultLink.href = job.result_url;
            headActions.appendChild(resultLink);
        }

        if (isTerminal(job)) {
            var dismissButton = createElement("button", "scrape-tracker-dismiss", "閉じる");
            dismissButton.type = "button";
            dismissButton.addEventListener("click", function () {
                dismissJob(job.job_id);
            });
            headActions.appendChild(dismissButton);
        }

        head.appendChild(badge);
        head.appendChild(headActions);

        var main = createElement("div", "scrape-progress-main");
        var spinner = createElement("div", "loading-spinner scrape-progress-spinner");
        spinner.setAttribute("aria-hidden", "true");
        if (isTerminal(job)) {
            spinner.hidden = true;
        }

        var copy = createElement("div", "scrape-progress-copy");
        var title = createElement("p", "scrape-progress-title", getTitle(job));
        var subtitle = createElement("p", "scrape-progress-subtitle", getSubtitle(job));
        copy.appendChild(title);
        copy.appendChild(subtitle);
        main.appendChild(spinner);
        main.appendChild(copy);

        var track = createElement("div", "scrape-progress-track");
        var trackFill = createElement("div", "scrape-progress-track-fill is-" + trackState);
        track.appendChild(trackFill);

        var details = createElement("div", "scrape-progress-details");
        var meta = createElement("div", "scrape-progress-meta");
        meta.appendChild(createElement("span", "", job.context.site_label || "商品抽出"));
        meta.appendChild(createElement("span", "", job.context.limit_label || "件数未設定"));
        meta.appendChild(createElement("span", "", "経過 " + job.elapsed_seconds + "秒"));

        var phaseText = variant === "mobile" ? getMobilePhase(job) : getPhase(job);
        var phase = createElement("p", "scrape-progress-phase", phaseText);

        details.appendChild(meta);
        details.appendChild(phase);

        shell.appendChild(head);
        shell.appendChild(main);
        shell.appendChild(track);
        shell.appendChild(details);
        article.appendChild(shell);
        return article;
    }

    function renderDesktop(visibleJobs) {
        var dismissibleCount = visibleJobs.filter(isTerminal).length;

        if (toolbarEl) {
            toolbarEl.hidden = dismissibleCount <= 1;
        }
        listEl.innerHTML = "";

        visibleJobs.slice(0, maxVisibleJobs).forEach(function (job) {
            listEl.appendChild(buildCard(job, "desktop"));
        });

        var overflowCount = visibleJobs.length - maxVisibleJobs;
        if (overflowCount > 0) {
            overflowEl.hidden = false;
            overflowEl.textContent = "他 " + overflowCount + " 件の抽出ジョブがあります。";
        } else {
            overflowEl.hidden = true;
            overflowEl.textContent = "";
        }
    }

    function renderMobilePill(visibleJobs) {
        var priorityJob = getPriorityJob(visibleJobs);
        var trackState = getTrackState(priorityJob.status);
        pillBadgeEl.className = "scrape-progress-badge global-scrape-tracker-pill-badge is-" + trackState;
        pillBadgeEl.textContent = getBadgeText(priorityJob.status);
        pillTitleEl.textContent = getSubtitle(priorityJob);
        pillCountEl.textContent = visibleJobs.length === 1 ? "1件" : "全" + visibleJobs.length + "件";
    }

    function renderMobileSheet(visibleJobs) {
        var dismissibleCount = visibleJobs.filter(isTerminal).length;
        mobileListEl.innerHTML = "";
        visibleJobs.forEach(function (job) {
            mobileListEl.appendChild(buildCard(job, "mobile"));
        });
        sheetCountEl.textContent = visibleJobs.length === 1 ? "1件" : visibleJobs.length + "件";
        if (sheetDismissAllEl) {
            sheetDismissAllEl.hidden = dismissibleCount <= 1;
        }
    }

    function render() {
        purgeDismissed();

        var visibleJobs = sortJobs(
            Array.from(state.jobs.values()).filter(function (job) {
                return !(isTerminal(job) && isDismissed(job.job_id));
            })
        );

        state.visibleJobs = visibleJobs;

        if (!visibleJobs.length) {
            if (toolbarEl) {
                toolbarEl.hidden = true;
            }
            if (sheetDismissAllEl) {
                sheetDismissAllEl.hidden = true;
            }
            listEl.innerHTML = "";
            mobileListEl.innerHTML = "";
            overflowEl.hidden = true;
            overflowEl.textContent = "";
            closeMobileSheet();
            root.hidden = true;
            return;
        }

        root.hidden = false;
        state.isMobileViewport = mobileMedia.matches;

        renderDesktop(visibleJobs);
        renderMobilePill(visibleJobs);
        renderMobileSheet(visibleJobs);

        desktopContainer.hidden = state.isMobileViewport;
        pillEl.hidden = !state.isMobileViewport;

        if (!state.isMobileViewport) {
            closeMobileSheet();
        } else {
            updateMobileSheetState(state.mobileSheetOpen);
        }
    }

    function applyServerJobs(jobs) {
        state.jobs = new Map();
        (jobs || []).forEach(function (job) {
            var normalized = normalizeJob(job);
            state.jobs.set(normalized.job_id, normalized);
        });
        render();
        schedulePolling();
    }

    function refreshNow() {
        if (!jobsUrl || state.inFlight) {
            return Promise.resolve();
        }

        state.inFlight = true;
        return fetch(jobsUrl, {
            headers: { "Accept": "application/json" }
        })
            .then(function (response) {
                if (!response.ok) {
                    throw new Error("ジョブ一覧の取得に失敗しました");
                }
                return response.json();
            })
            .then(function (payload) {
                applyServerJobs(payload.jobs || []);
            })
            .catch(function () {
                schedulePolling();
            })
            .finally(function () {
                state.inFlight = false;
            });
    }

    function registerJob(jobSummary) {
        if (!jobSummary || !jobSummary.job_id) {
            return;
        }
        var job = normalizeJob(jobSummary);
        removeDismissal(job.job_id);
        state.jobs.set(job.job_id, job);
        render();
        schedulePolling();
        refreshNow();
    }

    function handleViewportChange(event) {
        state.isMobileViewport = event.matches;
        if (!state.isMobileViewport) {
            closeMobileSheet();
        }
        render();
    }

    function handleSidebarStateChange(event) {
        var isOpen = Boolean(event && event.detail && event.detail.open);
        if (isOpen) {
            closeMobileSheet();
        }
    }

    pillEl.addEventListener("click", function () {
        if (!state.visibleJobs.length || !state.isMobileViewport) {
            return;
        }
        openMobileSheet();
    });

    backdropEl.addEventListener("click", closeMobileSheet);
    sheetCloseEl.addEventListener("click", function (event) {
        event.stopPropagation();
        closeMobileSheet();
    });
    if (toolbarEl) {
        toolbarEl.addEventListener("click", function (event) {
            event.stopPropagation();
        });
    }
    if (sheetDismissAllEl) {
        sheetDismissAllEl.addEventListener("click", function (event) {
            event.stopPropagation();
            dismissVisibleJobs();
        });
    }
    var dismissAllButton = document.getElementById("globalScrapeTrackerDismissAll");
    if (dismissAllButton) {
        dismissAllButton.addEventListener("click", function () {
            dismissVisibleJobs();
        });
    }
    sheetHeaderEl.addEventListener("click", closeMobileSheet);
    sheetHeaderEl.addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            closeMobileSheet();
        }
    });
    document.addEventListener("keydown", function (event) {
        if (event.key === "Escape" && state.mobileSheetOpen) {
            closeMobileSheet();
        }
    });
    window.addEventListener("esp:sidebar-state", handleSidebarStateChange);

    if (typeof mobileMedia.addEventListener === "function") {
        mobileMedia.addEventListener("change", handleViewportChange);
    } else if (typeof mobileMedia.addListener === "function") {
        mobileMedia.addListener(handleViewportChange);
    }

    window.ESPScrapeTracker = {
        registerJob: registerJob,
        refreshNow: refreshNow,
        dismissJob: dismissJob,
        dismissVisibleJobs: dismissVisibleJobs
    };

    refreshNow();
})();
