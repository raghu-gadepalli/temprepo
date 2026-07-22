// static/js/trade_exit.js
// Shared trade exit workflow
//
// Supports sources like:
// - position
// - order   (future)
//
/*
Expected button contract:
  class="js-trade-exit"
  data-source="position|order"
  data-record="<encoded json>"

Expected modal ids / fields (default based on current positions exit modal):
  modalId: "tradeExitModal"
  modalLabelId: "tradeExitModalLabel"

  Hidden/text fields:
    #pem-userid
    #pem-userid-head
    #pem-symbol-head
    #pem-side-head
    #pem-price-head
    #pem-type
    #pem-qty
    #pem-last
    #pem-equity-ref
    #pem-reason
    #pem-status
    #pem-status-footer
    #pem-submit-btn
*/

(function () {
  "use strict";

  let _currentSource = null;
  let _currentItem = null;
  let _lastTrigger = null;

  function cfg(source) {
    const src = String(source || _currentSource || "").trim().toLowerCase();

    if (window.TRADE_EXIT_CONFIG) return window.TRADE_EXIT_CONFIG;
    if (src === "position" && window.POSITIONS_CONFIG) return window.POSITIONS_CONFIG;
    if (src === "order" && window.ORDERS_CONFIG) return window.ORDERS_CONFIG;

    return window.POSITIONS_CONFIG || window.ORDERS_CONFIG || {};
  }

  function modalId() {
    return cfg().exitModalId || "tradeExitModal";
  }

  function modalLabelId() {
    return cfg().exitModalLabelId || "tradeExitModalLabel";
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

  function money(v) {
    const n = Number(v);
    return Number.isFinite(n) ? n.toFixed(2) : "0.00";
  }

  function txt(v, fallback = "—") {
    return (v == null || String(v).trim() === "") ? fallback : String(v);
  }

  function normStatus(x) {
    return String(x || "").toUpperCase().trim();
  }

  function netQty(item) {
    const v = Number(item?.open_qty ?? item?.net_qty ?? item?.qty ?? item?.quantity ?? 0);
    return Number.isFinite(v) ? v : 0;
  }

  function canExit(item) {
    const status = normStatus(item?.status);
    const exitStatus = normStatus(item?.exit_status);
    const qty = netQty(item);

    if (!Number.isFinite(qty) || qty === 0) return false;
    if (status === "CLOSED") return false;
    if (exitStatus === "READY" || exitStatus === "SUBMITTED") return false;

    return true;
  }

  function setStatus(msg, cls) {
    const s = $("#pem-status");
    const f = $("#pem-status-footer");

    s.removeClass("text-danger text-success text-muted text-warning");
    f.removeClass("text-danger text-success text-muted text-warning");

    if (cls) {
      s.addClass(cls);
      f.addClass(cls);
    }

    s.text(msg || "");
    f.text(msg || "");
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

  function buildPayloadBase(item) {
    return {
      userid: item?.userid,
      symbol: item?.symbol,
      equity_ref: item?.equity_ref,
      instrument_type: item?.instrument_type || item?.type,
      reason: ($("#pem-reason").val() || "MANUAL_EXIT").trim()
    };
  }

  function refreshTargets() {
    const targets = cfg().refreshTargets || ["orders", "positions"];
    targets.forEach(refreshTarget);
  }

  function openModal(item, source = "position", triggerEl = null) {
    if (!item || !canExit(item)) return;

    _currentSource = String(source || "position").trim().toLowerCase();
    _currentItem = item;
    _lastTrigger = triggerEl || null;

    setVal("pem-userid", txt(item.userid, ""));
    setText("pem-userid-head", txt(item.userid));
    setText("pem-symbol-head", txt(item.symbol));
    setText("pem-side-head", txt(item.side || item.trade_type));
    setText("pem-price-head", `₹${money(item.last_price || 0)}`);

    setVal("pem-type", txt(item.instrument_type || item.type, ""));
    setVal("pem-qty", txt(item.open_qty ?? item.net_qty ?? item.qty ?? item.quantity, ""));
    setVal("pem-last", money(item.last_price || 0));
    setVal("pem-equity-ref", txt(item.equity_ref, ""));
    setVal("pem-reason", "MANUAL_EXIT");

    setStatus("Ready.", "text-muted");

    const submitBtn = byId("pem-submit-btn");
    if (submitBtn) submitBtn.disabled = false;

    const modalEl = byId(modalId());
    if (!modalEl) return;

    const modal = bootstrap.Modal.getOrCreateInstance(modalEl, { focus: true });
    modal.show();
  }

  async function submit(source = null) {
    if (!_currentItem) return;

    const src = String(source || _currentSource || "position").trim().toLowerCase();
    _currentSource = src;

    const C = cfg(src);
    const submitBtn = byId("pem-submit-btn");
    if (submitBtn) submitBtn.disabled = true;

    try {
      const payloadBase = buildPayloadBase(_currentItem);

      setStatus("Validating exit...", "text-muted");

      const planResp = await fetch(C.exitPlanUrl || "/dashboard/trading/exit_plan", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payloadBase)
      });

      const planData = await planResp.json().catch(() => ({}));

      if (!planResp.ok || !planData.ok) {
        setStatus(planData.error || planData.reason || "Exit validation failed.", "text-danger");
        if (submitBtn) submitBtn.disabled = false;
        return;
      }

      setStatus("Queueing exit...", "text-muted");

      const exitResp = await fetch(C.exitIntentUrl || "/dashboard/trading/exit_trade", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payloadBase)
      });

      const exitData = await exitResp.json().catch(() => ({}));

      if (!exitResp.ok || exitData.status !== "success") {
        setStatus(exitData.error || exitData.reason || "Exit failed.", "text-danger");
        if (submitBtn) submitBtn.disabled = false;
        return;
      }

      setStatus("Exit queued successfully.", "text-success");
      refreshTargets();

      setTimeout(() => {
        const modalEl = byId(modalId());
        const modal = modalEl ? bootstrap.Modal.getInstance(modalEl) : null;
        if (modal) modal.hide();
      }, 700);

    } catch (err) {
      console.error("trade exit failed:", err);
      setStatus("Exit request failed.", "text-danger");
      if (submitBtn) submitBtn.disabled = false;
    }
  }

  function bindGenericExitClicks() {
    $(document).on("click", ".js-trade-exit", function (e) {
      e.preventDefault();
      e.stopPropagation();

      const btn = this;
      const source = String($(btn).attr("data-source") || "position").trim().toLowerCase();
      const enc = $(btn).attr("data-record") || "";

      let item = null;
      try {
        item = JSON.parse(enc);
      } catch (_) {
        try {
          item = JSON.parse(decodeURIComponent(enc));
        } catch (err) {
          console.error("Invalid shared trade-exit payload", err);
          return;
        }
      }

      if (!canExit(item)) return;
      openModal(item, source, btn);
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
      _currentItem = null;

      const submitBtn = byId("pem-submit-btn");
      if (submitBtn) submitBtn.disabled = false;

      if (_lastTrigger && typeof _lastTrigger.focus === "function") {
        _lastTrigger.focus();
      }
      _lastTrigger = null;
    });
  }

  function bindSubmit() {
    $(document).on("click", "#pem-submit-btn", async function (e) {
      e.preventDefault();
      await submit();
    });
  }

  $(document).ready(() => {
    setupModalLifecycle();
    bindGenericExitClicks();
    bindSubmit();
  });

  window.TradeExitModal = {
    openModal,
    submit,
    setStatus,
    canExit
  };
})();