"use strict";

/*
 * Small progressive-navigation layer for the server-rendered operations UI.
 * It preserves Django as the authority for permissions, validation, messages,
 * and redirects, but replaces the content area for ordinary same-origin page
 * moves and scheduler-control forms. Login, logout, files, admin, and any
 * explicitly marked form/link retain normal browser navigation.
 */
(() => {
    let activeRequest = null;
    const messageTimers = new WeakMap();
    const closingOverlays = new WeakMap();
    let overlayScrollState = null;
    let timersStarted = false;
    let refreshTimer = null;

    const contentRoot = () => document.getElementById("app-content");
    const isSameOrigin = (url) => url.origin === window.location.origin;
    const bootstrapApi = () => window.bootstrap || null;

    const applyTheme = (theme) => {
        const root = document.documentElement;
        root.dataset.theme = theme;
        document.querySelector('meta[name="theme-color"]')?.setAttribute("content", theme === "night" ? "#050505" : "#102a43");
        const button = document.querySelector("[data-theme-toggle]");
        if (!button) return;
        const night = theme === "night";
        button.setAttribute("aria-label", night ? "Enable day theme" : "Enable night theme");
        button.setAttribute("title", night ? "Enable day theme" : "Enable night theme");
        const icon = button.querySelector("i");
        if (icon) {
            icon.classList.remove("bi-moon-stars", "bi-sun");
            icon.classList.add(night ? "bi-sun" : "bi-moon-stars");
        }
        const label = button.querySelector("span");
        if (label) label.textContent = night ? "Day theme" : "Night theme";
    };

    const initialiseTheme = () => {
        const button = document.querySelector("[data-theme-toggle]");
        const storageKey = "itrp-scheduler-theme";
        const isTheme = (value) => value === "day" || value === "night";
        const saveTheme = (theme) => {
            try {
                window.localStorage.setItem(storageKey, theme);
            } catch {
                // The switch still works when browser storage is unavailable.
            }
        };
        let savedTheme = null;
        try {
            savedTheme = window.localStorage.getItem(storageKey);
            if (!isTheme(savedTheme)) {
                // Keep the operator's preference across the portal rename.
                savedTheme = window.localStorage.getItem("sbi-scheduler-theme");
                if (isTheme(savedTheme)) saveTheme(savedTheme);
            }
        } catch {
            // Restricted browser profiles may deny even read access.
        }
        const prefersNight = window.matchMedia?.("(prefers-color-scheme: dark)").matches;
        applyTheme(isTheme(savedTheme) ? savedTheme : (prefersNight ? "night" : "day"));
        button?.addEventListener("click", () => {
            const next = document.documentElement.dataset.theme === "night" ? "day" : "night";
            saveTheme(next);
            applyTheme(next);
        });
    };

    const updateClock = () => {
        const time = new Intl.DateTimeFormat("en-IN", {
            timeZone: "Asia/Kolkata",
            hour: "2-digit",
            minute: "2-digit",
            second: "2-digit",
            hour12: false,
        }).format(new Date());
        document.querySelectorAll("[data-live-clock]").forEach((node) => { node.textContent = time; });
    };

    const pad = (number) => String(number).padStart(2, "0");
    const formatRemaining = (milliseconds) => {
        const seconds = Math.max(0, Math.floor(milliseconds / 1000));
        const hours = Math.floor(seconds / 3600);
        const minutes = Math.floor((seconds % 3600) / 60);
        return hours ? `${hours}h ${pad(minutes)}m ${pad(seconds % 60)}s` : `${minutes}m ${pad(seconds % 60)}s`;
    };

    const updateCountdowns = () => {
        const now = Date.now();
        document.querySelectorAll("[data-countdown-target]").forEach((node) => {
            const target = Date.parse(node.dataset.countdownTarget);
            if (Number.isNaN(target)) return;
            const remaining = target - now;
            node.textContent = remaining > 0
                ? `${node.dataset.countdownPrefix || "Time remaining"} ${formatRemaining(remaining)}`
                : (node.dataset.countdownFinished || "Time reached — refresh scheduler status");
        });
    };

    const dismissMessagesLater = () => {
        const seen = new Set();
        let visible = 0;
        [...document.querySelectorAll(".messages-stack .alert")].reverse().forEach((alert) => {
            const key = alert.textContent.trim().replace(/\s+/g, " ");
            if (seen.has(key) || visible >= 2) {
                window.clearTimeout(messageTimers.get(alert));
                alert.remove();
                return;
            }
            seen.add(key);
            visible += 1;
            if (!alert.matches(".alert-success, .alert-info") || messageTimers.has(alert)) return;
            // Preserve the original expiry across automatic page refreshes.
            const expiry = Number(alert.dataset.expiresAt) || Date.now() + 6000;
            alert.dataset.expiresAt = String(expiry);
            messageTimers.set(alert, window.setTimeout(() => {
                bootstrapApi()?.Alert?.getInstance(alert)?.dispose();
                alert.remove();
            }, Math.max(0, expiry - Date.now())));
        });
    };

    const initialiseRunbookBuilders = (scope) => {
        scope.querySelectorAll("[data-runbook-builder]").forEach((builder) => {
            if (builder.dataset.runbookReady === "true") return;
            builder.dataset.runbookReady = "true";
            const source = builder.querySelector("[data-runbook-source]");
            const list = builder.querySelector("[data-step-list]");
            const addButton = builder.querySelector("[data-add-step]");
            const form = builder.closest("form");
            if (!source || !list) return;

            const stepRows = () => [...list.querySelectorAll("[data-step-row]")];
            const sync = () => {
                source.value = stepRows()
                    .map((row) => row.querySelector("input")?.value.trim() || "")
                    .filter(Boolean)
                    .join("\n");
            };
            const renumber = () => stepRows().forEach((row, index) => {
                row.querySelector("[data-step-number]").textContent = index + 1;
                row.querySelector("[data-move-up]").disabled = index === 0;
                row.querySelector("[data-move-down]").disabled = index === stepRows().length - 1;
            });
            const addRow = (value = "") => {
                const row = document.createElement("div");
                row.className = "step-builder-row";
                row.dataset.stepRow = "";
                const number = document.createElement("span");
                number.className = "step-builder-number";
                number.dataset.stepNumber = "";
                const input = document.createElement("input");
                input.type = "text";
                input.className = "form-control form-control-sm";
                input.placeholder = "Describe this operating step";
                input.value = value;
                input.setAttribute("aria-label", "Operating step");
                input.addEventListener("input", sync);
                const actions = document.createElement("div");
                actions.className = "step-builder-actions";
                const makeAction = (icon, label, attribute) => {
                    const button = document.createElement("button");
                    button.type = "button";
                    button.className = "btn btn-sm btn-light border";
                    button.setAttribute("aria-label", label);
                    button.title = label;
                    button.dataset[attribute] = "";
                    button.innerHTML = `<i class="bi ${icon}"></i>`;
                    return button;
                };
                const up = makeAction("bi-arrow-up", "Move step up", "moveUp");
                const down = makeAction("bi-arrow-down", "Move step down", "moveDown");
                const remove = makeAction("bi-trash3", "Remove step", "removeStep");
                up.addEventListener("click", () => {
                    const previous = row.previousElementSibling;
                    if (previous) list.insertBefore(row, previous);
                    renumber();
                    sync();
                });
                down.addEventListener("click", () => {
                    const next = row.nextElementSibling;
                    if (next) list.insertBefore(next, row);
                    renumber();
                    sync();
                });
                remove.addEventListener("click", () => {
                    row.remove();
                    if (!stepRows().length) addRow();
                    renumber();
                    sync();
                });
                actions.append(up, down, remove);
                row.append(number, input, actions);
                list.append(row);
                renumber();
                return input;
            };

            source.value.split(/\r?\n/).map((value) => value.trim()).filter(Boolean).forEach(addRow);
            if (!stepRows().length) addRow();
            addButton?.addEventListener("click", () => addRow().focus());
            form?.addEventListener("submit", sync, true);
        });
    };

    const initialisePage = (scope = document) => {
        initialiseRunbookBuilders(scope);
        scope.querySelectorAll("[data-queue-editor]").forEach((form) => {
            const renumber = () => {
                const rows = [...form.querySelectorAll("[data-queue-row]")];
                rows.forEach((row, index) => {
                    row.querySelector("[data-queue-rank]").textContent = index + 1;
                    const up = row.querySelector("[data-queue-up]");
                    const down = row.querySelector("[data-queue-down]");
                    if (up) up.disabled = index === 0;
                    if (down) down.disabled = index === rows.length - 1;
                });
            };
            form.addEventListener("click", (event) => {
                const up = event.target.closest("[data-queue-up]");
                const down = event.target.closest("[data-queue-down]");
                const button = up || down;
                if (!button) return;
                const row = button.closest("[data-queue-row]");
                const sibling = up ? row.previousElementSibling : row.nextElementSibling;
                if (!sibling) return;
                if (up) row.parentElement.insertBefore(row, sibling);
                else row.parentElement.insertBefore(sibling, row);
                form.dataset.dirty = "true";
                form.querySelector("[data-queue-feedback]").textContent = "Unsaved order";
                renumber();
                button.focus();
            });
            renumber();
        });
        window.clearTimeout(refreshTimer);
        const refresh = () => {
            const marker = contentRoot()?.querySelector("[data-live-refresh]");
            if (!marker) return;
            if (!document.hidden && !activeRequest && !document.querySelector("dialog[open], .modal.show")
                && !contentRoot().querySelector("form[data-dirty='true']")
                && !document.activeElement?.matches("input, textarea, select, button")) {
                void loadDocument(window.location.href, { historyMode: "none" });
            } else refreshTimer = window.setTimeout(refresh, 20000);
        };
        if (scope.querySelector("[data-live-refresh]")) refreshTimer = window.setTimeout(refresh, 20000);
        updateClock();
        updateCountdowns();
        dismissMessagesLater();
        if (!timersStarted) {
            timersStarted = true;
            window.setInterval(updateClock, 1000);
            window.setInterval(updateCountdowns, 1000);
        }
    };

    const setBusy = (busy) => {
        document.body.classList.toggle("spa-loading", busy);
        const root = contentRoot();
        if (root) root.setAttribute("aria-busy", busy ? "true" : "false");
    };

    const setFormBusy = (form, submitter, busy) => {
        const button = submitter || form.querySelector("button[type='submit']");
        if (!button) return;
        if (busy) {
            if (!button.dataset.busyHtml) button.dataset.busyHtml = button.innerHTML;
            button.disabled = true;
            button.innerHTML = '<span class="spinner-border spinner-border-sm me-1" aria-hidden="true"></span>Processing';
        } else if (button.dataset.busyHtml) {
            button.disabled = false;
            button.innerHTML = button.dataset.busyHtml;
            delete button.dataset.busyHtml;
        }
    };

    const showClientError = (message) => {
        const root = contentRoot();
        if (!root) return;
        root.querySelector("[data-spa-error]")?.remove();
        const alert = document.createElement("div");
        alert.className = "alert alert-warning alert-dismissible fade show shadow-sm";
        alert.dataset.spaError = "true";
        alert.setAttribute("role", "alert");
        alert.textContent = message;
        const close = document.createElement("button");
        close.className = "btn-close";
        close.setAttribute("aria-label", "Dismiss notification");
        close.addEventListener("click", () => alert.remove());
        alert.append(close);
        let stack = root.querySelector(".messages-stack");
        if (!stack) { stack = document.createElement("div"); stack.className = "messages-stack"; root.prepend(stack); }
        stack.append(alert);
        dismissMessagesLater();
    };

    const captureOverlayScroll = () => {
        if (overlayScrollState) return;
        overlayScrollState = [...document.querySelectorAll("body, .fixed-top, .fixed-bottom, .is-fixed, .sticky-top")]
            .map((node) => ({ node, overflow: node.style.overflow, paddingRight: node.style.paddingRight, marginRight: node.style.marginRight }));
    };
    const clearOverlayArtifacts = () => {
        if (document.querySelector(".modal.show, .offcanvas.show, .offcanvas.showing, [aria-modal='true']")) return;
        document.querySelectorAll(".modal-backdrop, .offcanvas-backdrop").forEach((node) => node.remove());
        const wasLocked = document.body.classList.contains("modal-open");
        document.body.classList.remove("modal-open");
        if (overlayScrollState) {
            overlayScrollState.forEach(({ node, overflow, paddingRight, marginRight }) => {
                Object.assign(node.style, { overflow, paddingRight, marginRight });
                ["overflow", "padding-right", "margin-right"].forEach((property) => node.removeAttribute(`data-bs-${property}`));
            });
            overlayScrollState = null;
        } else if (wasLocked) {
            document.body.style.removeProperty("overflow");
            document.body.style.removeProperty("padding-right");
        }
    };
    document.addEventListener("show.bs.modal", captureOverlayScroll);
    document.addEventListener("show.bs.offcanvas", captureOverlayScroll);
    document.addEventListener("hidden.bs.modal", clearOverlayArtifacts);
    document.addEventListener("hidden.bs.offcanvas", clearOverlayArtifacts);

    const closeBootstrapOverlay = (element, component, kind) => {
        if (closingOverlays.has(element)) return closingOverlays.get(element);
        const instance = component?.getInstance(element);
        if (!element.matches(".show, .showing, .hiding, [aria-modal='true']")) return Promise.resolve();
        let complete;
        const closing = new Promise((resolve) => { complete = resolve; });
        closingOverlays.set(element, closing);
        let settled = false;
        let fallback;
        const hide = () => instance?.hide();
        const finish = (forced = false) => {
            if (settled) return;
            settled = true;
            window.clearTimeout(fallback);
            element.removeEventListener(`hidden.bs.${kind}`, hidden);
            element.removeEventListener(`shown.bs.${kind}`, hide);
            if (forced) {
                // A missing transition event must never trap the user behind
                // a stale backdrop or keep navigation waiting indefinitely.
                instance?.dispose();
                element.classList.remove("show", "showing", "hiding");
                element.removeAttribute("aria-modal");
                element.removeAttribute("role");
                element.setAttribute("aria-hidden", "true");
                if (kind === "modal") element.style.display = "none";
                if (element.contains(document.activeElement)) contentRoot()?.focus({ preventScroll: true });
            }
            clearOverlayArtifacts();
            closingOverlays.delete(element);
            complete();
        };
        const hidden = () => finish();
        element.addEventListener(`hidden.bs.${kind}`, hidden, { once: true });
        // A submit can arrive while the opening transition is finishing.
        element.addEventListener(`shown.bs.${kind}`, hide, { once: true });
        fallback = window.setTimeout(() => finish(true), 1500);
        if (instance) hide();
        else finish(true);
        return closing;
    };

    const disposeFloatingUi = async () => {
        const api = bootstrapApi();
        const modals = [...document.querySelectorAll("#app-content .modal")];
        const confirmation = document.getElementById("operation-confirm");
        if (confirmation?.open) confirmation.close("cancel");
        const navigation = document.getElementById("mobileNavigation");
        await Promise.all([
            ...modals.map((modal) => closeBootstrapOverlay(modal, api?.Modal, "modal")),
            ...(navigation ? [closeBootstrapOverlay(navigation, api?.Offcanvas, "offcanvas")] : []),
        ]);
        // Bootstrap's hidden event restores body overflow, scrollbar padding
        // and focus before its elements are disposed and the page is replaced.
        modals.forEach((modal) => api?.Modal?.getInstance(modal)?.dispose());
        clearOverlayArtifacts();
    };

    const syncNavigation = (nextDocument) => {
        ["[data-spa-nav-desktop]", "[data-spa-nav-mobile]"].forEach((selector) => {
            const current = document.querySelector(selector);
            const incoming = nextDocument.querySelector(selector);
            if (current && incoming) current.innerHTML = incoming.innerHTML;
        });
    };

    const updateHistory = (url, mode) => {
        if (mode === "none") return;
        if (mode === "replace" || url.href === window.location.href) {
            window.history.replaceState({ spa: true }, "", url.href);
        } else {
            window.history.pushState({ spa: true }, "", url.href);
        }
    };

    const loadDocument = async (requestedUrl, { method = "GET", body = null, historyMode = "push", form = null, submitter = null } = {}) => {
        const url = new URL(requestedUrl, window.location.href);
        if (!isSameOrigin(url)) {
            window.location.assign(url.href);
            return;
        }

        if (activeRequest?.form) setFormBusy(activeRequest.form, activeRequest.submitter, false);
        activeRequest?.controller.abort();
        const controller = new AbortController();
        const request = { controller, form, submitter, method, timedOut: false };
        activeRequest = request;
        setBusy(true);
        const timeout = window.setTimeout(() => {
            request.timedOut = true;
            controller.abort();
        }, 20000);

        try {
            const response = await window.fetch(url.href, {
                method,
                body,
                credentials: "same-origin",
                redirect: "follow",
                signal: controller.signal,
                headers: {
                    Accept: "text/html, application/xhtml+xml",
                    "X-Requested-With": "XMLHttpRequest",
                },
            });
            if (!response.ok) {
                showClientError(response.status === 403 ? "You do not have permission for this action. Ask the responsible operator or SUPERUSER." : "The request could not be completed. Refresh and try again.");
                if (form) setFormBusy(form, submitter, false);
                return;
            }
            const markup = await response.text();
            const nextDocument = new DOMParser().parseFromString(markup, "text/html");
            const nextRoot = nextDocument.getElementById("app-content");
            const finalUrl = new URL(response.url || url.href, window.location.href);
            // Fetch responses omit URL fragments. Keep the table destination
            // during paging, while allowing redirects to select their own page.
            if (method === "GET" && finalUrl.pathname === url.pathname && finalUrl.search === url.search) {
                finalUrl.hash = url.hash;
            }
            const currentIsAuthenticated = Boolean(document.querySelector(".topbar"));
            const nextIsAuthenticated = Boolean(nextDocument.querySelector(".topbar"));

            // Auth-shell transitions and non-HTML endpoints use the browser's
            // normal navigation so security/session boundaries stay explicit.
            if (!nextRoot || !isSameOrigin(finalUrl) || currentIsAuthenticated !== nextIsAuthenticated) {
                window.location.assign(finalUrl.href);
                return;
            }

            await disposeFloatingUi();
            if (activeRequest !== request || controller.signal.aborted) return;
            const root = contentRoot();
            if (!root) {
                window.location.assign(finalUrl.href);
                return;
            }
            const retainedMessages = method === "GET" && historyMode === "none"
                ? root.querySelector(".messages-stack") : null;
            root.innerHTML = nextRoot.innerHTML;
            if (retainedMessages?.children.length) {
                const incomingMessages = root.querySelector(".messages-stack");
                if (incomingMessages) incomingMessages.append(...retainedMessages.children);
                else root.prepend(retainedMessages);
            }
            document.title = nextDocument.title || document.title;
            syncNavigation(nextDocument);
            updateHistory(finalUrl, historyMode);
            initialisePage(root);

            if (method === "GET" && historyMode !== "none") {
                const pager = finalUrl.hash.startsWith("#pagination-")
                    ? document.getElementById(finalUrl.hash.slice(1)) : null;
                if (pager?.matches("[data-table-pagination]")) {
                    const tableSection = pager.closest(".card") || pager;
                    tableSection.classList.add("pagination-focus-target");
                    tableSection.tabIndex = -1;
                    tableSection.focus({ preventScroll: true });
                    tableSection.scrollIntoView({ block: "start" });
                } else {
                    window.scrollTo(0, 0);
                    root.focus({ preventScroll: true });
                }
            }
        } catch (error) {
            if (request.timedOut) {
                showClientError(method === "POST"
                    ? "The request timed out. It may still have been recorded. Check the task status before submitting it again."
                    : "The page took too long to respond. You can continue browsing and try again.");
            } else if (error.name !== "AbortError") {
                showClientError(method === "POST"
                    ? "The response could not be received. Check the task status before submitting the request again."
                    : "The page could not be updated. Please check the connection and try again.");
            }
        } finally {
            window.clearTimeout(timeout);
            if (form) setFormBusy(form, submitter, false);
            if (activeRequest === request) {
                activeRequest = null;
                setBusy(false);
            }
        }
    };

    const makeFormData = (form, submitter) => {
        try {
            return new FormData(form, submitter);
        } catch (_) {
            const data = new FormData(form);
            if (submitter?.name) data.append(submitter.name, submitter.value);
            return data;
        }
    };

    const visit = (url, historyMode = "push") => {
        const target = new URL(url, window.location.href);
        if (target.href === window.location.href) return;
        void loadDocument(target.href, { historyMode });
    };

    document.addEventListener("click", (event) => {
        if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
        const link = event.target.closest("a[href]");
        if (!link || link.dataset.noSpa !== undefined || link.target || link.hasAttribute("download")) return;
        const rawHref = link.getAttribute("href");
        if (!rawHref || rawHref.startsWith("#") || /^(mailto:|tel:|javascript:)/i.test(rawHref)) return;
        const target = new URL(link.href, window.location.href);
        if (!isSameOrigin(target) || target.pathname.startsWith("/admin/")) return;
        event.preventDefault();
        visit(target.href);
    });

    document.addEventListener("submit", (event) => {
        if (event.defaultPrevented) return;
        const form = event.target;
        if (!(form instanceof HTMLFormElement)) return;
        if (form.method.toLowerCase() === "dialog") return;
        if (form.method.toUpperCase() === "POST" && activeRequest?.method === "POST") {
            event.preventDefault();
            showClientError("A request is still being processed. Wait for its result before submitting another change.");
            return;
        }
        if (form.dataset.confirm && form.dataset.confirmed !== "true") {
            event.preventDefault();
            const dialog = document.getElementById("operation-confirm");
            if (dialog.open) return;
            const submitter = event.submitter;
            dialog.querySelector("[data-confirm-copy]").textContent = form.dataset.confirm;
            const reason = dialog.querySelector("textarea");
            reason.value = form.querySelector("[name=reason]")?.value || "";
            dialog.returnValue = "";
            dialog.addEventListener("close", () => {
                if (dialog.returnValue !== "confirm" || !form.isConnected) { submitter?.focus(); return; }
                let input = form.querySelector("[name=reason]");
                if (!input) { input = document.createElement("input"); input.type = "hidden"; input.name = "reason"; form.append(input); }
                input.value = reason.value;
                form.dataset.confirmed = "true";
                if (submitter) form.requestSubmit(submitter); else form.requestSubmit();
                delete form.dataset.confirmed;
            }, { once: true });
            dialog.showModal();
            return;
        }
        const submitter = event.submitter instanceof HTMLElement ? event.submitter : null;
        const skipsSpa = form.dataset.noSpa !== undefined
            || Boolean(form.target)
            || form.enctype === "multipart/form-data"
            || Boolean(form.querySelector("input[type='file']"));
        if (skipsSpa) {
            if (form.dataset.disableOnSubmit !== undefined && (form.noValidate || form.checkValidity())) setFormBusy(form, submitter, true);
            return;
        }

        const method = (form.method || "GET").toUpperCase();
        if (!/^(GET|POST)$/.test(method)) return;
        event.preventDefault();
        if (!form.noValidate && !submitter?.formNoValidate && !form.checkValidity()) {
            form.reportValidity();
            return;
        }
        setFormBusy(form, submitter, true);
        const action = new URL(form.action || window.location.href, window.location.href);
        const data = makeFormData(form, submitter);
        const modal = form.closest(".modal");
        if (method === "POST" && modal) {
            void closeBootstrapOverlay(modal, bootstrapApi()?.Modal, "modal");
        }
        if (method === "GET") {
            const query = new URLSearchParams();
            data.forEach((value, key) => {
                if (typeof value === "string") query.append(key, value);
            });
            action.search = query.toString();
            void loadDocument(action.href, { historyMode: "push", form, submitter });
        } else {
            void loadDocument(action.href, { method, body: data, historyMode: "replace", form, submitter });
        }
    });

    window.addEventListener("popstate", () => {
        void loadDocument(window.location.href, { historyMode: "none" });
    });

    document.addEventListener("input", (event) => {
        const form = event.target.closest("form");
        if (form) form.dataset.dirty = "true";
    });

    document.addEventListener("DOMContentLoaded", () => {
        initialiseTheme();
        initialisePage(document);
    });
})();
