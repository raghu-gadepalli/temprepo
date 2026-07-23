// static/js/watchlist.js
// Watchlist page logic

(function () {
  "use strict";

  const CFG = window.WATCHLIST_CONFIG || {
    dataUrl: "/dashboard/watchlist/data",
  };

  let wlAuctionMap = {};
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
    const side = accepted?.side || structure?.candidate?.side || structure?.raw?.side || "NEUTRAL";
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

      const auction = wlAuctionMap[symbol] || {};
      const action = String(auction.action || "NO_LOCAL_OPPORTUNITY").toUpperCase();
      const side = String(auction.side || "NONE").toUpperCase();

      if (sel === "LOCAL_CONFIRMED") return action === "LOCAL_CONFIRMED";
      if (sel === "LOCAL_WATCH") return action === "LOCAL_WATCH";
      if (sel === "BUY") return side === "BUY";
      if (sel === "SELL") return side === "SELL";

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

    wlAuctionMap = {};
    wlShortlistMap = {};

    data.forEach((item) => {
      const det = item.details || {};
      const symbol = item.symbol || "N/A";
      const structure = det?.structure || {};
      const auction = det?.auction || {};
      const decision = auction?.decision || {};

      wlAuctionMap[symbol] = {
        action: decision.action || "NO_LOCAL_OPPORTUNITY",
        side: decision.side || "NONE",
        state: auction?.state?.current || "UNKNOWN",
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
      const raw = structure?.raw || {};
      const accepted = structure?.accepted || {};
      const candidate = structure?.candidate || {};
      const boundary = auction?.boundary || {};
      const acceptedRange = accepted?.range || {};

      const structureTip = [
        `Accepted: ${accepted.state || "UNKNOWN"} / frozen ${accepted.frozen === true ? "YES" : "NO"}`,
        `Accepted range: ${num(acceptedRange.low)} - ${num(acceptedRange.high)}`,
        `Raw: ${raw.state || "UNKNOWN"} / ${raw.side || "NEUTRAL"}`,
        `Candidate: ${candidate.status || "NONE"} / ${candidate.side || "NEUTRAL"}`,
        `Auction: ${auction?.state?.current || "UNKNOWN"}`,
        `Decision: ${decision.action || "NO_LOCAL_OPPORTUNITY"} / ${decision.family || "NONE"} / ${decision.side || "NONE"}`,
        `Boundary: ${boundary.status || "NONE"} / ${boundary.boundary_side || "NONE"} / ${num(boundary.boundary_price)}`,
      ].join(" | ");

      const structureCell = `
        <span data-bs-toggle="tooltip" title="${esc(structureTip)}">
          ${esc(structureLabel)}
        </span>
      `;

      const vwapVal = det?.indicators?.vwap?.value;
      const vwapPct = det?.indicators?.vwap?.distance_pct ?? item.vwap_pct;
      const vwapCell = (() => {
        const v = vwapVal != null && !isNaN(vwapVal) ? Number(vwapVal).toFixed(2) : "N/A";
        const tip = `Δ vs VWAP: ${pct(vwapPct, 2)}`;
        return `<span data-bs-toggle="tooltip" title="${esc(tip)}">${v}</span>`;
      })();

      const rsiVal = item.rsi ?? det?.indicators?.rsi?.value;
      const rsiZone = item.rsi_zone || det?.indicators?.rsi?.zone || "N/A";
      const rsiCell = (() => {
        const v = rsiVal != null && !isNaN(rsiVal) ? Number(rsiVal).toFixed(2) : "N/A";
        return `<span data-bs-toggle="tooltip" title="${esc(rsiZone)}">${v}</span>`;
      })();

      const bb = det?.indicators?.bollinger || {};
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
        pct(
          item.gap_pct ?? ((Number(det?.levels?.today?.open) - Number(det?.levels?.prev_day?.close)) / Number(det?.levels?.prev_day?.close) * 100),
          2
        ),
        pct(item.move_pct ?? det?.market_windows?.sod?.move_pct, 2),
        UI.renderBadge(item.state || "NO_TREND"),
        UI.renderStrength(null, item.strength || "N/A"),
        vwapCell,
        num(item.bar_rvol ?? det?.volume?.bar_rvol ?? det?.volume?.rvol, 2),

        UI.renderStrength(
          item.adx ?? det?.indicators?.adx?.value,
          item.adx_band || det?.indicators?.adx?.band || "N/A"
        ),

        UI.renderStrength(
          item.atr ?? det?.indicators?.atr?.value,
          item.atr_band || det?.indicators?.atr?.band || "N/A"
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