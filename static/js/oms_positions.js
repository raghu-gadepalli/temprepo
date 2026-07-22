//------------------------------------------------
// OMS POSITIONS
//------------------------------------------------

(function () {
    "use strict";

    const CFG = window.OMS_POSITIONS_CONFIG || {
        dataUrl: "/oms/positions/data",
        exitUrl: "/oms/exit_position",
        bulkExitUrl: "/oms/bulk_exit_positions",
        refreshMs: 30000
    };

    let selectAllState = false;
    let lastRows = [];

    function esc(value) {
        return String(value ?? "")
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    function text(value, fallback = "—") {
        const s = String(value ?? "").trim();
        return s ? s : fallback;
    }

    function num(value, digits = 2, fallback = "—") {
        const n = Number(value);
        if (!Number.isFinite(n)) return fallback;

        return new Intl.NumberFormat("en-IN", {
            minimumFractionDigits: digits,
            maximumFractionDigits: digits
        }).format(n);
    }

    function money(value, digits = 2, fallback = "—") {
        const n = Number(value);
        if (!Number.isFinite(n)) return fallback;
        return num(n, digits);
    }

    function signedMoneySpan(value, digits = 2) {
        const n = Number(value);
        if (!Number.isFinite(n)) return "—";

        const cls = n >= 0 ? "text-success" : "text-danger";
        return `<span class="${cls}">${esc(money(n, digits))}</span>`;
    }

    function showToast(message, type = "success") {
        const toastEl = document.getElementById("omsToast");
        const msgEl = document.getElementById("toastMessage");

        if (!toastEl || !msgEl) return;

        msgEl.textContent = message || "Done";

        toastEl.classList.remove(
            "text-bg-success",
            "text-bg-danger",
            "text-bg-warning",
            "text-bg-secondary"
        );

        const clsMap = {
            success: "text-bg-success",
            error: "text-bg-danger",
            warning: "text-bg-warning",
            info: "text-bg-secondary"
        };

        toastEl.classList.add(clsMap[type] || clsMap.success);

        const toast = new bootstrap.Toast(toastEl);
        toast.show();
    }

    function showConfirm(message) {
        return new Promise((resolve) => {
            const modalEl = document.getElementById("omsConfirmModal");
            const msgEl = document.getElementById("omsConfirmMessage");
            const okBtn = document.getElementById("omsConfirmOkBtn");

            if (!modalEl || !msgEl || !okBtn) {
                resolve(window.confirm(message));
                return;
            }

            msgEl.textContent = message || "Are you sure?";

            const modal = new bootstrap.Modal(modalEl);

            let resolved = false;

            function cleanup() {
                okBtn.onclick = null;
                modalEl.removeEventListener("hidden.bs.modal", onHidden);
            }

            function onHidden() {
                if (!resolved) {
                    resolved = true;
                    cleanup();
                    resolve(false);
                }
            }

            modalEl.addEventListener("hidden.bs.modal", onHidden);

            okBtn.onclick = function () {
                if (resolved) return;
                resolved = true;
                cleanup();
                modal.hide();
                resolve(true);
            };

            modal.show();
        });
    }

    function showError(message) {
        const el = document.getElementById("oms-positions-alert");
        if (!el) return;

        el.textContent = message || "Unable to load OMS positions data.";
        el.classList.remove("d-none");
    }

    function clearError() {
        const el = document.getElementById("oms-positions-alert");
        if (!el) return;

        el.textContent = "";
        el.classList.add("d-none");
    }

    function groupRows(rows) {
        const grouped = new Map();

        (rows || []).forEach(row => {
            const clientId = String(row.client_id || "").trim();
            const clientName = String(row.client_name || clientId || "Unknown Client").trim();
            const key = clientId || clientName;

            if (!grouped.has(key)) {
                grouped.set(key, {
                    client_id: clientId,
                    client_name: clientName,
                    positions: [],
                    total_pnl: 0
                });
            }

            const g = grouped.get(key);
            g.positions.push(row);

            const pnl = Number(row.pnl);
            if (Number.isFinite(pnl)) {
                g.total_pnl += pnl;
            }
        });

        return Array.from(grouped.values()).sort((a, b) =>
            String(a.client_name).localeCompare(String(b.client_name))
        );
    }

    function filterRows(rows, searchText) {
        const q = String(searchText || "").trim().toLowerCase();
        if (!q) return rows || [];

        return (rows || []).filter(row => {
            const hay = [
                row.client_id,
                row.client_name,
                row.tradingsymbol,
                row.product,
                row.exchange,
                row.quantity,
                row.average_price,
                row.last_price,
                row.pnl
            ].join(" ").toLowerCase();

            return hay.includes(q);
        });
    }

    function renderPositions(rows) {
        const container = document.getElementById("oms-positions-container");
        if (!container) return;

        const searchVal = document.getElementById("oms-positions-search")?.value || "";
        const filtered = filterRows(rows, searchVal);
        const groups = groupRows(filtered);

        if (!groups.length) {
            container.innerHTML = `
                <div class="p-3 text-muted small">
                    No positions found.
                </div>
            `;
            return;
        }

        const html = groups.map((group, idx) => {
            const collapseId = `omsPositionsGroup${idx + 1}`;
            const pnlClass = Number(group.total_pnl) >= 0 ? "text-success" : "text-danger";

            const rowsHtml = group.positions.map(p => {
                const qty = Number(p.quantity || 0);
                const qtyClass = qty > 0 ? "text-success" : (qty < 0 ? "text-danger" : "");
                const isOpen = qty !== 0;

                return `
                    <tr>
                        <td>
                            <input
                                type="checkbox"
                                class="row-select form-check-input"
                                data-client="${esc(p.client_id)}"
                                data-exchange="${esc(p.exchange)}"
                                data-product="${esc(p.product)}"
                                data-symbol="${esc(p.tradingsymbol)}"
                                data-qty="${esc(qty)}"
                                ${isOpen ? "" : "disabled"}
                            >
                        </td>
                        <td>${esc(text(p.product))}</td>
                        <td>${esc(text(p.tradingsymbol))}</td>
                        <td class="${qtyClass}">${esc(text(qty, "0"))}</td>
                        <td>${esc(num(p.average_price, 2))}</td>
                        <td>${esc(num(p.last_price, 2))}</td>
                        <td>${signedMoneySpan(p.pnl, 2)}</td>
                        <td class="text-center">
                            ${
                                isOpen
                                    ? `<button
                                            type="button"
                                            class="btn btn-sm btn-outline-danger js-exit-position"
                                            data-client="${esc(p.client_id)}"
                                            data-exchange="${esc(p.exchange)}"
                                            data-product="${esc(p.product)}"
                                            data-symbol="${esc(p.tradingsymbol)}"
                                            data-qty="${esc(qty)}"
                                       >
                                            Exit
                                       </button>`
                                    : ""
                            }
                        </td>
                    </tr>
                `;
            }).join("");

            return `
                <div class="accordion mb-2" id="omsPositionsAccordion${idx + 1}">
                    <div class="accordion-item">
                        <h2 class="accordion-header">
                            <button
                                class="accordion-button collapsed"
                                type="button"
                                data-bs-toggle="collapse"
                                data-bs-target="#${collapseId}"
                                aria-expanded="false"
                                aria-controls="${collapseId}"
                            >
                                <div class="w-100 d-flex justify-content-between align-items-center pe-3">
                                    <div>
                                        <strong>${esc(group.client_name)}</strong>
                                        &nbsp;&nbsp;
                                        Positions: ${group.positions.length}
                                    </div>
                                    <div class="${pnlClass}">
                                        P&amp;L: ${esc(num(group.total_pnl, 2))}
                                    </div>
                                </div>
                            </button>
                        </h2>

                        <div id="${collapseId}" class="accordion-collapse collapse">
                            <div class="accordion-body">

                                <div class="mb-2">
                                    <button
                                        type="button"
                                        class="btn btn-sm btn-outline-danger js-exit-client-selected"
                                        data-client="${esc(group.client_id)}"
                                    >
                                        Exit Selected
                                    </button>
                                </div>

                                <div class="table-responsive">
                                    <table class="table table-sm table-hover align-middle mb-0">
                                        <thead class="oms-table-header">
                                            <tr>
                                                <th>
                                                    <input
                                                        type="checkbox"
                                                        class="select-client form-check-input"
                                                        data-client="${esc(group.client_id)}"
                                                    >
                                                </th>
                                                <th>Product</th>
                                                <th>Instrument</th>
                                                <th>Qty</th>
                                                <th>Avg</th>
                                                <th>LTP</th>
                                                <th>P&amp;L</th>
                                                <th class="text-center">Action</th>
                                            </tr>
                                        </thead>
                                        <tbody>
                                            ${rowsHtml}
                                        </tbody>
                                    </table>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            `;
        }).join("");

        container.innerHTML = html;
    }

    async function loadPositions() {
        try {
            const response = await fetch(CFG.dataUrl, {
                method: "GET",
                headers: { "Accept": "application/json" },
                credentials: "same-origin"
            });

            if (!response.ok) {
                throw new Error(`HTTP ${response.status}`);
            }

            const payload = await response.json();

            if (payload.status !== "success") {
                throw new Error(payload.message || "Positions fetch failed");
            }

            clearError();
            lastRows = payload.data || [];
            renderPositions(lastRows);
        } catch (err) {
            console.error("OMS positions load failed:", err);
            showError("Unable to load OMS positions data.");
            showToast("Unable to load OMS positions data.", "error");
        }
    }

    async function submitBulkExit(payload) {
        try {
            const res = await fetch(CFG.bulkExitUrl, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                credentials: "same-origin",
                body: JSON.stringify(payload)
            });

            const data = await res.json();

            if (!res.ok || data.status === "error") {
                throw new Error(data.message || "Exit failed");
            }

            showToast(data.message || "Exit orders submitted", "success");
            await loadPositions();
        } catch (err) {
            console.error("Bulk exit failed:", err);
            showToast(err.message || "Exit failed", "error");
        }
    }

    async function exitSinglePosition(payload) {
        try {
            const res = await fetch(CFG.exitUrl, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                credentials: "same-origin",
                body: JSON.stringify(payload)
            });

            const data = await res.json();

            if (!res.ok || data.status === "error") {
                throw new Error(data.message || "Exit failed");
            }

            showToast(data.message || "Exit order submitted", "success");
            await loadPositions();
        } catch (err) {
            console.error("Single exit failed:", err);
            showToast(err.message || "Exit failed", "error");
        }
    }

    function getSelectedRows(selector) {
        return Array.from(document.querySelectorAll(selector)).map(el => ({
            client_id: el.dataset.client,
            exchange: el.dataset.exchange,
            product: el.dataset.product,
            tradingsymbol: el.dataset.symbol,
            quantity: el.dataset.qty
        }));
    }

    function bindEvents() {
        const search = document.getElementById("oms-positions-search");
        if (search) {
            search.addEventListener("input", function () {
                renderPositions(lastRows);
            });
        }

        const refreshBtn = document.getElementById("oms-positions-refresh");
        if (refreshBtn) {
            refreshBtn.addEventListener("click", function () {
                loadPositions();
            });
        }

        const selectAllBtn = document.getElementById("toggleSelectAllBtn");
        if (selectAllBtn) {
            selectAllBtn.addEventListener("click", function () {
                selectAllState = !selectAllState;

                document.querySelectorAll(".row-select").forEach(row => {
                    if (!row.disabled) {
                        row.checked = selectAllState;
                    }
                });

                this.classList.toggle("btn-primary", selectAllState);
                this.classList.toggle("btn-outline-primary", !selectAllState);
            });
        }

        const globalExitBtn = document.getElementById("oms-exit-selected-global");
        if (globalExitBtn) {
            globalExitBtn.addEventListener("click", async function () {
                const payload = getSelectedRows(".row-select:checked");

                if (!payload.length) {
                    showToast("No positions selected", "warning");
                    return;
                }

                const ok = await showConfirm(`Exit ${payload.length} positions across clients?`);
                if (!ok) return;

                submitBulkExit(payload);
            });
        }

        document.addEventListener("change", function (e) {
            if (e.target.classList.contains("select-client")) {
                const client = e.target.dataset.client;

                document.querySelectorAll(`.row-select[data-client='${client}']`).forEach(row => {
                    if (!row.disabled) {
                        row.checked = e.target.checked;
                    }
                });
            }
        });

        document.addEventListener("click", async function (e) {
            const singleBtn = e.target.closest(".js-exit-position");
            if (singleBtn) {
                const payload = {
                    client_id: singleBtn.dataset.client,
                    exchange: singleBtn.dataset.exchange,
                    product: singleBtn.dataset.product,
                    tradingsymbol: singleBtn.dataset.symbol,
                    quantity: singleBtn.dataset.qty
                };

                const ok = await showConfirm(`Exit position ${payload.tradingsymbol} (${payload.quantity}) ?`);
                if (!ok) return;

                exitSinglePosition(payload);
                return;
            }

            const clientExitBtn = e.target.closest(".js-exit-client-selected");
            if (clientExitBtn) {
                const client = clientExitBtn.dataset.client;
                const payload = getSelectedRows(`.row-select[data-client='${client}']:checked`);

                if (!payload.length) {
                    showToast("No positions selected", "warning");
                    return;
                }

                const ok = await showConfirm(`Exit ${payload.length} positions for this client?`);
                if (!ok) return;

                submitBulkExit(payload);
            }
        });
    }

    document.addEventListener("DOMContentLoaded", function () {
        bindEvents();
        loadPositions();
        window.setInterval(loadPositions, Number(CFG.refreshMs || 30000));
    });
})();