// static/js/audit_timeline.js
// Shared read-only audit timeline modal for Signals and Positions/Trades.

(function () {
  "use strict";

  function esc(s) {
    return String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/'/g, "&#39;")
      .replace(/"/g, "&quot;");
  }

  function text(v, fallback = "—") {
    const s = String(v ?? "").trim();
    return s ? s : fallback;
  }

  function getCfg() {
    return window.AUDIT_TIMELINE_CONFIG || {};
  }

  function setVisible(id, visible) {
    const el = document.getElementById(id);
    if (!el) return;
    el.classList.toggle("d-none", !visible);
  }

  function clearTimeline() {
    $("#auditTimelineBody").html("");
    $("#auditTimelinePayload").text("{}");
    $("#auditTimelineSelectedLabel").text("—");
    setVisible("auditTimelineLoading", false);
    setVisible("auditTimelineEmpty", false);
    setVisible("auditTimelineError", false);
  }

  function confidence(v) {
    const n = Number(v);
    return Number.isFinite(n) ? n.toFixed(2) : "—";
  }

  function timeOnly(ts) {
    const s = String(ts || "").trim();
    if (!s) return "—";

    const iso = s.match(/T(\d{1,2}:\d{2})(?::\d{2})?/);
    if (iso) return iso[1];

    const spaced = s.match(/\s(\d{1,2}:\d{2})(?::\d{2})?/);
    if (spaced) return spaced[1];

    const plain = s.match(/^(\d{1,2}:\d{2})(?::\d{2})?$/);
    if (plain) return plain[1];

    return s;
  }

  function stateCell(row) {
    const prev = text(row.previous_state, "");
    const next = text(row.new_state, "");
    if (prev && next) return `${esc(prev)} → ${esc(next)}`;
    return esc(next || prev || "—");
  }

  function rowLabel(row) {
    return [
      timeOnly(row.ts),
      text(row.evaluation_stage, ""),
      text(row.action, ""),
      text(row.reason_code, "")
    ].filter(Boolean).join(" • ");
  }

  function rowHtml(row, idx) {
    const payload = encodeURIComponent(JSON.stringify(row.payload_json || {}));
    const activeClass = idx === 0 ? "table-active" : "";
    return `
      <tr class="audit-row ${activeClass}" data-payload="${payload}" data-label="${esc(rowLabel(row))}">
        <td class="small">${esc(timeOnly(row.ts))}</td>
        <td class="small">${esc(text(row.evaluation_stage))}</td>
        <td class="small fw-semibold">${esc(text(row.action))}</td>
        <td class="small">${stateCell(row)}</td>
        <td class="small">${esc(text(row.reason_code))}</td>
        <td class="small">${esc(text(row.reason_text))}</td>
        <td class="small text-end">${confidence(row.confidence)}</td>
      </tr>
    `;
  }

  function setSelectedPayloadFromRow($row) {
    if (!$row || !$row.length) return;

    $("#auditTimelineTable .audit-row").removeClass("table-active");
    $row.addClass("table-active");

    const raw = $row.attr("data-payload") || "%7B%7D";
    const label = $row.attr("data-label") || "Selected row";

    let payload = {};
    try { payload = JSON.parse(decodeURIComponent(raw)); } catch (_) { payload = {}; }

    $("#auditTimelineSelectedLabel").text(label);
    $("#auditTimelinePayload").text(JSON.stringify(payload, null, 2));
  }

  function renderRows(rows) {
    if (!Array.isArray(rows) || rows.length === 0) {
      $("#auditTimelineBody").html("");
      setVisible("auditTimelineEmpty", true);
      return;
    }

    $("#auditTimelineBody").html(rows.map(rowHtml).join(""));
    setSelectedPayloadFromRow($("#auditTimelineTable .audit-row").first());
  }

  async function openAuditTimeline(opts) {
    const cfg = getCfg();
    const dataUrl = cfg.dataUrl || "/dashboard/audit/timeline";
    const modalEl = document.getElementById("auditTimelineModal");
    if (!modalEl) return;

    const title = opts?.title || opts?.symbol || opts?.entityId || "Audit";
    const subtitle = opts?.subtitle || "";

    $("#auditTimelineTitle").text(title);
    $("#auditTimelineSubtitle").text(subtitle);

    clearTimeline();
    setVisible("auditTimelineLoading", true);

    bootstrap.Modal.getOrCreateInstance(modalEl).show();

    const url = new URL(dataUrl, window.location.origin);
    if (opts?.entityId) url.searchParams.set("entity_id", opts.entityId);
    if (opts?.relatedId) url.searchParams.set("related_id", opts.relatedId);
    if (opts?.symbol) url.searchParams.set("symbol", opts.symbol);
    if (opts?.userid) url.searchParams.set("userid", opts.userid);
    if (opts?.startTime) url.searchParams.set("start_time", opts.startTime);
    if (opts?.endTime) url.searchParams.set("end_time", opts.endTime);
    url.searchParams.set("limit", String(opts?.limit || cfg.limit || 300));

    try {
      const resp = await fetch(url.toString(), { credentials: "same-origin" });
      const payload = await resp.json().catch(() => ({}));

      setVisible("auditTimelineLoading", false);

      if (!resp.ok || payload.status !== "success") {
        console.error("audit timeline failed", resp.status, payload);
        setVisible("auditTimelineError", true);
        return;
      }

      renderRows(payload.data || []);
    } catch (err) {
      console.error("audit timeline failed", err);
      setVisible("auditTimelineLoading", false);
      setVisible("auditTimelineError", true);
    }
  }

  $(document).on("click", ".js-audit-timeline", function (e) {
    e.preventDefault();

    const raw = $(this).attr("data-record") || "{}";
    let opts = {};
    try {
      opts = JSON.parse(decodeURIComponent(raw));
    } catch (_) {
      try { opts = JSON.parse(raw); } catch (_) { opts = {}; }
    }

    openAuditTimeline(opts);
  });

  $(document).on("click", "#auditTimelineTable .audit-row", function () {
    setSelectedPayloadFromRow($(this));
  });

  window.openAuditTimeline = openAuditTimeline;
})();
