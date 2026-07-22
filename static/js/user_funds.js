(function () {
  "use strict";

  const CFG = window.USER_FUNDS_CONFIG || {
    dataUrl: "/user/funds/data",
    showUsers: false
  };

  let fundsLastTrigger = null;
  let fundsTable = null;

  function initTooltips(scope) {
    if (window.UI && typeof window.UI.initTooltips === "function") {
      window.UI.initTooltips(scope);
    }
  }

  function txt(v, fallback = "—") {
    return (v == null || String(v).trim() === "") ? fallback : String(v);
  }

  function money(v) {
    const n = Number(v);
    return Number.isFinite(n) ? n.toFixed(2) : "—";
  }

  function statusBadge(v) {
    const s = String(v || "").toUpperCase();
    if (s === "LOGGED_IN") {
      return `<span class="badge bg-success">Logged In</span>`;
    }
    return `<span class="badge bg-secondary">Not Logged In</span>`;
  }

  function fundsStatusBadge(v) {
    const s = String(v || "").toUpperCase();
    if (s === "LIVE") return `<span class="badge bg-success">Live</span>`;
    if (s === "NOT_REFRESHED") return `<span class="badge bg-secondary">Not Refreshed</span>`;
    if (s === "LIVE_FETCH_FAILED") return `<span class="badge bg-danger">Fetch Failed</span>`;
    if (s === "NOT_LOGGED_IN") return `<span class="badge bg-secondary">Not Logged In</span>`;
    if (s === "NOT_LOGGED_INTO_ZERODHA") return `<span class="badge bg-secondary">Not Logged In</span>`;
    if (s === "FUNDS_FETCH_FAILED") return `<span class="badge bg-danger">Fetch Failed</span>`;
    return `<span class="badge bg-secondary">${txt(v)}</span>`;
  }

  function yesNoBadge(v) {
    const on = Boolean(v);
    return on
      ? `<span class="badge bg-success">Yes</span>`
      : `<span class="badge bg-secondary">No</span>`;
  }

  function setHtml(id, html) {
    const el = document.getElementById(id);
    if (el) el.innerHTML = html;
  }

  function setText(id, text) {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
  }

  function renderUtilizedRows(targetId, details) {
    const tbody = document.getElementById(targetId);
    if (!tbody) return;

    tbody.innerHTML = "";

    if (!details || typeof details !== "object") {
      return;
    }

    Object.entries(details).forEach(([k, v]) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${txt(k)}</td>
        <td>${money(v)}</td>
      `;
      tbody.appendChild(tr);
    });
  }

  function populateSingle(row) {
    setText("sf-userid", txt(row.userid));
    setText("sf-name", txt(row.name));
    setText("sf-broker-name", txt(row.broker_name));

    setHtml("sf-status", statusBadge(row.status));
    setHtml("sf-funds-status", fundsStatusBadge(row.funds_status));
    setHtml("sf-live", yesNoBadge(row.live));
    setText("sf-logged-time", txt(row.logged_time));

    setText("sf-total-balance", money(row.total_balance));
    setText("sf-available-margin", money(row.available_margin));
    setText("sf-opening-balance", money(row.opening_balance));
    setText("sf-live-balance", money(row.live_balance));
    setText("sf-intraday-payin", money(row.intraday_payin));
    setText("sf-collateral", money(row.collateral));
    setText("sf-adhoc-margin", money(row.adhoc_margin));
    setText("sf-utilized-margin-total", money(row.utilized_margin_total));

    if (row.utilized_margin_details && typeof row.utilized_margin_details === "object") {
      renderUtilizedRows("sf-utilized-body", row.utilized_margin_details);
      $("#sf-utilized-wrap").removeClass("d-none");
    } else {
      $("#sf-utilized-wrap").addClass("d-none");
    }

    $("#funds-single-view").removeClass("d-none");
    $("#funds-multi-view").addClass("d-none");
  }

  function populateModal(row) {
    setText("mf-userid", txt(row.userid));
    setText("mf-name", txt(row.name));
    setText("mf-broker-name", txt(row.broker_name));

    setHtml("mf-status", statusBadge(row.status));
    setHtml("mf-funds-status", fundsStatusBadge(row.funds_status));
    setHtml("mf-live", yesNoBadge(row.live));
    setText("mf-logged-time", txt(row.logged_time));

    setText("mf-total-balance", money(row.total_balance));
    setText("mf-available-margin", money(row.available_margin));
    setText("mf-opening-balance", money(row.opening_balance));
    setText("mf-live-balance", money(row.live_balance));
    setText("mf-intraday-payin", money(row.intraday_payin));
    setText("mf-collateral", money(row.collateral));
    setText("mf-adhoc-margin", money(row.adhoc_margin));
    setText("mf-utilized-margin-total", money(row.utilized_margin_total));

    if (row.utilized_margin_details && typeof row.utilized_margin_details === "object") {
      renderUtilizedRows("mf-utilized-body", row.utilized_margin_details);
      $("#mf-utilized-wrap").removeClass("d-none");
    } else {
      $("#mf-utilized-wrap").addClass("d-none");
    }

    initTooltips("#fundsDetailsModal");
  }

  function showFundsModal(row, triggerEl) {
    fundsLastTrigger = triggerEl || null;
    populateModal(row);

    const modalEl = document.getElementById("fundsDetailsModal");
    if (!modalEl) return;

    const modal = bootstrap.Modal.getOrCreateInstance(modalEl, { focus: true });
    modal.show();
  }

  function setupModalLifecycle() {
    const modalEl = document.getElementById("fundsDetailsModal");
    if (!modalEl) return;

    modalEl.addEventListener("hide.bs.modal", () => {
      try {
        if (document.activeElement && modalEl.contains(document.activeElement)) {
          document.activeElement.blur();
        }
      } catch (_) {}
      modalEl.setAttribute("inert", "");
    });

    modalEl.addEventListener("shown.bs.modal", () => {
      modalEl.removeAttribute("inert");
    });

    modalEl.addEventListener("hidden.bs.modal", () => {
      modalEl.removeAttribute("inert");
      if (fundsLastTrigger && typeof fundsLastTrigger.focus === "function") {
        fundsLastTrigger.focus();
      }
      fundsLastTrigger = null;
    });
  }

  function ensureTable() {
    if ($.fn.DataTable.isDataTable("#funds-table")) {
      fundsTable = $("#funds-table").DataTable();
      return fundsTable;
    }

    fundsTable = $("#funds-table").DataTable({
      responsive: true,
      autoWidth: false,
      paging: false,
      searching: false,
      info: false,
      lengthChange: false,
      ordering: true,
      dom: "rt",
      columnDefs: [
        { orderable: false, targets: -1, className: "text-center" }
      ],
      drawCallback() {
        initTooltips("#funds-table");
      }
    });

    return fundsTable;
  }

  function populateMulti(rows) {
    const table = ensureTable();
    table.clear();

    rows.forEach((row) => {
      const payload = encodeURIComponent(JSON.stringify(row));

      const actionBtn = `
        <div class="text-center">
          <button type="button"
            class="btn btn-link p-0 text-primary fs-5 js-funds-view"
            data-record="${payload}"
            data-bs-toggle="tooltip"
            title="View Details">
            <i class="bi bi-info-circle-fill"></i>
          </button>
        </div>
      `;

      table.row.add([
        txt(row.userid),
        txt(row.name),
        txt(row.broker_name),
        statusBadge(row.status),
        fundsStatusBadge(row.funds_status),
        money(row.total_balance),
        money(row.available_margin),
        yesNoBadge(row.live),
        txt(row.logged_time),
        actionBtn
      ]);
    });

    table.draw();

    $("#funds-multi-view").removeClass("d-none");
    $("#funds-single-view").addClass("d-none");
  }

  function renderRows(rows) {
    if (CFG.showUsers) {
      populateMulti(rows);
    } else {
      populateSingle(rows[0] || {});
    }
  }

  function setRefreshBusy(isBusy) {
    const btn = document.getElementById("funds-refresh-btn");
    if (!btn) return;
    btn.disabled = !!isBusy;
    btn.textContent = isBusy ? "Refreshing..." : "Refresh";
  }

  function bindActions() {
    $(document).on("click", ".js-funds-view", function (e) {
      e.preventDefault();

      let row;
      try {
        row = JSON.parse(decodeURIComponent($(this).attr("data-record") || ""));
      } catch (err) {
        console.error("Invalid funds payload", err);
        return;
      }

      showFundsModal(row, this);
    });

    $("#funds-refresh-btn").on("click", async function () {
      await loadFundsData(true);
    });
  }

  async function fetchFundsData(refresh = false) {
    const url = refresh ? `${CFG.dataUrl}?refresh=1` : CFG.dataUrl;
    const resp = await fetch(url, { credentials: "same-origin" });
    return await resp.json().catch(() => ({}));
  }

  async function loadFundsData(refresh = false) {
    try {
      setRefreshBusy(refresh);

      const payload = await fetchFundsData(refresh);

      if (payload.status !== "success") {
        console.error("Funds data fetch failed:", payload);
        return;
      }

      const rows = Array.isArray(payload.data) ? payload.data : [];
      renderRows(rows);
    } catch (err) {
      console.error("Failed to load funds data", err);
    } finally {
      setRefreshBusy(false);
    }
  }

  $(document).ready(() => {
    setupModalLifecycle();
    bindActions();
    loadFundsData(false);
  });
})();