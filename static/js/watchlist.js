// static/js/watchlist.js
// Watchlist page logic

(function () {
  "use strict";

  const CFG = window.WATCHLIST_CONFIG || {
    dataUrl: "/dashboard/watchlist/data",
  };

  let wlBreakoutMap = {};
  let wlShortlistMap = {};

  function initTooltips(scope) {
    if (window.UI && typeof UI.initTooltips === "function") {
      UI.initTooltips(scope);
    }
  }

  function esc(s) {
    return String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/'/g, "&#39;")
      .replace(/"/g, "&quot;");
  }

  function num(v, d = 2) {
    const n = Number(v);
    return Number.isFinite(n) ? n.toFixed(d) : "N/A";
  }

  function pct(v, d = 2) {
    const n = Number(v);
    return Number.isFinite(n) ? `${n.toFixed(d)}%` : "N/A";
  }

  function structureBadgeLabel(structure) {
    const accepted = structure?.accepted || {};
    const state = accepted?.state || structure?.state || "UNKNOWN";
    const side = accepted?.side || structure?.side || "NEUTRAL";
    return `${state} / ${side}`;
  }

  function structureSide(structure) {
    return structure?.accepted?.side || structure?.side || structure?.raw?.side || "BUY";
  }

  function registerWatchlistFilters() {
    if (!$.fn.dataTable || !$.fn.dataTable.ext || !$.fn.dataTable.ext.search) return;

    $.fn.dataTable.ext.search.push(function (settings, data) {
      const tableId = settings.nTable && settings.nTable.id;
      if (tableId !== "watchlist-table") return true;

      const symbol = data && data[1] ? String(data[1]) : "";
      if (!symbol) return true;

      const shortlistedOnly = $("#wl-shortlisted-only").is(":checked");
      if (shortlistedOnly && !wlShortlistMap[symbol]) return false;

      const sel = ($("#wl-breakout-filter").val() || "ALL").toUpperCase();
      if (sel === "ALL") return true;

      const st = wlBreakoutMap[symbol] || {};
      const pdh = (st.pdh_pdl_status || "UNKNOWN").toUpperCase();
      const orb = (st.orb_status || "UNKNOWN").toUpperCase();

      const isBreakout =
        pdh === "ABOVE" ||
        pdh === "BELOW" ||
        orb === "ABOVE" ||
        orb === "BELOW";

      if (sel === "ANY_BREAKOUT") return isBreakout;
      if (sel === "ABOVE_PDH") return pdh === "ABOVE";
      if (sel === "BELOW_PDL") return pdh === "BELOW";
      if (sel === "ABOVE_ORB") return orb === "ABOVE";
      if (sel === "BELOW_ORB") return orb === "BELOW";

      return true;
    });
  }

  async function loadWatchlist() {
    try {
      const resp = await fetch(CFG.dataUrl, { credentials: "same-origin" });
      const payload = await resp.json().catch(() => ({}));
      const raw = payload?.data ?? payload;
      populateWatchlist(raw || []);
    } catch (e) {
      console.error("loadWatchlist failed", e);
    }
  }

  function populateWatchlist(response) {
    const data = Array.isArray(response) ? response : response.data || [];
    const table = $("#watchlist-table").DataTable();
    table.clear();

    wlBreakoutMap = {};
    wlShortlistMap = {};

    data.forEach((item) => {
      const det = item.details || {};
      const symbol = item.symbol || "N/A";
      const structure = det?.structure || {};
      const breakout = structure?.breakout_context || {};

      wlBreakoutMap[symbol] = {
        pdh_pdl_status: item.pdh_pdl_status || breakout.pdh_pdl || "UNKNOWN",
        orb_status: item.orb_status || breakout.orb || "UNKNOWN",
      };

      const isShortlisted = !!(item.gen_signals ?? det?.gen_signals ?? item.generate_signals ?? false);
      wlShortlistMap[symbol] = isShortlisted;

      const payloadObj = {
        symbol,
        side: structureSide(structure),
        ltp: item.ltp,
        ltp_time: item.ltp_time,
        details: det,
      };

      const payloadEnc = encodeURIComponent(JSON.stringify(payloadObj));

      const structureLabel = structureBadgeLabel(structure);
      const anchors = structure?.anchors || structure?.anchor || {};
      const raw = structure?.raw || {};
      const accepted = structure?.accepted || {};
      const bo = structure?.breakout || {};

      const structureTip = [
        `Accepted: ${accepted.state || structure.state || "UNKNOWN"} / ${accepted.side || structure.side || "NEUTRAL"}`,
        `Raw: ${raw.state || structure.raw_state || "UNKNOWN"} / ${raw.side || structure.raw_side || "NEUTRAL"}`,
        `Anchor: ${anchors.active_anchor || "UNKNOWN"}`,
        `Breakout: ${bo.status || "NONE"} / ${bo.side || "NEUTRAL"}`,
        `Swing: ${breakout.swing || "UNKNOWN"}`,
        `PDH/PDL: ${breakout.pdh_pdl || "UNKNOWN"}`,
        `ORB: ${breakout.orb || "UNKNOWN"}`,
        `Recent15: ${breakout.recent15 || "UNKNOWN"}`,
        `Reason: ${structure.reason || accepted.reason || raw.reason || "N/A"}`,
      ].join(" | ");

      const structureCell = `
        <span data-bs-toggle="tooltip" title="${esc(structureTip)}">
          ${esc(structureLabel)}
        </span>
      `;

      const vwapVal = det?.vwap?.value;
      const vwapPct = det?.vwap?.px_vs_vwap_pct ?? item.vwap_pct;
      const vwapCell = (() => {
        const v = vwapVal != null && !isNaN(vwapVal) ? Number(vwapVal).toFixed(2) : "N/A";
        const tip = `Δ vs VWAP: ${pct(vwapPct, 2)}`;
        return `<span data-bs-toggle="tooltip" title="${esc(tip)}">${v}</span>`;
      })();

      const rsiVal = item.rsi ?? det?.context?.rsi?.value;
      const rsiZone = item.rsi_zone || det?.context?.rsi?.zone || "N/A";
      const rsiCell = (() => {
        const v = rsiVal != null && !isNaN(rsiVal) ? Number(rsiVal).toFixed(2) : "N/A";
        return `<span data-bs-toggle="tooltip" title="${esc(rsiZone)}">${v}</span>`;
      })();

      const bb = det?.bollinger || {};
      const bbZone = bb.zone || "NA";
      const bbPos = bb.position != null && !isNaN(bb.position) ? Number(bb.position).toFixed(3) : "N/A";
      const bbW = bb.bb_width != null && !isNaN(bb.bb_width) ? Number(bb.bb_width).toFixed(3) : "N/A";
      const bbU = bb.upper != null && !isNaN(bb.upper) ? Number(bb.upper).toFixed(2) : "N/A";
      const bbM = bb.mid != null && !isNaN(bb.mid) ? Number(bb.mid).toFixed(2) : "N/A";
      const bbL = bb.lower != null && !isNaN(bb.lower) ? Number(bb.lower).toFixed(2) : "N/A";
      const bbTip = `pos=${bbPos}, width=${bbW} | U=${bbU} M=${bbM} L=${bbL}`;
      const bbCell = `<span data-bs-toggle="tooltip" title="${esc(bbTip)}">${bbZone}</span>`;

      const opt15 =
        item.option_sentiment ||
        det?.derivatives?.option_sentiment_windows?.["15m"]?.indication ||
        "N/A";

      const actionsCell = `
        <div class="d-flex align-items-center justify-content-center gap-1 flex-nowrap">
          <button type="button"
            class="btn btn-link btn-sm p-0 px-1 text-primary view-details-watchlist"
            data-record="${payloadEnc}"
            data-bs-toggle="tooltip"
            title="Snapshot Details">
            <i class="bi bi-info-circle"></i>
          </button>

          <button type="button"
            class="btn btn-link btn-sm p-0 px-1 text-success js-trade-create"
            data-source="watchlist"
            data-record="${payloadEnc}"
            data-bs-toggle="tooltip"
            title="Create Trade">
            <i class="bi bi-plus-circle"></i>
          </button>
        </div>
      `;

      table.row.add([
        isShortlisted ? 1 : 0,
        symbol,
        item.ltp != null && !isNaN(item.ltp) ? Number(item.ltp).toFixed(2) : "0.00",
        pct(item.gap_pct, 2),
        pct(item.move_pct, 2),
        UI.renderBadge(item.state || "NO_TREND"),
        UI.renderStrength(null, item.strength || "N/A"),
        vwapCell,
        num(item.bar_rvol ?? det?.volume?.bar_rvol ?? det?.volume?.rvol ?? det?.context?.volume?.rvol, 2),

        UI.renderStrength(
          item.adx ?? det?.context?.adx?.value ?? det?.adx?.value,
          item.adx_band || det?.context?.adx?.band || det?.adx?.band || "N/A"
        ),

        UI.renderStrength(
          item.atr ?? det?.context?.atr?.value ?? det?.atr?.value,
          item.atr_band || det?.context?.atr?.band || det?.atr?.band || "N/A"
        ),
        rsiCell,
        bbCell,
        structureCell,
        opt15,
        item.ltp_time || "N/A",
        actionsCell,
      ]);
    });

    table.draw();
    initTooltips("#watchlist-table");
  }

  function bindDetailsClicks() {
    $(document).on("click", ".view-details-watchlist", function (e) {
      e.preventDefault();
      e.stopPropagation();

      let info;
      try {
        const enc = $(this).attr("data-record") || "";
        info = JSON.parse(decodeURIComponent(enc));
      } catch (err) {
        console.error("Invalid details payload", err);
        return;
      }

      const snapshotData = info?.details || {};
      if (!snapshotData || typeof window.renderSnapshotModal !== "function") {
        console.error("renderSnapshotModal is not available");
        return;
      }

      window.renderSnapshotModal(snapshotData);
    });
  }


  $(document).ready(() => {
    registerWatchlistFilters();

    $("#watchlist-table").DataTable({
      responsive: true,
      autoWidth: false,
      pageLength: 20,
      lengthMenu: [[20, 50, 100], [20, 50, 100]],
      dom: "rtip",
      columnDefs: [
        { targets: 0, visible: false },
        { orderable: false, targets: -1 },
      ],
      drawCallback() {
        initTooltips("#watchlist-table");
      },
    });

    $("#wl-search").on("input", function () {
      $("#watchlist-table").DataTable().search(this.value).draw();
    });

    $("#wl-length").on("change", function () {
      $("#watchlist-table").DataTable().page.len(parseInt(this.value, 10) || 20).draw();
    });

    $("#wl-breakout-filter").on("change", function () {
      $("#watchlist-table").DataTable().draw();
    });

    $("#wl-shortlisted-only").on("change", function () {
      $("#watchlist-table").DataTable().draw();
    });

    bindDetailsClicks();
    loadWatchlist();
    setInterval(loadWatchlist, Number(CFG.refreshMs || 30000));
  });

  window.populateWatchlist = populateWatchlist;
})();