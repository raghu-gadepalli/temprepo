(function () {
  "use strict";

  const CFG = window.USER_PREF_CONFIG || {
    dataUrl: "/user/preferences/data",
    saveUrl: "/user/preferences",
    showUsers: false
  };

  let prefLastTrigger = null;

  function txt(v, fallback = "—") {
    return (v == null || String(v).trim() === "") ? fallback : String(v);
  }

  function setSelectValue(id, value) {
    const el = document.getElementById(id);
    if (!el) return;
    el.value = String(value ?? "");
  }

  function setInputValue(id, value) {
    const el = document.getElementById(id);
    if (!el) return;
    el.value = value == null ? "" : value;
  }

  function yesNo(v) {
    return Number(v) === 1 ? "Yes" : "No";
  }

  function enabledDisabled(v) {
    return Number(v) === 1 ? "Enabled" : "Disabled";
  }

  function initTooltips(scope) {
    if (window.UI && typeof window.UI.initTooltips === "function") {
      window.UI.initTooltips(scope);
    }
  }

  function fillSingleForm(row) {
    setInputValue("pref-form-userid", row.userid);
    setInputValue("name", row.name);
    setInputValue("email", row.email || "");
    setInputValue("mobile", row.mobile || "");
    setSelectValue("broker_login", Number(row.broker_login || 0));
    setInputValue("broker_name", row.broker_name || "");
    setSelectValue("intraday_only", Number(row.intraday_only || 0));
    setInputValue("stocks", row.stocks || "");
    setSelectValue("equity", Number(row.equity || 0));
    setSelectValue("futures", Number(row.futures || 0));
    setSelectValue("options", Number(row.options || 0));
    setSelectValue("execution_mode_display", row.execution_mode || "VIRTUAL");
    setInputValue("execution_mode", row.execution_mode || "VIRTUAL");
    setSelectValue("autotrade", Number(row.autotrade || 0));
    $("#pref-single-view").removeClass("d-none");
    $("#pref-multi-view").addClass("d-none");
  }

  function fillModalForm(row) {
    setInputValue("mp-userid-input", row.userid);
    setInputValue("mp-name", row.name);
    setInputValue("mp-email", row.email || "");
    setInputValue("mp-mobile", row.mobile || "");
    setSelectValue("mp-broker_login", Number(row.broker_login || 0));
    setInputValue("mp-broker_name", row.broker_name || "");
    setSelectValue("mp-intraday_only", Number(row.intraday_only || 0));
    setInputValue("mp-stocks", row.stocks || "");
    setSelectValue("mp-equity", Number(row.equity || 0));
    setSelectValue("mp-futures", Number(row.futures || 0));
    setSelectValue("mp-options", Number(row.options || 0));
    setSelectValue("mp-execution_mode_display", row.execution_mode || "VIRTUAL");
    setInputValue("mp-execution_mode", row.execution_mode || "VIRTUAL");
    setSelectValue("mp-autotrade", Number(row.autotrade || 0));
  }

  function showPrefModal(row, triggerEl) {
    prefLastTrigger = triggerEl || null;
    fillModalForm(row);
    const modalEl = document.getElementById("prefDetailsModal");
    if (!modalEl) return;
    bootstrap.Modal.getOrCreateInstance(modalEl, { focus: true }).show();
  }

  function setupModalLifecycle() {
    const modalEl = document.getElementById("prefDetailsModal");
    if (!modalEl) return;
    modalEl.addEventListener("hide.bs.modal", () => {
      try {
        if (document.activeElement && modalEl.contains(document.activeElement)) {
          document.activeElement.blur();
        }
      } catch (_) {}
      modalEl.setAttribute("inert", "");
    });
    modalEl.addEventListener("shown.bs.modal", () => modalEl.removeAttribute("inert"));
    modalEl.addEventListener("hidden.bs.modal", () => {
      modalEl.removeAttribute("inert");
      if (prefLastTrigger && typeof prefLastTrigger.focus === "function") prefLastTrigger.focus();
      prefLastTrigger = null;
    });
  }

  function ensureTable() {
    if ($.fn.DataTable.isDataTable("#pref-table")) return $("#pref-table").DataTable();
    return $("#pref-table").DataTable({
      responsive: true,
      autoWidth: false,
      paging: false,
      searching: false,
      info: false,
      lengthChange: false,
      ordering: true,
      dom: "rt",
      columnDefs: [{ orderable: false, targets: -1, className: "text-center" }],
      drawCallback() { initTooltips("#pref-table"); }
    });
  }

  function populateMulti(rows) {
    const table = ensureTable();
    table.clear();
    rows.forEach((row) => {
      const payload = encodeURIComponent(JSON.stringify(row));
      const actionBtn = `<div class="text-center"><button type="button" class="btn btn-link p-0 text-primary fs-5 js-pref-view" data-record="${payload}" data-bs-toggle="tooltip" title="View / Edit User"><i class="bi bi-info-circle-fill"></i></button></div>`;
      table.row.add([
        txt(row.userid),
        txt(row.name),
        txt(row.execution_mode),
        enabledDisabled(row.autotrade),
        enabledDisabled(row.equity),
        enabledDisabled(row.futures),
        enabledDisabled(row.options),
        actionBtn
      ]);
    });
    table.draw();
    $("#pref-multi-view").removeClass("d-none");
    $("#pref-single-view").addClass("d-none");
  }

  function bindActions() {
    $(document).on("click", ".js-pref-view", function (e) {
      e.preventDefault();
      let row;
      try { row = JSON.parse(decodeURIComponent($(this).attr("data-record") || "")); }
      catch (err) { console.error("Invalid preferences payload", err); return; }
      showPrefModal(row, this);
    });
  }

  async function loadPrefData() {
    try {
      const resp = await fetch(CFG.dataUrl, { credentials: "same-origin" });
      const payload = await resp.json().catch(() => ({}));
      if (payload.status !== "success") { console.error("Preferences data fetch failed:", payload); return; }
      const rows = Array.isArray(payload.data) ? payload.data : [];
      if (CFG.showUsers) populateMulti(rows); else fillSingleForm(rows[0] || {});
    } catch (err) { console.error("Failed to load preferences data", err); }
  }

  $(document).ready(() => {
    setupModalLifecycle();
    bindActions();
    loadPrefData();
  });
})();
