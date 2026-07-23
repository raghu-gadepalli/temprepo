// static/js/trade_edit.js
// Shared trade edit workflow.
// Trade management is config/lifecycle-driven. This modal only allows draft
// quantity edits before submission and displays managed stop/target information.

(function () {
  "use strict";

  let _current = null;
  let _lastTrigger = null;

  function cfg() {
    return window.TRADE_EDIT_CONFIG || window.ORDERS_CONFIG || window.POSITIONS_CONFIG || {};
  }

  function modalId() {
    return cfg().editModalId || "tradeEditModal";
  }

  function byId(id) {
    return document.getElementById(id);
  }

  function setVal(id, v) {
    const el = byId(id);
    if (el) el.value = v == null ? "" : String(v);
  }

  function setText(id, v) {
    const el = byId(id);
    if (el) el.textContent = v == null ? "" : String(v);
  }

  function num(v) {
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  }

  function intOr(v, d = 0) {
    const n = parseInt(v, 10);
    return Number.isFinite(n) ? n : d;
  }

  function fmtNum(v, d = 2) {
    const n = num(v);
    return n == null ? "" : n.toFixed(d);
  }

  function fmtMoney(v) {
    const n = num(v);
    return "₹" + (n == null ? "0.00" : n.toFixed(2));
  }

  function upper(x, def = "") {
    const s = String(x == null ? "" : x).trim();
    return s ? s.toUpperCase() : def;
  }

  function refreshTarget(target) {
    const t = String(target || "").trim().toLowerCase();

    if (t === "orders" && typeof window.loadOrders === "function") {
      window.loadOrders();
      return;
    }
    if (t === "positions" && typeof window.loadPositions === "function") {
      window.loadPositions();
      return;
    }
    if (t === "signals" && typeof window.loadSignals === "function") {
      window.loadSignals();
      return;
    }
  }

  function setStatus(msg, cls) {
    const s = $("#tem-status");
    const f = $("#tem-status-footer");

    s.removeClass("text-danger text-success text-muted text-warning");
    f.removeClass("text-danger text-success text-muted text-warning");

    if (cls) {
      s.addClass(cls);
      f.addClass(cls);
    }

    s.text(msg || "");
    f.text(msg || "");
  }

  function applyModeRules(mode) {
    const isDraft = upper(mode) === "DRAFT";

    $("#tem-qty").prop("disabled", !isDraft);
    $("#tem-product").prop("disabled", true);
    $("#tem-execution-mode").prop("disabled", true);
    $("#tem-entry-price").prop("disabled", true);
    $("#tem-current-stop-price").prop("disabled", true);
    $("#tem-current-target-price").prop("disabled", true);
  }

  function managedStop(row) {
    return row?.current_stop_price ?? row?.trade_management?.current_stop_price ?? null;
  }

  function managedTarget(row) {
    return row?.current_target_price ?? row?.trade_management?.current_target_price ?? null;
  }

  function openModal(row, mode = "draft", triggerEl = null) {
    _current = row || {};
    _lastTrigger = triggerEl || null;

    const m = upper(mode, "DRAFT");
    const tradeId = row?.id ?? row?.trade_id ?? "";

    setVal("tem-trade-id", tradeId);
    setVal("tem-mode", m.toLowerCase());

    setText("tem-head-user", row?.userid || "—");
    setText("tem-head-symbol", row?.symbol || "—");
    setText("tem-head-type", row?.instrument_type || row?.type || "EQ");
    setText("tem-head-side", row?.trade_type || row?.side || "BUY");
    setText("tem-head-price", fmtMoney(row?.entry_price ?? 0));

    setVal("tem-product", row?.product || "MIS");
    setVal("tem-execution-mode", row?.execution_mode || "VIRTUAL");
    setVal("tem-entry-price", fmtNum(row?.entry_price ?? 0));
    setVal("tem-qty", row?.quantity ?? row?.qty ?? "");
    setVal("tem-last-price", fmtNum(row?.last_price ?? 0));
    setVal("tem-last-pnl", fmtNum(row?.last_pnl_value ?? row?.pnl_value ?? row?.pnl ?? 0));
    setVal("tem-current-stop-price", fmtNum(managedStop(row)) || "—");
    setVal("tem-current-target-price", fmtNum(managedTarget(row)) || "—");

    applyModeRules(m);

    setStatus(
      m === "DRAFT"
        ? "Draft mode: only quantity can be edited. Stop/target management is config-driven."
        : "Live trade management is controlled by trade_management and monitor config.",
      "text-muted"
    );

    const modalEl = byId(modalId());
    if (!modalEl) return;

    const modal = bootstrap.Modal.getOrCreateInstance(modalEl, { focus: true });
    modal.show();
  }

  function buildPayload() {
    if (!_current) return null;

    const mode = upper($("#tem-mode").val(), "DRAFT");
    const tradeId = $("#tem-trade-id").val();

    const payload = {
      id: tradeId,
      trade_id: tradeId,
      mode: mode.toLowerCase()
    };

    if (mode === "DRAFT") {
      payload.quantity = intOr($("#tem-qty").val(), 0);
      payload.qty = intOr($("#tem-qty").val(), 0);
    }

    return payload;
  }

  async function save() {
    if (!_current) return;

    const payload = buildPayload();
    const saveBtn = byId("tem-save-btn");
    if (saveBtn) saveBtn.disabled = true;

    try {
      setStatus("Saving changes…", "text-muted");

      const resp = await fetch(cfg().editTradeUrl || "/dashboard/trading/edit_trade", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });

      const result = await resp.json().catch(() => ({}));

      if (!resp.ok || !result.ok) {
        setStatus(result.error || result.reason || result.message || "Save failed.", "text-danger");
        if (saveBtn) saveBtn.disabled = false;
        return;
      }

      setStatus("Changes saved successfully.", "text-success");

      const targets = cfg().refreshTargets || ["orders", "positions"];
      targets.forEach(refreshTarget);

      setTimeout(() => {
        const modalEl = byId(modalId());
        const modal = modalEl ? bootstrap.Modal.getInstance(modalEl) : null;
        if (modal) modal.hide();
      }, 500);

    } catch (err) {
      console.error("trade edit failed:", err);
      setStatus("Save request failed.", "text-danger");
      if (saveBtn) saveBtn.disabled = false;
    }
  }

  function bindGenericEditClicks() {
    $(document).on("click", ".js-trade-edit", function (e) {
      e.preventDefault();
      e.stopPropagation();

      const btn = this;
      const mode = String($(btn).attr("data-mode") || "draft").trim().toLowerCase();
      const enc = $(btn).attr("data-record") || "";

      let row = null;
      try {
        row = JSON.parse(enc);
      } catch (_) {
        try {
          row = JSON.parse(decodeURIComponent(enc));
        } catch (err) {
          console.error("Invalid trade-edit payload", err);
          return;
        }
      }

      openModal(row, mode, btn);
    });
  }

  function setupModalLifecycle() {
    const modalEl = byId(modalId());
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
      _current = null;

      const saveBtn = byId("tem-save-btn");
      if (saveBtn) saveBtn.disabled = false;

      if (_lastTrigger && typeof _lastTrigger.focus === "function") {
        _lastTrigger.focus();
      }
      _lastTrigger = null;
    });
  }

  function bindSave() {
    $(document).on("click", "#tem-save-btn", async function (e) {
      e.preventDefault();
      await save();
    });
  }

  $(document).ready(() => {
    setupModalLifecycle();
    bindGenericEditClicks();
    bindSave();
  });

  window.TradeEditModal = {
    openModal,
    save,
    setStatus
  };
})();
