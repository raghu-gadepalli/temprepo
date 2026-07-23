// static/js/positions.js
// Positions page
// Actions:
// - Info  -> consolidated info modal
// - Plus  -> add to existing position via shared trade_create.js
// - Exit  -> full exit via shared trade_exit.js

let posCurrentItem = null;
let posCollapsedGroups = Object.create(null);
let posAllRows = [];
let posLoadSequence = 0;
let posLoadController = null;

function posCfg() {
  return window.POSITIONS_CONFIG || {};
}

function posShowUsers() {
  return !!posCfg().showUsers;
}

function posSelectedUserFilter() {
  return String($("#pos-user-filter").val() || "ALL").trim().toUpperCase() || "ALL";
}

function posSelectedModeFilter() {
  return String($("#pos-mode-filter").val() || "ALL").trim().toUpperCase() || "ALL";
}

function posIsGroupCollapsed(group) {
  const key = String(group || "").trim();
  if (!Object.prototype.hasOwnProperty.call(posCollapsedGroups, key)) {
    posCollapsedGroups[key] = true;
  }
  return !!posCollapsedGroups[key];
}

// ------------------------------
// Generic helpers
// ------------------------------
function posNum(v, d = 2) {
  const x = Number(v);
  return Number.isFinite(x) ? x.toFixed(d) : "–";
}

function posMoney(v) {
  const x = Number(v);
  return Number.isFinite(x) ? x.toFixed(2) : "";
}

function posText(v, fallback = "–") {
  return (v == null || String(v).trim() === "") ? fallback : String(v);
}

