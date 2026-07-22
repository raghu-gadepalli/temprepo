// static/js/performance.js
// Managed performance across explicit user and REAL/VIRTUAL trade-row scopes.

(function () {
  "use strict";

  const CFG = window.PERFORMANCE_CONFIG || {
    dataUrl: "/dashboard/performance/data",
    managedUsersUrl: "/dashboard/users/managed",
    showUsers: false,
    refreshMs: 30000,
  };

  let dt = null;
  let rowsCache = [];
  let loadSequence = 0;
  let loadController = null;

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function fmtINR(v) {
    const n = Number(v);
    if (!Number.isFinite(n)) return "—";
    return `₹${n.toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;
  }

  function fmtInt(v) {
    const n = Number(v);
    return Number.isFinite(n) ? n.toLocaleString("en-IN") : "—";
  }

  function fmtTS(s) {
    if (!s) return "—";
    const d = new Date(s);
    if (isNaN(d.getTime())) return String(s);
    const p = (x) => String(x).padStart(2, "0");
    return `${p(d.getDate())}-${p(d.getMonth() + 1)}-${d.getFullYear()} ${p(d.getHours())}:${p(d.getMinutes())}`;
  }

  function entryPx(r) {
    return r.executed_entry_price ?? r.entry_price;
  }

  function exitPx(r) {
    return r.executed_exit_price ?? r.exit_price;
  }

  function pnlValue(r) {
    const v = r.pnl_value ?? r.pnl ?? r.exit_pnl ?? r.last_pnl_value ?? r.last_pnl ?? 0;
    const n = Number(v);
    return Number.isFinite(n) ? n : 0;
  }

  function pnlHtml(v) {
    const cls = v >= 0 ? "dash-text-success" : "dash-text-danger";
    return `<span class="${cls}">${fmtINR(v)}</span>`;
  }

  function modeBadge(mode) {
    const value = String(mode || "VIRTUAL").trim().toUpperCase() || "VIRTUAL";
    const cls = value === "REAL" ? "bg-primary" : "bg-secondary";
    return `<span class="badge ${cls}">${esc(value)}</span>`;
  }

  function initTooltips(scope) {
    if (window.UI && typeof window.UI.initTooltips === "function") {
      window.UI.initTooltips(scope);
    }
  }

  function ensureTable() {
    if (dt) return;

    dt = $("#performance-table").DataTable({
      responsive: true,
      autoWidth: false,
      pageLength: 20,
      lengthMenu: [[20, 50, 100, 200], [20, 50, 100, 200]],
      dom: "rtip",
      order: [[0, "desc"]],
      columnDefs: [
        { targets: [6, 7, 8], className: "text-end" },
      ],
      columns: [
        { data: "entry_time", render: fmtTS },
        { data: "userid", defaultContent: "—" },
        { data: "symbol", defaultContent: "—" },
        { data: "instrument_type", defaultContent: "—" },
        { data: "execution_mode", render: modeBadge },
        { data: "trade_type", defaultContent: "—" },
        { data: "quantity", render: fmtInt },
        { data: null, render: (_v, _t, r) => fmtINR(entryPx(r)) },
        { data: null, render: (_v, _t, r) => fmtINR(exitPx(r)) },
        { data: null, render: (_v, _t, r) => pnlHtml(pnlValue(r)) },
        { data: "trade_status", render: (s) => esc(String(s || "—").replace(/_/g, " ")) },
      ],
      drawCallback() {
        initTooltips("#performance-table");
      },
    });
  }

  function updateSummary(rows) {
    const wins = rows.filter((r) => pnlValue(r) > 0);
    const losses = rows.filter((r) => pnlValue(r) < 0);
    const net = rows.reduce((a, r) => a + pnlValue(r), 0);

    $("#pf-total").text(fmtInt(rows.length));
    $("#pf-wins").text(fmtInt(wins.length));
    $("#pf-losses").text(fmtInt(losses.length));
    $("#pf-net").html(pnlHtml(net));
  }

  function fillModal(rows) {
    const mode = String($("#perf-mode").val() || "all").toUpperCase();
    $("#pf-modal-mode").text(mode === "ALL" ? "ALL MODES" : mode);

    const sum = (arr, f) => arr.reduce((a, x) => a + f(x), 0);
    const avg = (arr, f) => arr.length ? sum(arr, f) / arr.length : 0;
    const wins = rows.filter((r) => pnlValue(r) > 0);
    const losses = rows.filter((r) => pnlValue(r) < 0);

    $("#pf-winrate").text(`${(rows.length ? (wins.length / rows.length) * 100 : 0).toFixed(1)}%`);
    $("#pf-avgwin").text(fmtINR(avg(wins, pnlValue)));
    $("#pf-avgloss").text(fmtINR(avg(losses, (r) => Math.abs(pnlValue(r)))));
    $("#pf-avgpnl").text(fmtINR(avg(rows, pnlValue)));
    $("#pf-gp").text(fmtINR(sum(wins, pnlValue)));
    $("#pf-gl").text(fmtINR(-sum(losses, pnlValue)));
    $("#pf-avghold").text("0");
    $("#pf-mdd").text("₹0");

    const sideAgg = {};
    rows.forEach((r) => {
      const k = String(r.trade_type || "—").toUpperCase();
      sideAgg[k] ||= { n: 0, pl: 0 };
      sideAgg[k].n += 1;
      sideAgg[k].pl += pnlValue(r);
    });

    $("#pf-by-side").empty();
    Object.entries(sideAgg).forEach(([k, v]) => {
      $("#pf-by-side").append(`<tr><td>${esc(k)}</td><td>${fmtInt(v.n)}</td><td>${fmtINR(v.pl)}</td></tr>`);
    });

    const typeAgg = {};
    rows.forEach((r) => {
      const k = `${String(r.execution_mode || "VIRTUAL").toUpperCase()} / ${String(r.instrument_type || "—").toUpperCase()}`;
      typeAgg[k] ||= { n: 0, pl: 0 };
      typeAgg[k].n += 1;
      typeAgg[k].pl += pnlValue(r);
    });

    $("#pf-by-type").empty();
    Object.entries(typeAgg).forEach(([k, v]) => {
      $("#pf-by-type").append(`<tr><td>${esc(k)}</td><td>${fmtInt(v.n)}</td><td>${fmtINR(v.pl)}</td></tr>`);
    });
  }

  async function loadManagedUsers() {
    if (!CFG.showUsers || !CFG.managedUsersUrl || !$("#perf-user-filter").length) return;

    const $select = $("#perf-user-filter");
    const previous = String($select.val() || "ALL").trim().toUpperCase() || "ALL";

    try {
      const resp = await fetch(CFG.managedUsersUrl, { credentials: "same-origin", cache: "no-store" });
      const payload = await resp.json().catch(() => ({}));
      if (!resp.ok || payload.status !== "success") return;

      $select.empty().append(new Option("All users", "ALL"));
      (Array.isArray(payload.data) ? payload.data : []).forEach((row) => {
        const userid = String(row?.userid || "").trim().toUpperCase();
        if (userid) $select.append(new Option(userid, userid));
      });
      $select.val($select.find(`option[value="${previous}"]`).length ? previous : "ALL");
    } catch (err) {
      console.warn("performance user fetch failed", err);
    }
  }

  async function loadPerformance() {
    const requestSequence = ++loadSequence;
    if (loadController) loadController.abort();
    loadController = new AbortController();

    try {
      const mode = String($("#perf-mode").val() || "all").toLowerCase();
      const selectedUser = String($("#perf-user-filter").val() || "ALL").trim().toUpperCase();
      const url = new URL(CFG.dataUrl, window.location.origin);
      url.searchParams.set("mode", mode);
      if (CFG.showUsers && selectedUser !== "ALL") {
        url.searchParams.set("userid", selectedUser);
      }
      url.searchParams.set("_", Date.now());

      const resp = await fetch(url.toString(), {
        credentials: "same-origin",
        cache: "no-store",
        signal: loadController.signal,
      });
      const payload = await resp.json().catch(() => ({}));
      if (requestSequence !== loadSequence) return;
      if (!resp.ok || payload.status !== "success") {
        console.error("performance fetch failed:", resp.status, payload);
        return;
      }

      rowsCache = Array.isArray(payload.data) ? payload.data : [];
      ensureTable();
      dt.clear().rows.add(rowsCache).draw(false);
      updateSummary(rowsCache);
    } catch (err) {
      if (err && err.name === "AbortError") return;
      console.error("performance fetch failed:", err);
    }
  }

  $(document).ready(() => {
    ensureTable();

    $("#perf-search input").on("input", function () {
      dt.search(this.value).draw();
    });

    $("#perf-length-select").on("change", function () {
      dt.page.len(parseInt(this.value, 10) || 20).draw(false);
    });

    $("#perf-mode, #perf-user-filter").on("change", loadPerformance);
    $("#perf-summary-btn").on("click", () => fillModal(rowsCache));

    loadManagedUsers().finally(loadPerformance);

    if (Number(CFG.refreshMs || 0) > 0) {
      setInterval(loadPerformance, Number(CFG.refreshMs));
    }
  });

  window.loadPerformance = loadPerformance;
  window.populatePerformance = function (rows) {
    rowsCache = Array.isArray(rows) ? rows : [];
    ensureTable();
    dt.clear().rows.add(rowsCache).draw(false);
    updateSummary(rowsCache);
  };
})();
