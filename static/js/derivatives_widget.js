// static/js/derivatives_widget.js
// Reusable derivatives widget renderer (DB-driven: raw + derived)

window.DerivativesWidget = (function () {

  function _num(v, d = 2) {
    const n = Number(v);
    return (v == null || Number.isNaN(n)) ? "—" : n.toFixed(d);
  }

  function _fmtSigned(v, d = 0) {
    const n = Number(v);
    if (v == null || Number.isNaN(n)) return "—";
    return (n >= 0 ? "+" : "") + n.toFixed(d);
  }

  function _pct(v, d = 2) {
    const n = Number(v);
    if (v == null || Number.isNaN(n)) return "—";
    // accept ratio (0.038) OR percent (3.8)
    const ratio = (Math.abs(n) > 1.0) ? (n / 100.0) : n;
    return (ratio * 100).toFixed(d) + "%";
  }

  function _qs(root, sel) {
    return root.querySelector(sel);
  }
  function _qsa(root, sel) {
    return Array.from(root.querySelectorAll(sel));
  }

  function _badgeForBias(biasOrIndication) {
    const s = String(biasOrIndication || "neutral").toLowerCase();
    const m = { bullish: "BUY", bearish: "SELL", neutral: "NO_TREND" };
    if (window.UI && typeof UI.renderBadge === "function") return UI.renderBadge(m[s] || "NO_TREND");
    return (m[s] || "NO_TREND");
  }

  function _biasFromFutLabel(label) {
    const s = String(label || "").toUpperCase();
    if (s === "LONG_BUILDUP" || s === "SHORT_COVERING") return "bullish";
    if (s === "SHORT_BUILDUP" || s === "LONG_UNWINDING") return "bearish";
    return "neutral";
  }

  function _pickBestWindow(winObj, keys) {
    for (const k of keys) {
      const w = winObj && winObj[k];
      if (w && w.status === "ok") return { key: k, w };
    }
    // fallback: any ok
    if (winObj) {
      for (const k of Object.keys(winObj)) {
        const w = winObj[k];
        if (w && w.status === "ok") return { key: k, w };
      }
    }
    return null;
  }

  function _renderOiChangeChart(host, rows, ceTot, peTot) {
    host.innerHTML = "";
    if (!Array.isArray(rows) || rows.length === 0) {
      host.innerHTML = `<div class="text-muted" style="font-size:12px;">No OI window</div>`;
      return;
    }

    const pts = rows.map(r => {
      const ce = Number(r?.ce_oi_chg ?? 0);
      const pe = Number(r?.pe_oi_chg ?? 0);
      return {
        strike: (r?.strike ?? "—"),
        ce: Number.isNaN(ce) ? 0 : ce,
        pe: Number.isNaN(pe) ? 0 : pe,
        ce_oi: r?.ce_oi ?? null,
        pe_oi: r?.pe_oi ?? null,
        ce_symbol: r?.ce_symbol ?? null,
        pe_symbol: r?.pe_symbol ?? null,
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

    const toH = (delta) => Math.max(1, Math.round((Math.abs(delta) / maxAbs) * 50));

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

    pts.forEach(p => {
      const ceH = toH(p.ce);
      const peH = toH(p.pe);

      const ceCls = (p.ce >= 0) ? "oi-pos" : "oi-neg";
      const peCls = (p.pe >= 0) ? "oi-pos" : "oi-neg";

      const ceTip = [
        p.ce_symbol ? p.ce_symbol : null,
        `ΔOI=${_fmtSigned(p.ce, 0)}`,
        (p.ce_oi != null ? `OI=${p.ce_oi}` : null),
      ].filter(Boolean).join(" | ");

      const peTip = [
        p.pe_symbol ? p.pe_symbol : null,
        `ΔOI=${_fmtSigned(p.pe, 0)}`,
        (p.pe_oi != null ? `OI=${p.pe_oi}` : null),
      ].filter(Boolean).join(" | ");

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

    host.innerHTML = html;

    if (window.UI && typeof UI.initTooltips === "function") {
      // scope: nearest modal or widget container
      UI.initTooltips("body");
    }
  }

  function render(root, payload) {
    // payload format from /dashboard/derivatives/data:
    // { symbol, snapshot_time, chain: { raw: {...}, derived: {...} } }

    const symbol = payload?.symbol || "—";
    const snapTime = payload?.snapshot_time || "—";

    const chain = payload?.chain || {};
    const raw = chain.raw || {};
    const derived = chain.derived || {};

    // DOM nodes (scoped)
    const $sum = _qs(root, "[data-summary-body]");
    const $fut = _qs(root, "[data-future-body]");
    const $snapSmall = _qs(root, "[data-snap-small]");

    const $optOverall = _qs(root, "[data-opt-sent-overall]");
    const $optDriver = _qs(root, "[data-opt-sent-driver]");
    const $optPcr = _qs(root, "[data-opt-sent-pcr]");
    const $optWins = _qs(root, "[data-opt-sent-windows]");

    const $oiCe = _qs(root, "[data-oi-ce-delta]");
    const $oiPe = _qs(root, "[data-oi-pe-delta]");
    const $oiChart = _qs(root, "[data-oi-chart]");

    const $ladderAtm = _qs(root, "[data-ladder-atm]");
    const $ladderWin = _qs(root, "[data-ladder-win]");
    const $ladderCalls = _qs(root, "[data-ladder-calls]");
    const $ladderPuts = _qs(root, "[data-ladder-puts]");

    const $json = _qs(root, "[data-json]");

    // reset
    if ($sum) $sum.innerHTML = "";
    if ($fut) $fut.innerHTML = "";
    if ($optWins) $optWins.innerHTML = "";
    if ($ladderCalls) $ladderCalls.innerHTML = "";
    if ($ladderPuts) $ladderPuts.innerHTML = "";
    if ($oiChart) $oiChart.innerHTML = "";
    if ($snapSmall) $snapSmall.textContent = snapTime ? `Snapshot: ${snapTime}` : "";

    // SUMMARY (use derived.options_lite if present)
    const spot = raw?.spot_price;
    const futRaw = raw?.future || {};

    const optLite = derived?.options_lite || {};
    const atm = optLite?.atm_strike ?? "—";

    const pcr = (typeof optLite?.pcr === "number") ? optLite.pcr.toFixed(3) : "—";
    const support = optLite?.support ?? "—";
    const resistance = optLite?.resistance ?? "—";
    const maxPain = optLite?.max_pain ?? "—";

    if ($sum) {
      $sum.insertAdjacentHTML("beforeend", `<tr><th style="width:12rem;">Symbol</th><td>${symbol}</td></tr>`);
      $sum.insertAdjacentHTML("beforeend", `<tr><th>Spot</th><td>${spot != null ? _num(spot, 2) : "—"}</td></tr>`);
      $sum.insertAdjacentHTML("beforeend", `<tr><th>ATM</th><td>${atm}</td></tr>`);
      $sum.insertAdjacentHTML("beforeend", `<tr><th>PCR</th><td>${pcr}</td></tr>`);
      $sum.insertAdjacentHTML("beforeend", `<tr><th>Support</th><td>${support}</td></tr>`);
      $sum.insertAdjacentHTML("beforeend", `<tr><th>Resistance</th><td>${resistance}</td></tr>`);
      $sum.insertAdjacentHTML("beforeend", `<tr><th>MaxPain</th><td>${maxPain}</td></tr>`);
    }

    // FUTURE + FUT SENTIMENT
    if ($fut) {
      $fut.insertAdjacentHTML("beforeend", `<tr><th style="width:12rem;">Instrument</th><td>${futRaw?.instrument ?? "—"}</td></tr>`);
      $fut.insertAdjacentHTML("beforeend", `<tr><th>Last Price</th><td>${(futRaw?.last_price ?? "—")}</td></tr>`);
      $fut.insertAdjacentHTML("beforeend", `<tr><th>OI</th><td>${(futRaw?.oi ?? "—")}</td></tr>`);
      $fut.insertAdjacentHTML("beforeend", `<tr><th>Volume</th><td>${(futRaw?.volume ?? "—")}</td></tr>`);
      $fut.insertAdjacentHTML("beforeend", `<tr><th>Expiry</th><td>${(futRaw?.expiry ?? "—")}</td></tr>`);

      const futWins = derived?.future_sentiment_windows || {};
      const futPriority = ["sod", "60m", "15m", "5m"];
      const bestF = _pickBestWindow(futWins, futPriority);

      $fut.insertAdjacentHTML("beforeend", `<tr><th colspan="2" class="text-muted" style="font-weight:600;">FUT Sentiment</th></tr>`);

      if (!bestF) {
        $fut.insertAdjacentHTML("beforeend", `<tr><th>Overall</th><td class="text-muted">—</td></tr>`);
      } else {
        const bias = _biasFromFutLabel(bestF.w?.label);
        const overallHtml =
          `${_badgeForBias(bias)}
           <span class="ms-1">${bestF.w?.label || "—"}</span>
           <small class="text-muted ms-1">(${bestF.key})</small>`;
        $fut.insertAdjacentHTML("beforeend", `<tr><th>Overall</th><td>${overallHtml}</td></tr>`);
      }

      // windows mini-table
      const renderRow = (k, w) => {
        if (!w || w.status !== "ok") {
          return `<tr>
            <td>${k}</td><td class="text-muted">—</td><td class="text-muted">—</td><td class="text-muted">—</td><td class="text-muted">—</td>
          </tr>`;
        }
        const bias = _biasFromFutLabel(w.label);
        return `<tr>
          <td>${k}</td>
          <td>${_badgeForBias(bias)}</td>
          <td>${w.label || "—"}</td>
          <td>${(typeof w.fut_ltp_delta === "number") ? _fmtSigned(w.fut_ltp_delta, 2) : "—"}</td>
          <td>${(typeof w.fut_oi_delta === "number") ? _fmtSigned(w.fut_oi_delta, 0) : "—"}</td>
        </tr>`;
      };

      const futTable = `
        <div class="table-responsive">
          <table class="table table-sm table-bordered mb-0">
            <thead>
              <tr>
                <th style="width:4.5rem;">Win</th>
                <th style="width:4.5rem;">Bias</th>
                <th>Label</th>
                <th style="width:6rem;">ΔLTP</th>
                <th style="width:6rem;">ΔOI</th>
              </tr>
            </thead>
            <tbody>
              ${futPriority.map(k => renderRow(k, futWins[k])).join("")}
            </tbody>
          </table>
        </div>
      `;
      $fut.insertAdjacentHTML("beforeend", `<tr><th>Windows</th><td>${futTable}</td></tr>`);
    }

    // OPTION SENTIMENT
    const optWins = derived?.option_sentiment_windows || {};
    const winPriority = ["sod", "60m", "15m", "5m"];
    const best = _pickBestWindow(optWins, winPriority);

    if (!$optOverall || !$optDriver || !$optPcr || !$optWins) {
      // widget missing sentiment section (shouldn't happen)
    } else if (!best) {
      $optOverall.innerHTML = "—";
      $optDriver.textContent = "—";
      $optPcr.textContent = "—";
      $optWins.innerHTML = `<tr><td colspan="6" class="text-muted">No sentiment windows</td></tr>`;
    } else {
      const conf = (typeof best.w?.strength === "number")
        ? ` <small class="text-muted">(${Math.round(best.w.strength * 100)}%)</small>`
        : "";
      $optOverall.innerHTML = _badgeForBias(best.w?.indication) + conf;

      const dLabel = best.w?.driver?.label || best.w?.driver?.key || "—";
      const dShare = (typeof best.w?.driver?.share === "number") ? Math.round(best.w.driver.share * 100) : null;
      $optDriver.textContent = (dShare != null) ? `${dLabel} (${dShare}%)` : dLabel;

      const pNow = (typeof best.w?.pcr_now === "number") ? best.w.pcr_now.toFixed(3) : "—";
      const pDel = (typeof best.w?.pcr_delta === "number") ? ` (${_fmtSigned(best.w.pcr_delta, 3)})` : "";
      $optPcr.textContent = `${pNow}${pDel}`;

      const renderWinRow = (k, w) => {
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
        const strength = (typeof w.strength === "number") ? `${Math.round(w.strength * 100)}%` : "—";
        const drvLabel = w?.driver?.label || w?.driver?.key || "—";
        const drvShare = (typeof w?.driver?.share === "number") ? `${Math.round(w.driver.share * 100)}%` : null;
        const drv = drvShare ? `${drvLabel} (${drvShare})` : drvLabel;
        const pNow2 = (typeof w.pcr_now === "number") ? w.pcr_now.toFixed(3) : "—";
        const pDel2 = (typeof w.pcr_delta === "number") ? _fmtSigned(w.pcr_delta, 3) : "—";
        return `<tr>
          <td>${k}</td>
          <td>${_badgeForBias(w.indication)}</td>
          <td>${strength}</td>
          <td>${drv}</td>
          <td>${pNow2}</td>
          <td>${pDel2}</td>
        </tr>`;
      };

      $optWins.innerHTML = winPriority.map(k => renderWinRow(k, optWins[k])).join("");
    }

    // OI WINDOW (prefer 15m, else first ok)
    const oiWins = derived?.oi_windows || {};
    let oiPick = oiWins?.["15m"] || oiWins?.["15"] || oiWins?.["60m"] || oiWins?.["sod"] || null;
    if (!oiPick && oiWins && typeof oiWins === "object") {
      const keys = Object.keys(oiWins);
      if (keys.length) oiPick = oiWins[keys[0]];
    }

    if (oiPick && typeof oiPick === "object") {
      const t = oiPick.totals || {};
      if ($oiCe) $oiCe.textContent = (t.ce_oi_chg != null) ? _fmtSigned(t.ce_oi_chg, 0) : "—";
      if ($oiPe) $oiPe.textContent = (t.pe_oi_chg != null) ? _fmtSigned(t.pe_oi_chg, 0) : "—";
      if ($oiChart) _renderOiChangeChart($oiChart, oiPick.rows || [], t.ce_oi_chg, t.pe_oi_chg);
    } else {
      if ($oiCe) $oiCe.textContent = "—";
      if ($oiPe) $oiPe.textContent = "—";
      if ($oiChart) $oiChart.innerHTML = `<div class="text-muted" style="font-size:12px;">No OI window</div>`;
    }

    // OPTION LADDER (your “full chain = ±5 strikes” requirement)
    const ladder = derived?.option_ladder || {};
    if ($ladderAtm) $ladderAtm.textContent = (ladder.atm_strike != null) ? String(ladder.atm_strike) : "—";
    if ($ladderWin) $ladderWin.textContent = (ladder.window != null) ? String(ladder.window) : "—";

    const calls = Array.isArray(ladder.calls) ? ladder.calls : [];
    const puts = Array.isArray(ladder.puts) ? ladder.puts : [];

    const renderLegRow = (leg) => {
      return `<tr>
        <td>${leg?.strike ?? "—"}</td>
        <td>${(leg?.oi != null) ? _num(leg.oi, 0) : "—"}</td>
        <td>${(leg?.oi_chg != null) ? _fmtSigned(leg.oi_chg, 0) : "—"}</td>
        <td>${(leg?.ltp != null) ? _num(leg.ltp, 2) : "—"}</td>
        <td class="text-muted">${leg?.symbol ?? "—"}</td>
      </tr>`;
    };

    if ($ladderCalls) {
      $ladderCalls.innerHTML = calls.length
        ? calls.map(renderLegRow).join("")
        : `<tr><td colspan="5" class="text-muted">No calls</td></tr>`;
    }

    if ($ladderPuts) {
      $ladderPuts.innerHTML = puts.length
        ? puts.map(renderLegRow).join("")
        : `<tr><td colspan="5" class="text-muted">No puts</td></tr>`;
    }

    // JSON
    if ($json) $json.textContent = JSON.stringify(payload, null, 2);

    if (window.UI && typeof UI.initTooltips === "function") {
      // init tooltips for anything newly inserted
      UI.initTooltips("body");
    }
  }

  async function loadInto(rootEl, symbol, asof) {
    const url = `/dashboard/derivatives/data?symbol=${encodeURIComponent(symbol)}&asof=${encodeURIComponent(asof || "")}`;
    const res = await fetch(url, { credentials: "same-origin" });
    const json = await res.json();
    const data = json && json.data ? json.data : null;
    if (!data) {
      // clean "no data"
      const sum = rootEl.querySelector("[data-summary-body]");
      if (sum) sum.innerHTML = `<tr><td class="text-muted">No derivatives found</td><td class="text-muted">—</td></tr>`;
      return;
    }
    render(rootEl, data);
  }

  return { render, loadInto };
})();