function posEscAttr(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/'/g, "&#39;")
    .replace(/"/g, "&quot;");
}

function posNormStatus(x) {
  return String(x || "").toUpperCase().trim();
}

function posPnlSpan(v) {
  const x = Number(v || 0);
  const cls = x >= 0 ? "dash-text-success" : "dash-text-danger";
  return `<span class="${cls}">${x.toFixed(2)}</span>`;
}

function posModeBadge(mode) {
  const value = posNormStatus(mode || "VIRTUAL") || "VIRTUAL";
  const cls = value === "REAL" ? "bg-primary" : "bg-secondary";
  return `<span class="badge ${cls} ms-1">${posText(value)}</span>`;
}

function posGetPnlValue(item) {
  const v =
    item?.pnl ??
    item?.pnl_value ??
    item?.exit_pnl ??
    item?.last_pnl_value ??
    item?.live_pnl ??
    item?.last_pnl ??
    0;

  const x = Number(v);
  return Number.isFinite(x) ? x : 0;
}

function posOriginLabel(value) {
  const origin = posNormStatus(value || "UNKNOWN");
  return ({
    SIGNAL_AUTO: "Signal Auto",
    SIGNAL_MANUAL: "Signal Manual",
    WATCHLIST: "Watchlist",
    POSITION_ADD: "Position Add",
    UNKNOWN: "Unknown"
  })[origin] || origin.replaceAll("_", " ");
}

function posManagementLabel(value) {
  return posNormStatus(value) === "SIGNAL_LIFECYCLE"
    ? "Signal Lifecycle"
    : "Manual Price-Based";
}

function posStatusBadgeFromItem(item) {
  const status = posNormStatus(item.status);

  if (status === "CLOSED") {
    return `<span class="badge bg-secondary">CLOSED</span>`;
  }
  if (status === "EXIT READY") {
    return `<span class="badge bg-warning text-dark">EXIT READY</span>`;
  }
  if (status === "EXIT SUBMITTED") {
    return `<span class="badge bg-warning text-dark">EXIT SUBMITTED</span>`;
  }
  return `<span class="badge bg-success">OPEN</span>`;
}

function posNetQty(item) {
  const v = Number(item?.net_qty ?? item?.qty ?? item?.quantity ?? 0);
  return Number.isFinite(v) ? v : 0;
}

function posCanExit(item) {
  const status = posNormStatus(item.status);
  const exitStatus = posNormStatus(item.exit_status);
  const netQty = posNetQty(item);

  if (!Number.isFinite(netQty) || netQty === 0) return false;
  if (status === "CLOSED") return false;
  if (exitStatus === "READY" || exitStatus === "SUBMITTED") return false;

  return true;
}

function posCanAdd(item) {
  const status = posNormStatus(item.status);
  const netQty = posNetQty(item);
  if (!Number.isFinite(netQty) || netQty === 0) return false;
  if (status === "CLOSED") return false;
  return true;
}

function posEntrySideFromPosition(item) {
  const side = posNormStatus(item?.side);
  if (side === "SHORT") return "SELL";
  return "BUY";
}

function posBuildTradePayload(item) {
  return {
    ...item,
    entry_side: posEntrySideFromPosition(item)
  };
}


function posBuildAuditPayload(item) {
  const id = posText(item?.id, "");
  const oppId = posText(item?.signal_id || item?.signal_id, "");
  const symbol = posText(item?.symbol, "");
  const userid = posText(item?.userid, "");
  const startTime = posText(item?.entry_plan_time || item?.entry_time || item?.entry_exec_time || item?.last_time, "");
  const endTime = posText(item?.exit_time, "");

  return {
    title: `Trade ${symbol}`,
    subtitle: `${userid} • ${posText(item?.trade_type || item?.side, "")} • ${posText(item?.entry_status || item?.status, "")}`,
    entityId: id,
    relatedId: oppId,
    symbol,
    userid,
    startTime,
    endTime,
    limit: 300
  };
}

// ------------------------------
// Populate positions table
// ------------------------------
function populatePositions(data) {
  const table = $("#positions-table").DataTable();
  table.clear();

  let total = 0;
  const showUsers = posShowUsers();

  (data || []).forEach((item) => {
    const group = String(item?.userid || "").trim();
    if (showUsers && group && !Object.prototype.hasOwnProperty.call(posCollapsedGroups, group)) {
      posCollapsedGroups[group] = true;
    }

    const pnlVal = posGetPnlValue(item);
    total += pnlVal;

    const infoBtn = `
      <button type="button"
        class="btn btn-link btn-sm p-0 px-1 text-primary pos-info"
        data-record='${posEscAttr(JSON.stringify(item))}'
        title="Info" aria-label="Info">
        <i class="bi bi-info-circle"></i>
      </button>
    `;

    const auditPayload = posEscAttr(encodeURIComponent(JSON.stringify(posBuildAuditPayload(item))));
    const auditBtn = `
      <button type="button"
        class="btn btn-link btn-sm p-0 px-1 text-secondary js-audit-timeline"
        data-record='${auditPayload}'
        title="Audit Timeline" aria-label="Audit Timeline">
        <i class="bi bi-clock-history"></i>
      </button>
    `;

    const addDisabled = posCanAdd(item) ? "" : "disabled";
    const addPayload = posEscAttr(JSON.stringify(posBuildTradePayload(item)));
    const addBtn = `
      <button type="button"
        class="btn btn-link btn-sm p-0 px-1 text-success js-trade-create"
        data-source="position"
        data-record='${addPayload}'
        title="Add Position" aria-label="Add Position" ${addDisabled}>
        <i class="bi bi-plus-circle"></i>
      </button>
    `;

    const exitDisabled = posCanExit(item) ? "" : "disabled";
    const exitBtn = `
      <button type="button"
        class="btn btn-link btn-sm p-0 px-1 text-danger js-trade-exit"
        data-source="position"
        data-record='${posEscAttr(JSON.stringify(item))}'
        title="Exit Position" aria-label="Exit Position" ${exitDisabled}>
        <i class="bi bi-box-arrow-right"></i>
      </button>
    `;

    table.row.add([
      posText(item.userid),
      posText(item.entry_plan_time || item.entry_time),
      posOriginLabel(item.origin),
      posText(item.symbol),
      `${posText(item.instrument_type || item.type)} / ${posText(item.product || "MIS")} ${posModeBadge(item.execution_mode)}`,
      posText(item.side || item.trade_type),
      posNum(item.avg_price),
      posText(item.net_qty ?? item.qty ?? item.quantity),
      posNum(item.last_price),
      posPnlSpan(pnlVal),
      posStatusBadgeFromItem(item),
      `<div class="d-flex align-items-center justify-content-center gap-1 flex-nowrap">${infoBtn}${addBtn}${exitBtn}</div>`
    ]);
  });

  table.draw();

  const cls = total >= 0 ? "dash-text-success" : "dash-text-danger";
  $("#positions-total-pnl").html(`<b class="${cls}">${total.toFixed(2)}</b>`);

  if (window.UI && typeof window.UI.initTooltips === "function") {
    window.UI.initTooltips("#positions-table");
  }
}

// ------------------------------
// Info modal
// ------------------------------
$(document).on("click", ".pos-info", function () {
  const raw = $(this).attr("data-record");
  if (!raw) return;

  let item;
  try { item = JSON.parse(raw); } catch { return; }

  posCurrentItem = item;

  $("#pi-userid").text(posText(item.userid));
  $("#pi-symbol").text(posText(item.symbol));
  $("#pi-type").text(`${posText(item.instrument_type || item.type)} / ${posText(item.product || "MIS")}`);
  $("#pi-side").text(posText(item.side || item.trade_type));

  $("#pi-exec-time").text(posText(item.entry_plan_time || item.entry_time));
  $("#pi-origin").text(posOriginLabel(item.origin));
  $("#pi-management-mode").text(posManagementLabel(item.management_mode));
  $("#pi-signal-reference").text(posText(item.signal_reference, "—"));
  $("#pi-last-time").text(posText(item.last_time));
  $("#pi-last-price").text(posNum(item.last_price));
  $("#pi-pnl").html(posPnlSpan(posGetPnlValue(item)));
  $("#pi-status").html(posStatusBadgeFromItem(item));
  $("#pi-execution-mode").html(posModeBadge(item.execution_mode));

  const buyQty = Number(item.buy_qty ?? 0);
  const sellQty = Number(item.sell_qty ?? 0);
  const netQty = Number(item.net_qty ?? item.qty ?? item.quantity ?? 0);

  const buyAvg = Number(item.buy_avg ?? item.buy_avg_price ?? item.avg_buy_price ?? 0);
  const sellAvg = Number(item.sell_avg ?? item.sell_avg_price ?? item.avg_sell_price ?? 0);
  const netAvg = Number(item.avg_price ?? 0);

  $("#pi-buy-qty").text(buyQty ? buyQty : "–");
  $("#pi-buy-avg").text(buyQty ? posNum(buyAvg) : "–");
  $("#pi-buy-value").text(buyQty ? posNum(buyQty * buyAvg) : "–");

  $("#pi-sell-qty").text(sellQty ? sellQty : "–");
  $("#pi-sell-avg").text(sellQty ? posNum(sellAvg) : "–");
  $("#pi-sell-value").text(sellQty ? posNum(sellQty * sellAvg) : "–");

  $("#pi-net-qty").text(Number.isFinite(netQty) ? netQty : "–");
  $("#pi-net-avg").text(posNum(netAvg));
  $("#pi-net-value").text(Number.isFinite(netQty) ? posNum(netQty * netAvg) : "–");

  const modal = bootstrap.Modal.getOrCreateInstance(document.getElementById("positionInfoModal"), { focus: true });
  modal.show();
});

// ------------------------------
// Group expand/collapse
// ------------------------------
$(document).on("click", "#positions-table tbody tr.pos-group-row", function () {
  if (!posShowUsers()) return;

  const group = String($(this).attr("data-group") || "").trim();
  if (!group) return;

  posCollapsedGroups[group] = !posIsGroupCollapsed(group);
  $("#positions-table").DataTable().draw(false);
});

// ------------------------------
// Cleanup + focus hygiene
// ------------------------------
$(document).on("hidden.bs.modal", "#positionInfoModal", function () {
  posCurrentItem = null;
});

$(document).ready(() => {
  const infoModalEl = document.getElementById("positionInfoModal");
  if (infoModalEl) {
    infoModalEl.addEventListener("hide.bs.modal", () => {
      try {
        if (document.activeElement && infoModalEl.contains(document.activeElement)) {
          document.activeElement.blur();
        }
      } catch (_) {}
    });
  }

  const showUsers = posShowUsers();

  $("#positions-table").DataTable({
    responsive: true,
    autoWidth: false,
    pageLength: -1,
    lengthMenu: [[100, 150, 200, -1], [100, 150, 200, "All"]],
    dom: "rtip",
    order: showUsers ? [[0, "asc"], [1, "desc"]] : [[1, "desc"]],
    rowGroup: showUsers ? {
      dataSrc: 0,
      startRender: function (rows, group) {
        const collapsed = posIsGroupCollapsed(group);

        rows.nodes().each(function (r) {
          r.style.display = collapsed ? "none" : "";
        });

        let totalPnl = 0;
        rows.data().each(function (row) {
          const raw = String(row[9] || "")
            .replace(/<[^>]*>/g, "")
            .replace(/,/g, "")
            .trim();
          const n = Number(raw);
          if (Number.isFinite(n)) totalPnl += n;
        });

        const pnlCls = totalPnl >= 0 ? "dash-text-success" : "dash-text-danger";
        const icon = collapsed ? "bi-chevron-right" : "bi-chevron-down";

        return $(`
          <tr class="table-light pos-group-row" data-group="${group}" style="cursor:pointer;">
            <td colspan="12">
              <div class="d-flex align-items-center justify-content-between">
                <div class="fw-semibold d-flex align-items-center gap-2">
                  <i class="bi ${icon}"></i>
                  <span>User: ${group}</span>
                </div>
                <div class="small text-muted">
                  Rows: ${rows.count()} &nbsp;|&nbsp;
                  P&amp;L:
                  <span class="${pnlCls}">${totalPnl.toFixed(2)}</span>
                </div>
              </div>
            </td>
          </tr>
        `);
      }
    } : undefined,
    columnDefs: [
      { targets: 0, visible: showUsers },
      { orderable: false, targets: -1 }
    ],
    drawCallback() {
      if (window.UI && typeof window.UI.initTooltips === "function") {
        window.UI.initTooltips("#positions-table");
      }
    }
  });

  $("#pos-search").on("input", function () {
    $("#positions-table").DataTable().search(this.value).draw();
  });

  $("#pos-length").on("change", function () {
    $("#positions-table").DataTable().page.len(Number(this.value) || -1).draw();
  });

  $("#pos-user-filter, #pos-mode-filter").on("change", function () {
    applyPositionFilters();
  });

  window.populatePositions = populatePositions;

  document.addEventListener("trade:exit:refresh", async function () {
    await loadPositions();
  });

  loadPositionUsers().finally(function () {
    loadPositions();
  });
});

// ------------------------------
// Data load
// ------------------------------
function applyPositionFilters() {
  const userFilter = posSelectedUserFilter();
  const modeFilter = posSelectedModeFilter();

  const rows = posAllRows.filter(function (row) {
    const userid = posNormStatus(row?.userid);
    const mode = posNormStatus(row?.execution_mode || "VIRTUAL") || "VIRTUAL";
    const userOk = userFilter === "ALL" || userid === userFilter;
    const modeOk = modeFilter === "ALL" || mode === modeFilter;
    return userOk && modeOk;
  });

  populatePositions(rows);
}

async function loadPositionUsers() {
  const usersUrl = posCfg().managedUsersUrl || posCfg().usersUrl;
  if (!posShowUsers() || !usersUrl || !$("#pos-user-filter").length) return;

  const $select = $("#pos-user-filter");
  const previous = String($select.val() || "").trim().toUpperCase();

  try {
    const resp = await fetch(usersUrl, { credentials: "same-origin", cache: "no-store" });
    const payload = await resp.json().catch(() => ({}));
    if (!resp.ok || payload.status !== "success") return;

    const users = Array.isArray(payload.data) ? payload.data : [];
    $select.empty().append(new Option("All users", "ALL"));
    users.forEach(function (row) {
      const userid = posNormStatus(row?.userid);
      if (userid) $select.append(new Option(userid, userid));
    });

    const preferred = previous || "ALL";
    $select.val($select.find(`option[value="${preferred}"]`).length ? preferred : "ALL");
  } catch (e) {
    console.warn("loadPositionUsers failed", e);
  }
}

async function loadPositions() {
  const requestSequence = ++posLoadSequence;

  if (posLoadController) {
    posLoadController.abort();
  }
  posLoadController = new AbortController();

  try {
    const baseUrl = posCfg().dataUrl || "/dashboard/positions/data?limit=5000";
    const separator = baseUrl.includes("?") ? "&" : "?";
    const url = `${baseUrl}${separator}_=${Date.now()}`;
    const resp = await fetch(url, {
      credentials: "same-origin",
      cache: "no-store",
      signal: posLoadController.signal
    });
    const payload = await resp.json().catch(() => ({}));

    if (requestSequence !== posLoadSequence) return;
    if (payload.status !== "success") return;
    posAllRows = Array.isArray(payload.data) ? payload.data : [];
    applyPositionFilters();

  } catch (e) {
    if (e && e.name === "AbortError") return;
    console.error("loadPositions failed", e);
  }
}

// ----------------------------------------------------
// DashboardAPI Registration
// Allows other modules such as orders.js to refresh
// positions through DashboardAPI.fetch("positions").
// ----------------------------------------------------
if (window.DashboardAPI) {
    DashboardAPI.register("positions", async () => {
        await loadPositions();
    });
}