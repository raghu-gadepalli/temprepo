//------------------------------------------------
// OMS FUNDS
//------------------------------------------------

(function () {
    "use strict";

    const CFG = window.OMS_FUNDS_CONFIG || {
        dataUrl: "/oms/funds/data",
        refreshMs: 30000
    };

    let table = null;

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

    function money(value, digits = 0) {
        const n = Number(value);
        if (!Number.isFinite(n)) return "—";

        return "₹ " + new Intl.NumberFormat("en-IN", {
            minimumFractionDigits: digits,
            maximumFractionDigits: digits
        }).format(n);
    }

    function signedMoney(value, digits = 0) {
        const n = Number(value);
        if (!Number.isFinite(n)) return "—";

        const display = money(n, digits);
        const cls = n >= 0 ? "text-success" : "text-danger";
        return `<span class="${cls}">${esc(display)}</span>`;
    }

    function badge(label, type) {
        const clsMap = {
            success: "text-bg-success",
            danger: "text-bg-danger",
            secondary: "text-bg-secondary",
            warning: "text-bg-warning"
        };
        return `<span class="badge ${clsMap[type] || clsMap.secondary}">${esc(label)}</span>`;
    }

    function fundsStatusBadge(status) {
        const s = String(status || "").toUpperCase();
        if (s === "LIVE") return badge("Live", "success");
        if (s === "NOT_REFRESHED") return badge("Not Refreshed", "warning");
        return badge("Not Logged In", "secondary");
    }

    function initTable() {
        if (!$.fn.DataTable) {
            console.error("DataTables is not available");
            return;
        }

        table = $("#oms-funds-table").DataTable({
            responsive: true,
            autoWidth: false,
            pageLength: 10,
            lengthChange: false,
            dom: "rtip",
            order: [[0, "asc"]]
        });
    }

    function showError(message) {
        const el = document.getElementById("oms-funds-alert");
        if (!el) return;

        el.textContent = message || "Unable to load OMS funds data.";
        el.classList.remove("d-none");
    }

    function clearError() {
        const el = document.getElementById("oms-funds-alert");
        if (!el) return;

        el.textContent = "";
        el.classList.add("d-none");
    }

    function populateFunds(rows) {
        if (!table) return;

        table.clear();

        (rows || []).forEach(function (f) {
            table.row.add([
                esc(text(f.client_name)),
                fundsStatusBadge(f.funds_status),
                money(f.net_balance, 0),
                money(f.available_cash, 0),
                money(f.utilised_margin, 0),
                money(f.option_premium, 0),
                signedMoney(f.m2m_realised, 0),
                signedMoney(f.m2m_unrealised, 0),
                esc(text(f.polled_at))
            ]);
        });

        table.draw(false);
    }

    async function loadFunds() {
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
                throw new Error(payload.message || "Funds fetch failed");
            }

            clearError();
            populateFunds(payload.data || []);
        } catch (err) {
            console.error("OMS funds load failed:", err);
            showError("Unable to load OMS funds data.");
        }
    }

    function bindSearch() {
        const input = document.getElementById("oms-funds-search");
        if (!input || !table) return;

        input.addEventListener("input", function () {
            table.search(this.value || "").draw();
        });
    }

    function bindLength() {
        const select = document.getElementById("oms-funds-length");
        if (!select || !table) return;

        select.addEventListener("change", function () {
            const len = parseInt(this.value, 10) || 10;
            table.page.len(len).draw();
        });
    }

    function bindRefresh() {
        const btn = document.getElementById("oms-funds-refresh");
        if (!btn) return;

        btn.addEventListener("click", function () {
            loadFunds();
        });
    }

    document.addEventListener("DOMContentLoaded", function () {
        initTable();
        bindSearch();
        bindLength();
        bindRefresh();
        loadFunds();
        window.setInterval(loadFunds, Number(CFG.refreshMs || 30000));
    });
})();