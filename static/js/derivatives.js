// static/js/derivatives.js
// Derivatives page renderer (Option D):
// - Row1: Summary (4) + Future (8, includes future sentiment windows)
// - Row2: Option Sentiment (full width)
// - OI card: Tabs (Table/Chart)

$(document).ready(() => {
  // Tabs default
  setOiTab("table");

  $("#deriv-tab-table").on("click", () => setOiTab("table"));
  $("#deriv-tab-chart").on("click", () => setOiTab("chart"));

  // Load handlers
  $("#deriv-load").on("click", () => loadDerivatives());

  $("#deriv-symbol, #deriv-asof").on("keydown", (e) => {
    if (e.key === "Enter") loadDerivatives();
  });

  // Auto-load on page open
  loadDerivatives();
});

function setOiTab(which) {
  const isTable = which === "table";

  $("#deriv-oi-view-table").toggleClass("d-none", !isTable);
  $("#deriv-oi-view-chart").toggleClass("d-none", isTable);

  // keep simple button state (no style overhaul)
  $("#deriv-tab-table").toggleClass("btn-outline-secondary", !isTable).toggleClass("btn-secondary", isTable);
  $("#deriv-tab-chart").toggleClass("btn-outline-secondary", isTable).toggleClass("btn-secondary", !isTable);
}

// ------------------------------
// Helpers
// ------------------------------
function _num(v, d = 2) {
  const n = Number(v);
  return (v == null || Number.isNaN(n)) ? "—" : n.toFixed(d);
}

function _fmtSigned(v, d = 3) {
  const n = Number(v);
  if (v == null || Number.isNaN(n)) return "—";
  return (n >= 0 ? "+" : "") + n.toFixed(d);
}

function _safeGet(obj, pathArr, defVal) {
  try {
    let cur = obj;
    for (let i = 0; i < pathArr.length; i++) {
      if (cur == null || typeof cur !== "object") return defVal;
      cur = cur[pathArr[i]];
    }
    return (cur === undefined || cur === null) ? defVal : cur;
  } catch (e) {
    return defVal;
  }
}

function _mapBiasToBadge(indication) {
  if (!window.UI || typeof UI.renderBadge !== "function") {
    return (indication || "—");
  }
  const s = String(indication || "neutral").toLowerCase();
  const m = { bullish: "BUY", bearish: "SELL", neutral: "NO_TREND" };
  return UI.renderBadge(m[s] || "NO_TREND");
}

// ------------------------------
// Fetch + render
// ------------------------------
function loadDerivatives() {
  const symbol = String($("#deriv-symbol").val() || "").trim().toUpperCase();
  const asof = String($("#deriv-asof").val() || "").trim();

  if (!symbol) {
    $("#deriv-snapshot").text("—");
    $("#deriv-json").text(JSON.stringify({ status: "error", reason: "missing_symbol" }, null, 2));
    return;
  }

  // Put symbol/asof in URL (so /dashboard/derivatives?symbol=INFY works)
  const u = new URL(window.location.href);
  u.searchParams.set("symbol", symbol);
  if (asof) u.searchParams.set("asof", asof);
  else u.searchParams.delete("asof");
  window.history.replaceState({}, "", u.toString());

  // Reset UI quickly
  $("#deriv-snapshot").text("—");
  $("#deriv-snapshot-small").text("");

  $("#deriv-summary-body").empty();
  $("#deriv-future-body").empty();
  $("#deriv-fut-windows").empty();

  $("#deriv-sent-overall").html("—");
  $("#deriv-sent-driver").text("—");
  $("#deriv-sent-pcr").text("—");
  $("#deriv-sent-windows").empty();

  $("#deriv-oi-window").text("—");
  $("#deriv-oi-ce-delta").text("—");
  $("#deriv-oi-pe-delta").text("—");
  $("#deriv-oi-rows").empty();
  $("#deriv-oi-chart").empty();

  // Fetch
  const url = `/dashboard/derivatives/data?symbol=${encodeURIComponent(symbol)}&asof=${encodeURIComponent(asof)}`;
  fetch(url, { credentials: "same-origin" })
    .then(r => r.json())
    .then(json => {
      if (!json || json.status !== "success") {
        $("#deriv-json").text(JSON.stringify(json || { status: "error" }, null, 2));
        return;
      }
      const data = json.data || {};
      renderDerivativesPage(data);
      $("#deriv-json").text(JSON.stringify(json, null, 2));

      if (window.UI && typeof UI.initTooltips === "function") {
        UI.initTooltips("#derivativesPage");
      }
    })
    .catch(err => {
      console.error("derivatives fetch failed:", err);
      $("#deriv-json").text(JSON.stringify({ status: "error", reason: "fetch_failed" }, null, 2));
    });
}

