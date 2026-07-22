(function () {
  "use strict";

  const CFG = window.USER_PROFILE_CONFIG || {
    dataUrl: "/user/profile/data",
    showUsers: false
  };

  let profileLastTrigger = null;

  function initTooltips(scope) {
    if (window.UI && typeof window.UI.initTooltips === "function") {
      window.UI.initTooltips(scope);
    }
  }

  function txt(v, fallback = "—") {
    return (v == null || String(v).trim() === "") ? fallback : String(v);
  }

  function yesNoBadge(v) {
    const on = Number(v) === 1 || String(v).toLowerCase() === "true";
    return on
      ? `<span class="badge badge-yes">Yes</span>`
      : `<span class="badge badge-no">No</span>`;
  }

  function enabledDisabledBadge(v) {
    const on = Number(v) === 1 || String(v).toLowerCase() === "true";
    return on
      ? `<span class="badge badge-yes">Enabled</span>`
      : `<span class="badge badge-no">Disabled</span>`;
  }

  function statusBadge(v) {
    const s = String(v || "").toUpperCase();
    if (s === "LOGGED_IN") {
      return `<span class="badge bg-success">Logged In</span>`;
    }
    return `<span class="badge bg-secondary">Not Logged In</span>`;
  }

  function setHtml(id, html) {
    const el = document.getElementById(id);
    if (el) el.innerHTML = html;
  }

  function setText(id, text) {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
  }

  function populateSingle(row) {
    setText("sp-name", txt(row.name));
    setText("sp-email", txt(row.email));
    setText("sp-mobile", txt(row.mobile));

    setHtml("sp-broker-login", yesNoBadge(row.broker_login));
    setText("sp-broker-name", txt(row.broker_name));
    setText("sp-execution-mode", txt(row.execution_mode));

    setHtml("sp-equity", yesNoBadge(row.equity));
    setHtml("sp-futures", yesNoBadge(row.futures));
    setHtml("sp-options", yesNoBadge(row.options));
    setHtml("sp-intraday-only", yesNoBadge(row.intraday_only));

    setHtml("sp-autotrade", enabledDisabledBadge(row.autotrade));
    setHtml("sp-active", yesNoBadge(row.active));
    setHtml("sp-status", statusBadge(row.status));
    setText("sp-logged-time", txt(row.logged_time));
    setText("sp-stocks", txt(row.stocks));

    $("#profile-single-view").removeClass("d-none");
    $("#profile-multi-view").addClass("d-none");
  }

  function populateModal(row) {
    setText("mp-userid", txt(row.userid));
    setText("mp-name", txt(row.name));
    setText("mp-email", txt(row.email));
    setText("mp-mobile", txt(row.mobile));

    setHtml("mp-broker-login", yesNoBadge(row.broker_login));
    setText("mp-broker-name", txt(row.broker_name));
    setText("mp-execution-mode", txt(row.execution_mode));

    setHtml("mp-equity", yesNoBadge(row.equity));
    setHtml("mp-futures", yesNoBadge(row.futures));
    setHtml("mp-options", yesNoBadge(row.options));
    setHtml("mp-intraday-only", yesNoBadge(row.intraday_only));

    setHtml("mp-autotrade", enabledDisabledBadge(row.autotrade));
    setHtml("mp-active", yesNoBadge(row.active));
    setHtml("mp-status", statusBadge(row.status));
    setText("mp-logged-time", txt(row.logged_time));
    setText("mp-stocks", txt(row.stocks));

    initTooltips("#profileDetailsModal");
  }

  function showProfileModal(row, triggerEl) {
    profileLastTrigger = triggerEl || null;
    populateModal(row);

    const modalEl = document.getElementById("profileDetailsModal");
    if (!modalEl) return;

    const modal = bootstrap.Modal.getOrCreateInstance(modalEl, { focus: true });
    modal.show();
  }

  function setupModalLifecycle() {
    const modalEl = document.getElementById("profileDetailsModal");
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
      if (profileLastTrigger && typeof profileLastTrigger.focus === "function") {
        profileLastTrigger.focus();
      }
      profileLastTrigger = null;
    });
  }

  function ensureTable() {
    if ($.fn.DataTable.isDataTable("#profile-table")) {
      return $("#profile-table").DataTable();
    }

    return $("#profile-table").DataTable({
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
        initTooltips("#profile-table");
      }
    });
  }

  function populateMulti(rows) {
    const table = ensureTable();
    table.clear();

    rows.forEach((row) => {
      const payload = encodeURIComponent(JSON.stringify(row));

      const actionBtn = `
        <div class="text-center">
          <button type="button"
            class="btn btn-link p-0 text-primary fs-5 js-profile-view"
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
        txt(row.execution_mode),
        enabledDisabledBadge(row.autotrade),
        txt(row.logged_time),
        actionBtn
      ]);
    });

    table.draw();

    $("#profile-multi-view").removeClass("d-none");
    $("#profile-single-view").addClass("d-none");
  }

  function bindActions() {
    $(document).on("click", ".js-profile-view", function (e) {
      e.preventDefault();

      let row;
      try {
        row = JSON.parse(decodeURIComponent($(this).attr("data-record") || ""));
      } catch (err) {
        console.error("Invalid profile payload", err);
        return;
      }

      showProfileModal(row, this);
    });
  }

  async function loadProfileData() {
    try {
      const resp = await fetch(CFG.dataUrl, { credentials: "same-origin" });
      const payload = await resp.json().catch(() => ({}));

      if (payload.status !== "success") {
        console.error("Profile data fetch failed:", payload);
        return;
      }

      const rows = Array.isArray(payload.data) ? payload.data : [];

      if (CFG.showUsers) {
        populateMulti(rows);
      } else {
        populateSingle(rows[0] || {});
      }
    } catch (err) {
      console.error("Failed to load profile data", err);
    }
  }

  $(document).ready(() => {
    setupModalLifecycle();
    bindActions();
    loadProfileData();
  });
})();