//------------------------------------------------
// OMS CLIENTS
//------------------------------------------------

(function () {
    "use strict";

    const CFG = window.OMS_CLIENTS_CONFIG || {
        dataUrl: "/oms/clients/data",
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

    function yesNoBadge(flag) {
        const on = Boolean(Number(flag)) || flag === true;
        const cls = on ? "text-bg-success" : "text-bg-secondary";
        const label = on ? "Yes" : "No";
        return `<span class="badge ${cls}">${label}</span>`;
    }

    function initTable() {
        if (!$.fn.DataTable) {
            console.error("DataTables is not available");
            return;
        }

        table = $("#oms-clients-table").DataTable({
            responsive: true,
            autoWidth: false,
            pageLength: 20,
            lengthChange: false,
            dom: "rtip",
            order: [[1, "asc"]],
            columnDefs: [
                { orderable: false, targets: [3, 4, 6] }
            ]
        });
    }

    function showError(message) {
        const el = document.getElementById("oms-clients-alert");
        if (!el) return;

        el.textContent = message || "Unable to load OMS clients data.";
        el.classList.remove("d-none");
    }

    function clearError() {
        const el = document.getElementById("oms-clients-alert");
        if (!el) return;

        el.textContent = "";
        el.classList.add("d-none");
    }

    function populateClients(rows) {
        if (!table) return;

        table.clear();

        (rows || []).forEach(function (client) {
            table.row.add([
                esc(text(client.client_id)),
                esc(text(client.name)),
                esc(text(client.execution_mode)),
                yesNoBadge(client.broker_login),
                yesNoBadge(client.logged_in),
                esc(text(client.logged_time)),
                yesNoBadge(client.active)
            ]);
        });

        table.draw(false);
    }

    async function loadClients() {
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
                throw new Error(payload.message || "Clients fetch failed");
            }

            clearError();
            populateClients(payload.data || []);
        } catch (err) {
            console.error("OMS clients load failed:", err);
            showError("Unable to load OMS clients data.");
        }
    }

    function bindSearch() {
        const input = document.getElementById("oms-clients-search");
        if (!input || !table) return;

        input.addEventListener("input", function () {
            table.search(this.value || "").draw();
        });
    }

    function bindLength() {
        const select = document.getElementById("oms-clients-length");
        if (!select || !table) return;

        select.addEventListener("change", function () {
            const len = parseInt(this.value, 10) || 20;
            table.page.len(len).draw();
        });
    }

    function bindRefresh() {
        const btn = document.getElementById("oms-clients-refresh");
        if (!btn) return;

        btn.addEventListener("click", function () {
            loadClients();
        });
    }

    document.addEventListener("DOMContentLoaded", function () {
        initTable();
        bindSearch();
        bindLength();
        bindRefresh();
        loadClients();
        window.setInterval(loadClients, Number(CFG.refreshMs || 30000));
    });
})();