function renderDerivativesPage(data) {
  const chain = data.chain || {};
  const symbol = (data.symbol || "").toUpperCase();
  const snapTime = data.snapshot_time || "—";

  $("#deriv-snapshot").text(snapTime || "—");
  $("#deriv-snapshot-small").text(snapTime ? `Snapshot: ${snapTime}` : "");

  // ------------------------------
  // Summary (uses options_lite)
  // ------------------------------
  const optLite = (chain.options_lite && typeof chain.options_lite === "object") ? chain.options_lite : null;
  const spot = chain.spot_price;

  const atm = optLite ? (optLite.atm_strike ?? optLite.atm ?? "—") : "—";
  const pcr = (optLite && typeof optLite.pcr === "number") ? optLite.pcr : null;
  const support = optLite ? (optLite.support ?? "—") : "—";
  const resistance = optLite ? (optLite.resistance ?? "—") : "—";
  const maxpain = optLite ? (optLite.max_pain ?? optLite.maxpain ?? "—") : "—";

  const $sb = $("#deriv-summary-body").empty();
  $sb.append(`<tr><th style="width:12rem;">Symbol</th><td>${symbol || "—"}</td></tr>`);
  $sb.append(`<tr><th>Spot</th><td>${spot != null ? _num(spot, 2) : "—"}</td></tr>`);
  $sb.append(`<tr><th>ATM</th><td>${atm}</td></tr>`);
  $sb.append(`<tr><th>PCR</th><td>${pcr != null ? Number(pcr).toFixed(3) : "—"}</td></tr>`);
  $sb.append(`<tr><th>Support</th><td>${support}</td></tr>`);
  $sb.append(`<tr><th>Resistance</th><td>${resistance}</td></tr>`);
  $sb.append(`<tr><th>MaxPain</th><td>${maxpain}</td></tr>`);

  // ------------------------------
  // Future (instrument info)
  // ------------------------------
  const fut = chain.future || {};
  const $fb = $("#deriv-future-body").empty();
  $fb.append(`<tr><th style="width:12rem;">Instrument</th><td>${fut.instrument ?? "—"}</td></tr>`);
  $fb.append(`<tr><th>Last Price</th><td>${(fut.last_price ?? fut.ltp) ?? "—"}</td></tr>`);
  $fb.append(`<tr><th>OI</th><td>${fut.oi ?? "—"}</td></tr>`);
  $fb.append(`<tr><th>Volume</th><td>${fut.volume ?? "—"}</td></tr>`);
  $fb.append(`<tr><th>Expiry</th><td>${fut.expiry ?? "—"}</td></tr>`);

  // ------------------------------
  // Future sentiment windows (sod/60m/15m/5m)
  // ------------------------------
  const futWins = (chain.future_sentiment_windows && typeof chain.future_sentiment_windows === "object")
    ? chain.future_sentiment_windows
    : {};
  const winPriority = ["sod", "60m", "15m", "5m"];
  const $fw = $("#deriv-fut-windows").empty();

  const renderFutWinRow = (k, w) => {
    if (!w || w.status !== "ok") {
      return `<tr>
        <td>${k}</td>
        <td class="text-muted">—</td>
        <td class="text-muted">—</td>
        <td class="text-muted">—</td>
      </tr>`;
    }
    const lbl = w.label || "—";
    const dltp = (typeof w.fut_ltp_delta === "number") ? _fmtSigned(w.fut_ltp_delta, 2) : "—";
    const doi  = (typeof w.fut_oi_delta === "number") ? _fmtSigned(w.fut_oi_delta, 0) : "—";
    return `<tr>
      <td>${k}</td>
      <td>${lbl}</td>
      <td>${dltp}</td>
      <td>${doi}</td>
    </tr>`;
  };

  let anyFut = false;
  winPriority.forEach(k => { if (futWins && futWins[k]) anyFut = true; });

  if (!anyFut && (!futWins || Object.keys(futWins).length === 0)) {
    $fw.append(`<tr><td colspan="4" class="text-muted">No future sentiment windows</td></tr>`);
  } else {
    // always include priority + any extras
    const keysToShow = [...winPriority];
    Object.keys(futWins || {}).forEach(k => { if (!keysToShow.includes(k)) keysToShow.push(k); });
    keysToShow.forEach(k => $fw.append(renderFutWinRow(k, futWins[k])));
  }

  // ------------------------------
  // Option sentiment windows
  // ------------------------------
  const sentWins = (chain.option_sentiment_windows && typeof chain.option_sentiment_windows === "object")
    ? chain.option_sentiment_windows
    : {};

  const pickBestWindow = () => {
    for (let i = 0; i < winPriority.length; i++) {
      const k = winPriority[i];
      if (sentWins[k] && sentWins[k].status === "ok") return sentWins[k];
    }
    const ks = sentWins ? Object.keys(sentWins) : [];
    for (let i = 0; i < ks.length; i++) {
      const k = ks[i];
      if (sentWins[k] && sentWins[k].status === "ok") return sentWins[k];
    }
    return null;
  };

  const best = pickBestWindow();
  if (!best) {
    $("#deriv-sent-overall").html("—");
    $("#deriv-sent-driver").text("—");
    $("#deriv-sent-pcr").text("—");
  } else {
    const badge = _mapBiasToBadge(best.indication);
    const conf = (typeof best.strength === "number")
      ? ` <small class="text-muted">(${Math.round(best.strength * 100)}%)</small>`
      : "";
    $("#deriv-sent-overall").html(badge + conf);

    const dLabel = _safeGet(best, ["driver", "label"], null) || _safeGet(best, ["driver", "key"], "—");
    const dShare = _safeGet(best, ["driver", "share"], null);
    const dPct = (typeof dShare === "number") ? Math.round(dShare * 100) : null;
    $("#deriv-sent-driver").text(dPct != null ? `${dLabel} (${dPct}%)` : dLabel);

    const pNow = (typeof best.pcr_now === "number") ? Number(best.pcr_now).toFixed(3) : "—";
    const dlt = (typeof best.pcr_delta === "number") ? ` (${_fmtSigned(best.pcr_delta, 3)})` : "";
    $("#deriv-sent-pcr").text(`${pNow}${dlt}`);
  }

  const $sw = $("#deriv-sent-windows").empty();

  const renderSentRow = (k, w) => {
    if (!w || w.status !== "ok") {
      return `<tr>
        <td>${k}</td>
        <td class="text-muted">—</td>
        <td class="text-muted">—</td>
        <td class="text-muted">—</td>
        <td class="text-muted">—</td>
        <td class="text-muted">—</td>
      </tr>`;
    }
    const bias = _mapBiasToBadge(w.indication);
    const strength = (typeof w.strength === "number") ? `${Math.round(w.strength * 100)}%` : "—";

    const drvLabel = _safeGet(w, ["driver", "label"], null) || _safeGet(w, ["driver", "key"], "—");
    const drvShare = _safeGet(w, ["driver", "share"], null);
    const drvPct = (typeof drvShare === "number") ? `${Math.round(drvShare * 100)}%` : null;
    const drv = drvPct ? `${drvLabel} (${drvPct})` : drvLabel;

    const pNow = (typeof w.pcr_now === "number") ? Number(w.pcr_now).toFixed(3) : "—";
    const pDel = (typeof w.pcr_delta === "number") ? _fmtSigned(w.pcr_delta, 3) : "—";

    return `<tr>
      <td>${k}</td>
      <td>${bias}</td>
      <td>${strength}</td>
      <td>${drv}</td>
      <td>${pNow}</td>
      <td>${pDel}</td>
    </tr>`;
  };

  const keysToShow = [...winPriority];
  Object.keys(sentWins || {}).forEach(k => { if (!keysToShow.includes(k)) keysToShow.push(k); });

  const hasAnySent = keysToShow.some(k => sentWins && sentWins[k]);
  if (!hasAnySent && (!sentWins || Object.keys(sentWins).length === 0)) {
    $sw.append(`<tr><td colspan="6" class="text-muted">No sentiment windows</td></tr>`);
  } else {
    keysToShow.forEach(k => $sw.append(renderSentRow(k, sentWins[k])));
  }

  // ------------------------------
  // OI Windows: pick one window to display (prefer 15m, then 5m, then 60m, then sod)
  // ------------------------------
  const oiWins = (chain.oi_windows && typeof chain.oi_windows === "object") ? chain.oi_windows : {};

  const pickOiWindowKey = () => {
    const pref = ["15m", "5m", "60m", "sod"];
    for (let i = 0; i < pref.length; i++) {
      const k = pref[i];
      if (oiWins[k] && Array.isArray(oiWins[k].rows) && oiWins[k].rows.length) return k;
    }
    // fallback: any with rows
    const ks = Object.keys(oiWins || {});
    for (let i = 0; i < ks.length; i++) {
      const k = ks[i];
      if (oiWins[k] && Array.isArray(oiWins[k].rows) && oiWins[k].rows.length) return k;
    }
    return null;
  };

  const oiKey = pickOiWindowKey();
  const oi = oiKey ? oiWins[oiKey] : null;

  $("#deriv-oi-window").text(oiKey || "—");

  const rows = (oi && Array.isArray(oi.rows)) ? oi.rows : [];
  const totals = (oi && oi.totals && typeof oi.totals === "object") ? oi.totals : {};

  $("#deriv-oi-ce-delta").text(totals.ce_oi_chg != null ? _fmtSigned(totals.ce_oi_chg, 0) : "—");
  $("#deriv-oi-pe-delta").text(totals.pe_oi_chg != null ? _fmtSigned(totals.pe_oi_chg, 0) : "—");

  // Table rows
  const $or = $("#deriv-oi-rows").empty();
  if (!rows.length) {
    $or.append(`<tr><td colspan="7" class="text-muted">No OI window</td></tr>`);
    $("#deriv-oi-chart").html(`<div class="text-muted" style="font-size:12px;">No OI window</div>`);
  } else {
    rows.forEach(r => {
      $or.append(`
        <tr>
          <td>${r.strike ?? "—"}</td>
          <td>${r.ce_ltp != null ? _num(r.ce_ltp, 2) : "—"}</td>
          <td>${r.ce_oi ?? "—"}</td>
          <td>${r.ce_oi_chg != null ? _fmtSigned(r.ce_oi_chg, 0) : "—"}</td>
          <td>${r.pe_ltp != null ? _num(r.pe_ltp, 2) : "—"}</td>
          <td>${r.pe_oi ?? "—"}</td>
          <td>${r.pe_oi_chg != null ? _fmtSigned(r.pe_oi_chg, 0) : "—"}</td>
        </tr>
      `);
    });

    // Chart
    renderOiChangeChart(rows);
  }

  // tooltips after DOM
  if (window.UI && typeof UI.initTooltips === "function") {
    UI.initTooltips("#derivativesPage");
  }
}

