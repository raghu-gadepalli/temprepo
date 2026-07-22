//------------------------------------------------
// OMS DASHBOARD
//------------------------------------------------

(function () {
    "use strict";

    const CFG = window.OMS_DASHBOARD_CONFIG || {
        dataUrl: "/oms/dashboard/data",
        refreshMs: 10000
    };

    function formatNumber(value, digits = 0) {
        const n = Number(value);
        if (!Number.isFinite(n)) return "—";

        return new Intl.NumberFormat("en-IN", {
            minimumFractionDigits: digits,
            maximumFractionDigits: digits
        }).format(n);
    }

    function formatCurrency(value) {
        const n = Number(value);
        if (!Number.isFinite(n)) return "₹ —";
        return "₹ " + formatNumber(n, 2);
    }

    function setText(id, value) {
        const el = document.getElementById(id);
        if (!el) return;
        el.textContent = value;
    }

    function setSignedCurrency(id, value) {
        const el = document.getElementById(id);
        if (!el) return;

        const n = Number(value);
        el.textContent = formatCurrency(n);

        el.classList.remove("text-success", "text-danger");
        el.classList.add(n >= 0 ? "text-success" : "text-danger");
    }

    function showError(message) {
        const el = document.getElementById("oms-dashboard-alert");
        if (!el) return;

        el.textContent = message || "Unable to load OMS dashboard data.";
        el.classList.remove("d-none");
    }

    function clearError() {
        const el = document.getElementById("oms-dashboard-alert");
        if (!el) return;

        el.textContent = "";
        el.classList.add("d-none");
    }

    function renderDashboard(data) {
        setText("active_clients", formatNumber(data.active_clients || 0));
        setText("total_clients", formatNumber(data.total_clients || 0));
        setText("open_orders", formatNumber(data.open_orders || 0));
        setText("total_positions", formatNumber(data.total_positions || 0));
        setText("open_positions", formatNumber(data.open_positions || 0));

        setSignedCurrency("total_funds", data.total_funds || 0);
        setSignedCurrency("today_pnl", data.today_pnl || 0);
    }

    async function loadDashboard() {
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
                throw new Error(payload.message || "Dashboard fetch failed");
            }

            clearError();
            renderDashboard(payload.data || {});
        } catch (err) {
            console.error("OMS dashboard load failed:", err);
            showError("Unable to load OMS dashboard data.");
        }
    }

    function bindRefresh() {
        const btn = document.getElementById("oms-dashboard-refresh");
        if (!btn) return;

        btn.addEventListener("click", function () {
            loadDashboard();
        });
    }

    function bindCardNavigation() {
        document.querySelectorAll(".kpi-card[data-href]").forEach(card => {
            card.addEventListener("click", function () {
                const href = this.dataset.href;
                if (href) {
                    window.location.href = href;
                }
            });
        });
    }

    document.addEventListener("DOMContentLoaded", function () {
        bindRefresh();
        bindCardNavigation();
        loadDashboard();
        window.setInterval(loadDashboard, Number(CFG.refreshMs || 10000));
    });
})();