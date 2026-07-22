// static/js/signalsmodal.js
// Shared symbol-signals modal logic

(function () {
  "use strict";

  function byId(id) {
    return document.getElementById(id);
  }

  function setText(id, val) {
    const el = byId(id);
    if (el) el.textContent = val;
  }

  function setHtml(id, val) {
    const el = byId(id);
    if (el) el.innerHTML = val;
  }

  function esc(v) {
    return String(v ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function txt(v, fallback = "—") {
    if (v === null || v === undefined) return fallback;
    const s = String(v).trim();
    return s || fallback;
  }

  function num(v, d = 2) {
    const n = Number(v);
    return Number.isFinite(n) ? n.toFixed(d) : "—";
  }

  function badgeHtml(value, type) {
    const s = String(value || "").toUpperCase();
    let cls = "bg-secondary";

    if (type === "state") {
      if (s === "OPEN") cls = "bg-success";
      else if (s === "READY") cls = "bg-primary";
      else if (s === "TRACKING") cls = "bg-info text-dark";
      else if (s === "BLOCKED") cls = "bg-warning text-dark";
      else if (s === "STALE") cls = "bg-dark";
      else if (s === "NOT_READY") cls = "bg-secondary";
    } else if (type === "entry") {
      if (s === "ENTER") cls = "bg-success";
      else if (s === "WATCH") cls = "bg-info text-dark";
      else if (s === "LATE") cls = "bg-warning text-dark";
      else if (s === "AVOID") cls = "bg-danger";
    } else if (type === "continue") {
      if (s === "STRONG") cls = "bg-success";
      else if (s === "HOLD_OK") cls = "bg-primary";
      else if (s === "WEAKENING") cls = "bg-warning text-dark";
      else if (s === "LOST") cls = "bg-danger";
      else if (s === "NA") cls = "bg-secondary";
    } else if (type === "resolved") {
      if (s === "ENTER") cls = "bg-success";
      else if (s === "WATCH") cls = "bg-info text-dark";
      else if (s === "LATE") cls = "bg-warning text-dark";
      else if (s === "AVOID") cls = "bg-danger";
    }

    return `<span class="badge state-badge ${cls}">${esc(s || "—")}</span>`;
  }

  function renderContextRows(ctx) {
    const rows = [
      ["HMA", txt(ctx?.hma_state)],
      ["Strength", txt(ctx?.hma_strength)],
      ["RSI", ctx?.rsi == null ? "—" : num(ctx.rsi, 2)],
      ["BB Zone", txt(ctx?.bb_zone)],
    ];

    return rows.map(([k, v]) => `
      <tr>
        <th>${esc(k)}</th>
        <td>${esc(v)}</td>
      </tr>
    `).join("");
  }

  function renderResolvedRows(resolved) {
    const rows = [
      ["Action", badgeHtml(resolved?.action, "resolved")],
      ["Preferred", esc(txt(resolved?.preferred_lifecycle))],
    ];

    return rows.map(([k, v]) => `
      <tr>
        <th>${esc(k)}</th>
        <td>${v}</td>
      </tr>
    `).join("");
  }

  function renderLifecycleRows(lifecycles) {
    const rows = Array.isArray(lifecycles) ? lifecycles : [];
    if (!rows.length) {
      return `<tr><td colspan="7" class="text-center text-muted">—</td></tr>`;
    }

    return rows.map((r) => `
      <tr>
        <td>${esc(txt(r.lifecycle))}</td>
        <td>${badgeHtml(r.state, "state")}</td>
        <td>${esc(txt(r.side))}</td>
        <td>${badgeHtml(r.entry_view, "entry")}</td>
        <td>${badgeHtml(r.continuation_view, "continue")}</td>
        <td>${esc(txt(r.transition))}</td>
        <td>${esc(txt(r.summary))}</td>
      </tr>
    `).join("");
  }

  function renderSignalsModal(data) {
    if (!data) return;

    setText("sig-symbol", txt(data.symbol));
    setText("sig-ltp", `LTP: ${num(data.ltp, 2)}`);
    setText("sig-time", txt(data.snapshot_time));

    setHtml("sig-context-body", renderContextRows(data.context || {}));
    const resolution = data.signal_resolution || {};
    const signals = data.signals || [];

    setHtml("sig-resolved-body", renderResolvedRows(resolution));
    setText("sig-resolved-summary", txt(resolution?.summary));
    setHtml("sig-lifecycle-body", renderLifecycleRows(signals));
    const modalEl = byId("signalsModal");
    if (!modalEl) return;

    const modal = bootstrap.Modal.getOrCreateInstance(modalEl);
    modal.show();
  }

  async function fetchSignalsBySymbol(symbol) {
    const base =
      (window.SIGNALS_CONFIG && window.SIGNALS_CONFIG.dataUrl) ||
      "/dashboard/signals/data";

    const url = new URL(base, window.location.origin);
    url.searchParams.set("symbol", String(symbol || "").trim().toUpperCase());

    const resp = await fetch(url.toString(), { credentials: "same-origin" });
    const payload = await resp.json().catch(() => ({}));

    if (!resp.ok) {
      throw new Error(payload?.reason || `HTTP_${resp.status}`);
    }

    const rows = Array.isArray(payload?.data) ? payload.data : [];
    return rows.length ? rows[0] : null;
  }

  async function openSignalsModalBySymbol(symbol) {
    const sym = String(symbol || "").trim().toUpperCase();
    if (!sym) return;

    try {
      const row = await fetchSignalsBySymbol(sym);
      if (!row) {
        console.warn("No signals row returned for symbol:", sym);
        return;
      }
      renderSignalsModal(row);
    } catch (err) {
      console.error("openSignalsModalBySymbol failed:", err);
    }
  }

  window.renderSignalsModal = renderSignalsModal;
  window.fetchSignalsBySymbol = fetchSignalsBySymbol;
  window.openSignalsModalBySymbol = openSignalsModalBySymbol;
})();