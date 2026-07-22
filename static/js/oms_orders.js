//------------------------------------------------
// OMS ORDERS
//------------------------------------------------

(function () {
    "use strict";

    const CFG = window.OMS_ORDERS_CONFIG || {
        dataUrl: "/oms/orders/data",
        cancelUrl: "/oms/cancel-order",
        refreshMs: 30000
    };

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

    function showToast(message, type = "success") {
        const toastEl = document.getElementById("omsToast");
        const body = document.getElementById("toastMessage");

        if (!toastEl || !body) return;

        body.textContent = message || "Done";

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
        const el = document.getElementById("oms-orders-alert");
        if (!el) return;

        el.textContent = message || "Unable to load OMS orders data.";
        el.classList.remove("d-none");
    }

    function clearError() {
        const el = document.getElementById("oms-orders-alert");
        if (!el) return;

        el.textContent = "";
        el.classList.add("d-none");
    }

    function filterRows(rows, searchText) {
        const q = String(searchText || "").trim().toLowerCase();
        if (!q) return rows || [];

        return (rows || []).filter(row => {
            const hay = [
                row.client_id,
                row.client_name,
                row.order_id,
                row.exchange_order_id,
                row.tradingsymbol,
                row.exchange,
                row.transaction_type,
                row.product,
                row.status,
                row.order_type,
                row.variety,
                row.quantity,
                row.filled_quantity,
                row.pending_quantity,
                row.price,
                row.average_price,
                row.order_timestamp
            ].join(" ").toLowerCase();

            return hay.includes(q);
        });
    }

    function groupRows(rows) {
        const grouped = new Map();

        (rows || []).forEach(row => {
            const key = String(row.client_id || row.client_name || "UNKNOWN").trim();
            const clientName = String(row.client_name || row.client_id || "Unknown Client").trim();

            if (!grouped.has(key)) {
                grouped.set(key, {
                    client_id: row.client_id,
                    client_name: clientName,
                    orders: []
                });
            }

            grouped.get(key).orders.push(row);
        });

        return Array.from(grouped.values()).sort((a, b) =>
            String(a.client_name).localeCompare(String(b.client_name))
        );
    }

    function sideBadge(side) {
        const s = String(side || "").toUpperCase();
        const cls = s === "BUY" ? "badge-buy" : "badge-sell";
        return `<span class="badge ${cls}">${esc(s || "—")}</span>`;
    }

    function statusBadge(status) {
        const s = String(status || "").toUpperCase();
        let cls = "badge-cancelled";

        if (["COMPLETE", "COMPLETED"].includes(s)) cls = "badge-complete";
        else if (["OPEN", "TRIGGER PENDING"].includes(s)) cls = "badge-open";
        else if (["REJECTED"].includes(s)) cls = "badge-rejected";

        return `<span class="badge order-status ${cls}">${esc(status || "—")}</span>`;
    }

    function canAct(order) {
        const s = String(order.status || "").toUpperCase();
        return !["COMPLETE", "COMPLETED", "CANCELLED", "REJECTED", "CANCELLED AMO"].includes(s);
    }

    function renderOrders(rows) {
        const container = document.getElementById("oms-orders-container");
        if (!container) return;

        const searchVal = document.getElementById("oms-orders-search")?.value || "";
        const filtered = filterRows(rows, searchVal);
        const groups = groupRows(filtered);

        if (!groups.length) {
            container.innerHTML = `
                <div class="p-3 text-muted small">
                    No orders found.
                </div>
            `;
            return;
        }

        const html = groups.map((group, idx) => {
            const collapseId = `omsOrdersGroup${idx + 1}`;

            const rowsHtml = group.orders.map(o => {
                const displayPrice = Number(o.average_price) > 0 ? o.average_price : o.price;
                const actionsHtml = canAct(o)
                    ? `
                        <button
                            type="button"
                            class="btn btn-sm btn-warning js-modify-order"
                            data-client="${esc(o.client_id)}"
                            data-order-id="${esc(o.order_id)}"
                            data-variety="${esc(o.variety)}"
                            data-quantity="${esc(o.quantity)}"
                            data-price="${esc(o.average_price || o.price || "")}"
                            disabled
                            title="Modify wiring can be added next"
                        >
                            Modify
                        </button>

                        <button
                            type="button"
                            class="btn btn-sm btn-danger js-cancel-order"
                            data-client="${esc(o.client_id)}"
                            data-order-id="${esc(o.order_id)}"
                            data-variety="${esc(o.variety)}"
                        >
                            Cancel
                        </button>
                    `
                    : "";

                return `
                    <tr>
                        <td>${esc(text(o.order_timestamp))}</td>
                        <td class="text-center">${sideBadge(o.transaction_type)}</td>
                        <td>
                            ${esc(text(o.tradingsymbol))}
                            <span class="exchange">${esc(text(o.exchange, ""))}</span>
                        </td>
                        <td>${esc(text(o.product))}</td>
                        <td class="text-end">
                            ${esc(text(o.filled_quantity ?? 0, "0"))} / ${esc(text(o.quantity, "0"))}
                        </td>
                        <td class="text-end">${esc(num(displayPrice, 2))}</td>
                        <td class="text-center">${statusBadge(o.status)}</td>
                        <td class="text-center">${actionsHtml}</td>
                    </tr>
                `;
            }).join("");

            return `
                <div class="accordion mb-2" id="omsOrdersAccordion${idx + 1}">
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
                                <div class="w-100 d-flex justify-content-between pe-3">
                                    <div>
                                        <strong>${esc(group.client_name)}</strong>
                                        &nbsp;&nbsp;
                                        Orders: ${group.orders.length}
                                    </div>
                                </div>
                            </button>
                        </h2>

                        <div id="${collapseId}" class="accordion-collapse collapse">
                            <div class="accordion-body">
                                <div class="table-responsive">
                                    <table class="table table-sm orders-table align-middle mb-0">
                                        <thead class="oms-table-header">
                                            <tr>
                                                <th>Time</th>
                                                <th>Type</th>
                                                <th>Instrument</th>
                                                <th>Product</th>
                                                <th class="text-end">Qty</th>
                                                <th class="text-end">Price</th>
                                                <th class="text-center">Status</th>
                                                <th class="text-center">Actions</th>
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

    async function loadOrders() {
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
                throw new Error(payload.message || "Orders fetch failed");
            }

            clearError();
            lastRows = payload.data || [];
            
            renderOrders(lastRows);
            return;
            renderOrders(lastRows);
        } catch (err) {
            console.error("OMS orders load failed:", err);
            showError("Unable to load OMS orders data.");
            showToast("Unable to load OMS orders data.", "error");
        }
    }

    async function cancelOrder(clientId, orderId, variety, btn) {
        const ok = await showConfirm(`Cancel order ${orderId}?`);
        if (!ok) return;

        const originalText = btn ? btn.textContent : "Cancel";

        if (btn) {
            btn.disabled = true;
            btn.textContent = "Cancelling...";
        }

        try {
            const res = await fetch(CFG.cancelUrl, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json"
                },
                credentials: "same-origin",
                body: JSON.stringify({
                    client_id: clientId,
                    order_id: orderId,
                    variety: variety
                })
            });

            const data = await res.json();

            if (!res.ok || data.status === "error") {
                throw new Error(data.message || "Cancel failed");
            }

            showToast(data.message || "Order cancelled successfully", "success");
            await loadOrders();
        } catch (err) {
            console.error(err);
            showToast(err.message || "Cancel failed", "error");

            if (btn) {
                btn.disabled = false;
                btn.textContent = originalText;
            }
        }
    }

    function bindEvents() {
        const search = document.getElementById("oms-orders-search");
        if (search) {
            search.addEventListener("input", function () {
                renderOrders(lastRows);
            });
        }

        const refreshBtn = document.getElementById("oms-orders-refresh");
        if (refreshBtn) {
            refreshBtn.addEventListener("click", function () {
                loadOrders();
            });
        }

        document.addEventListener("click", async function (e) {
            const cancelBtn = e.target.closest(".js-cancel-order");
            if (cancelBtn) {
                await cancelOrder(
                    cancelBtn.dataset.client,
                    cancelBtn.dataset.orderId,
                    cancelBtn.dataset.variety,
                    cancelBtn
                );
                return;
            }

            const modifyBtn = e.target.closest(".js-modify-order");
            if (modifyBtn) {
                showToast("Modify wiring can be added next.", "info");
            }
        });
    }

    document.addEventListener("DOMContentLoaded", function () {
        bindEvents();
        loadOrders();
        window.setInterval(loadOrders, Number(CFG.refreshMs || 30000));
    });
})();