// ------------------------------
// OI Change Chart (simple HTML bars)
// ------------------------------
function renderOiChangeChart(rows) {
  const $host = $("#deriv-oi-chart").empty();

  if (!Array.isArray(rows) || rows.length === 0) {
    $host.html(`<div class="text-muted" style="font-size:12px;">No OI window</div>`);
    return;
  }

  const pts = rows.map(r => {
    const strike = (r && r.strike != null) ? r.strike : "—";
    const ce = Number(r && r.ce_oi_chg != null ? r.ce_oi_chg : 0);
    const pe = Number(r && r.pe_oi_chg != null ? r.pe_oi_chg : 0);
    return {
      strike,
      ce: Number.isNaN(ce) ? 0 : ce,
      pe: Number.isNaN(pe) ? 0 : pe,
      ce_oi: (r && r.ce_oi != null) ? r.ce_oi : null,
      pe_oi: (r && r.pe_oi != null) ? r.pe_oi : null,
      ce_symbol: r && r.ce_symbol ? r.ce_symbol : null,
      pe_symbol: r && r.pe_symbol ? r.pe_symbol : null
    };
  });

  let maxAbs = 0;
  pts.forEach(p => { maxAbs = Math.max(maxAbs, Math.abs(p.ce), Math.abs(p.pe)); });
  if (!maxAbs || maxAbs < 1) maxAbs = 1;

  const fmt = (n) => {
    const a = Math.abs(n);
    if (a >= 1e7) return (n / 1e7).toFixed(1) + "Cr";
    if (a >= 1e5) return (n / 1e5).toFixed(1) + "L";
    if (a >= 1e3) return (n / 1e3).toFixed(1) + "K";
    return String(Math.round(n));
  };

  const yTop = fmt(maxAbs);
  const yMid = "0";
  const yBot = fmt(-maxAbs);

  let html = `
    <div class="oi-legend">
      <span><span class="oi-dot" style="background:#198754;"></span>Put OI chg</span>
      <span><span class="oi-dot" style="background:#dc3545;"></span>Call OI chg</span>
    </div>

    <div class="oi-chart">
      <div class="oi-chart-grid">
        <div class="oi-ylabels">
          <div style="height: 24px;"></div>
          <div>${yTop}</div>
          <div style="height: 96px;"></div>
          <div>${yMid}</div>
          <div style="height: 96px;"></div>
          <div>${yBot}</div>
        </div>

        <div class="oi-plot">
          <div class="oi-axis-zero"></div>
          <div class="oi-bars">
  `;

  const toH = (delta) => Math.max(1, Math.round((Math.abs(delta) / maxAbs) * 50));

  pts.forEach(p => {
    const ceH = toH(p.ce);
    const peH = toH(p.pe);

    const ceCls = (p.ce >= 0) ? "oi-pos" : "oi-neg";
    const peCls = (p.pe >= 0) ? "oi-pos" : "oi-neg";

    const ceTipParts = [];
    if (p.ce_symbol) ceTipParts.push(p.ce_symbol);
    ceTipParts.push(`ΔOI=${_fmtSigned(p.ce, 0)}`);
    if (p.ce_oi != null) ceTipParts.push(`OI=${p.ce_oi}`);
    const ceTip = ceTipParts.join(" | ");

    const peTipParts = [];
    if (p.pe_symbol) peTipParts.push(p.pe_symbol);
    peTipParts.push(`ΔOI=${_fmtSigned(p.pe, 0)}`);
    if (p.pe_oi != null) peTipParts.push(`OI=${p.pe_oi}`);
    const peTip = peTipParts.join(" | ");

    html += `
      <div class="oi-col">
        <div class="oi-bar oi-call ${ceCls}" style="height:${ceH}%;"
             data-bs-toggle="tooltip" title="${ceTip}"></div>

        <div class="oi-bar oi-put ${peCls}" style="height:${peH}%;"
             data-bs-toggle="tooltip" title="${peTip}"></div>

        <div class="oi-strike">${p.strike}</div>
      </div>
    `;
  });

  html += `
          </div>
        </div>
      </div>
    </div>
  `;

  $host.html(html);

  // tooltips
  if (window.UI && typeof UI.initTooltips === "function") {
    UI.initTooltips("#derivativesPage");
  }
}
