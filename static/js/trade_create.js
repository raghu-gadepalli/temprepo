// static/js/trade_create.js
// Shared create-trade modal logic for watchlist / signals / position pages.
// Expected config may be provided as one of:
// - window.TRADE_CREATE_CONFIG
// - window.WATCHLIST_CONFIG
// - window.SIGNALS_CONFIG
// - window.POSITIONS_CONFIG
//
// Generic button contract supported:
//   class="js-trade-create"
//   data-source="watchlist|signals|signal|position"
//   data-record="<urlencoded JSON payload>"

(function () {
  "use strict";

  let _currentSource = null;

  function isSignalLikeSource(src) {
    const s = String(src || "").trim().toLowerCase();
    return s === "signal" || s === "signals";
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

  function cfg(source) {
    const src = String(source || _currentSource || "").trim().toLowerCase();

    if (window.TRADE_CREATE_CONFIG) return window.TRADE_CREATE_CONFIG;
    if (src === "watchlist" && window.WATCHLIST_CONFIG) return window.WATCHLIST_CONFIG;
    if ((src === "signal" || src === "signals") && window.SIGNALS_CONFIG) return window.SIGNALS_CONFIG;
    if (src === "position" && window.POSITIONS_CONFIG) return window.POSITIONS_CONFIG;

    return (
      window.WATCHLIST_CONFIG ||
      window.SIGNALS_CONFIG ||
      window.POSITIONS_CONFIG ||
      {}
    );
  }

  function showUsers() {
    return true;
  }

  function modalId() {
    return cfg().modalId || "tradeCreateModal";
  }

  function modalLabelId() {
    return cfg().modalLabelId || "tradeCreateModalLabel";
  }

  function wtmMoney(v) {
    const n = Number(v);
    return Number.isFinite(n) ? n.toFixed(2) : "";
  }

  function sortByStrike(rows) {
    return (Array.isArray(rows) ? rows : [])
      .slice()
      .sort((a, b) => Number(a?.strike || 0) - Number(b?.strike || 0));
  }

  function wtmEsc(s) {
    return String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/'/g, "&#39;")
      .replace(/"/g, "&quot;");
  }

  function showAlert(icon, title, text) {
    if (typeof Swal !== "undefined") {
      return Swal.fire({ icon, title, text });
    }
    alert(text || title || "Something went wrong.");
    return Promise.resolve();
  }

  const REASON_TEXT = {
    missing_userid: "User is missing.",
    missing_signal_id: "Signal is missing.",
    user_not_autogen_eligible: "User is not eligible for automatic trading.",
    autotrade_not_enabled: "Autotrade is not enabled for this user.",
    signal_not_open: "Signal is no longer open.",
    signal_terminal: "Signal is already terminal.",
    signal_already_deployed: "This signal has already been deployed for this user.",
    active_trade_exists_for_signal: "An active trade already exists for this signal.",
    active_trade_exists_for_symbol_family: "An active trade already exists for this stock family.",
    signal_action_invalidating_opposite_wait_for_confirmation: "Signal is resolving an opposite setup and requires explicit confirmation.",
    stage_not_deployable: "Signal is not currently in a deployable stage.",
    initiated_setup_deployment_window_expired: "The original signal deployment window has expired.",
    EXHAUSTION_ENTRY_WAIT_NEXT_CANDLE: "Exhaustion entry is waiting for the next completed candle.",
    EXHAUSTION_ENTRY_WAIT_FAVORABLE_MOVE: "Exhaustion entry is waiting for favourable follow-through.",
    EXHAUSTION_ENTRY_WINDOW_EXPIRED: "Exhaustion entry window has expired.",
    EXHAUSTION_ENTRY_SKIPPED_CHASED_MOVE: "The exhaustion move is already too extended to chase.",
    SIGNAL_ENTRY_WAIT_NOT_IN_LOSS: "Current price is adverse to the signal. Automatic entry will wait; manual entry requires explicit confirmation.",
    SIGNAL_ENTRY_WAIT_PRICE_UNAVAILABLE: "Current or signal creation price is unavailable. Automatic entry will wait; manual entry requires explicit confirmation."
  };

  function humanizeReason(value) {
    const raw = String(value || "").trim();
    if (!raw) return "";
    if (REASON_TEXT[raw]) return REASON_TEXT[raw];
    if (REASON_TEXT[raw.toUpperCase()]) return REASON_TEXT[raw.toUpperCase()];
    if (raw.includes(" ")) return raw;
    return raw
      .replace(/_/g, " ")
      .replace(/\b\w/g, ch => ch.toUpperCase());
  }

  function validationReasonText(validation) {
    const reasons = Array.isArray(validation?.reasons) ? validation.reasons : [];
    const warnings = Array.isArray(validation?.warnings) ? validation.warnings : [];
    return [...reasons, ...warnings].map(humanizeReason).filter(Boolean);
  }

  function extractErrorMessage(payload) {
    const data = payload || {};
    const results = Array.isArray(data.results) ? data.results : [];
    const failed =
      results.find(r => r && r.ok === false && r.result) ||
      results.find(r => r && r.result && r.result.ok === false) ||
      null;
    const inner = failed?.result || {};
    const error = String(inner.error || data.error || "").trim().toUpperCase();
    const details = inner.details || data.details || {};

    if (error === "SIGNAL_ALREADY_DEPLOYED" || error === "TRADE_ALREADY_EXISTS_FOR_SIGNAL") {
      const existing = Array.isArray(details?.existing) ? details.existing : [];
      if (existing.length) {
        return "Trade already exists for this signal: " + existing.map(x =>
          `${x.instrument_type}:${x.symbol} (${x.entry_status}/${x.exit_status})`
        ).join(", ");
      }
      return "This signal has already been deployed for the selected user.";
    }
    if (error === "MANUAL_OVERRIDE_REQUIRED") {
      const text = validationReasonText(details);
      return text.length ? text.join(" ") : "Explicit confirmation is required for this trade.";
    }
    if (error === "TRADE_VALIDATION_BLOCKED" || error === "NO_USERS_ELIGIBLE_FOR_SIGNAL_TRADE") {
      const text = validationReasonText(details);
      const validations = Array.isArray(data.user_validation) ? data.user_validation : [];
      const perUser = validations.flatMap(v =>
        (Array.isArray(v.reasons) ? v.reasons : []).map(reason => `${v.userid}: ${humanizeReason(reason)}`)
      );
      return [...text, ...perUser].filter(Boolean).join(" ") || "Trade creation is blocked by current validation.";
    }
    if (error === "EQ_SELL_REQUIRES_INTRADAY_PRODUCT") return "Equity SELL is allowed only with MIS/intraday product.";
    if (error === "OPTION_SELL_NOT_ALLOWED") return "Option selling is not supported. Use BUY CE or BUY PE.";
    if (error === "USER_INSTRUMENT_NOT_ENABLED") return "The selected instrument is not enabled for this user.";
    if (error === "SELECTED_TRADE_SYMBOL_NOT_AVAILABLE") return "The selected contract is no longer available. Reopen the form and select a current contract.";
    if (error === "SELECTED_TRADE_SYMBOL_PRICE_UNAVAILABLE") return "A current price is unavailable for the selected contract.";
    if (error === "SELECTED_LEG_REQUIRES_SINGLE_INSTRUMENT") return "Select one instrument before choosing a specific contract.";
    if (error === "INSTRUMENT_NOT_APPLICABLE_TO_SIGNAL_SIDE") return "The selected instrument is not applicable to this signal direction.";
    if (error === "QUANTITY_NOT_MULTIPLE_OF_LOT_SIZE") return "Quantity must be a multiple of the instrument lot size.";
    if (error === "SIGNAL_TERMINAL_STATUS") return "Signal is already terminal and cannot create a trade.";

    return humanizeReason(inner.reason || inner.error || data.reason || data.error || "Create trade failed.");
  }

  function initState() {
    window._wtmContext = null;
    window._wtmBaseLtp = 0;
    window._wtmRiskRefPrice = 0;
    window._wtmTradeableUsers = [];
  }

  function selectedInst() {
    const v = $('input[name="wtm-inst"]:checked').val() || "EQ";
    return String(v).toUpperCase();
  }

  function selectedSide() {
    const side = $('input[name="wtm-side"]:checked').val() || "BUY";
    return String(side).toUpperCase();
  }

  function isSaveForLater() {
    return !!$("#wtm-save-for-later").prop("checked");
  }

  function updatePrimaryButton() {
    $("#wtm-submit-btn").text(isSaveForLater() ? "Create Only" : "Create & Queue");
  }

  function getContext() {
    return window._wtmContext || {};
  }

  function getTradeWarningInfo() {
    const form = getForm();
    const selectedUsers = new Set(getSelectedUserIds());
    const perUser = Array.isArray(form.user_validation) ? form.user_validation : [];
    const warnings = [];
    let requiresOverride = false;

    if (perUser.length) {
      perUser.forEach(v => {
        const userid = String(v?.userid || "").trim().toUpperCase();
        if (!selectedUsers.has(userid)) return;
        const decision = String(v?.decision || "").trim().toUpperCase();
        if (decision === "WAIT" || v?.requires_override) requiresOverride = true;
        validationReasonText(v).forEach(text => warnings.push(`${userid}: ${text}`));
      });
    } else {
      const validation = form.trade_validation || {};
      const decision = String(validation.decision || "").trim().toUpperCase();
      if (decision === "WAIT" || form.requires_override) requiresOverride = true;
      validationReasonText(validation).forEach(text => warnings.push(text));
    }

    if (isSideBlocked(selectedInst(), selectedSide())) {
      const inst = selectedInst();
      const side = selectedSide();
      const product = String($("#wtm-product").val() || form.product || "MIS").trim().toUpperCase();
      if (inst === "EQ" && side === "SELL") {
        warnings.push(`Equity SELL requires MIS/intraday product; ${product || "current product"} is not permitted.`);
      } else if ((inst === "CE" || inst === "PE") && side === "SELL") {
        warnings.push("Option selling is not supported; use BUY CE or BUY PE.");
      }
    }

    return {
      hasWarning: warnings.length > 0 || requiresOverride,
      requiresOverride,
      warnings: warnings.length ? warnings : (requiresOverride ? ["Explicit manual confirmation is required."] : []),
    };
  }

  function refreshTradeWarnings() {
    const info = getTradeWarningInfo();
    const $card = $("#wtm-warning-card");
    const $list = $("#wtm-warning-list");
    const $confirm = $("#wtm-warning-confirm");

    if (!$card.length) return info;

    $confirm.prop("checked", false);

    if (!info.hasWarning) {
      $card.addClass("d-none");
      $list.html("");
      return info;
    }

    $list.html(
      info.warnings.map(w => `<li>${wtmEsc(w)}</li>`).join("")
    );

    $card.removeClass("d-none");
    return info;
  }

  function warningConfirmedIfNeeded() {
    const info = getTradeWarningInfo();
    if (!info.hasWarning) return true;
    return !!$("#wtm-warning-confirm").prop("checked");
  }

  function getForm() {
    return getContext().form || {};
  }

  function getFields() {
    return getForm().fields || {};
  }

  function getInstruments() {
    return getForm().instruments || {};
  }

  function getInstrumentBlock(inst) {
    return getInstruments()[String(inst || "").toUpperCase()] || null;
  }

  function getCurrentBlock() {
    return getInstrumentBlock(selectedInst());
  }

  function getCurrentOption() {
    const block = getCurrentBlock();
    const sym = String($("#wtm-trading-symbol").val() || "").trim();
    const options = Array.isArray(block?.options) ? block.options : [];
    if (!options.length) return null;
    const exact = options.find(x => String(x.symbol || "").trim() === sym);
    return exact || options[0];
  }

  function setStatus(msg, cls) {
    const $s = $("#wtm-status");
    const $f = $("#wtm-status-footer");
    $s.removeClass("text-danger text-success text-muted text-warning");
    $f.removeClass("text-danger text-success text-muted text-warning");
    if (cls) {
      $s.addClass(cls);
      $f.addClass(cls);
    }
    $s.text(msg || "");
    $f.text(msg || "");
  }

  function applicableSidesFor(inst) {
    const block = getInstrumentBlock(inst);
    const sides = Array.isArray(block?.applicable_sides) ? block.applicable_sides : [];
    return sides.map(x => String(x || "").trim().toUpperCase()).filter(Boolean);
  }

  function allowedOrderSidesFor(inst) {
    const block = getInstrumentBlock(inst);
    const sides = Array.isArray(block?.allowed_sides) ? block.allowed_sides : [];
    return sides.map(x => String(x || "").trim().toUpperCase()).filter(Boolean);
  }

  function entrySideFor(inst, selectedDirection = null) {
    const block = getInstrumentBlock(inst);
    const entrySide = String(block?.entry_side || "").trim().toUpperCase();
    if (entrySide) return entrySide;

    // Fallback for older payloads. Options are always long-only in autotrades.
    const inst0 = String(inst || "").trim().toUpperCase();
    if (inst0 === "CE" || inst0 === "PE") return "BUY";
    return String(selectedDirection || selectedSide() || "BUY").trim().toUpperCase();
  }

  function isSideBlocked(inst, side) {
    const inst0 = String(inst || "").toUpperCase();
    const side0 = String(side || "").toUpperCase();
    const product = String($("#wtm-product").val() || getForm().product || "MIS").trim().toUpperCase();
    if (inst0 === "EQ" && side0 === "SELL" && !["MIS", "INTRADAY"].includes(product)) return true;
    if ((inst0 === "CE" || inst0 === "PE") && side0 === "SELL") return true;
    const allowed = allowedOrderSidesFor(inst0);
    return allowed.length > 0 && !allowed.includes(side0);
  }

  function isInstrumentApplicable(inst, direction) {
    const applicable = applicableSidesFor(inst);
    return !applicable.length || applicable.includes(String(direction || "BUY").toUpperCase());
  }

  function enforceSideRules() {
    const fields = getFields();
    const sideEditable = !!fields.side_editable;

    const $buy = $("#wtm-side-buy");
    const $sell = $("#wtm-side-sell");

    if (!sideEditable) {
      $('input[name="wtm-side"]').prop("disabled", true);
      return;
    }

    $sell.prop("disabled", false);
    $buy.prop("disabled", false);
  }

  function populateInstrumentOptions(form) {
    const available = Array.isArray(form?.available_instruments)
      ? form.available_instruments.map(x => String(x || "").trim().toUpperCase()).filter(Boolean)
      : [];

    const fields = form?.fields || {};
    const instEditable = !!fields.instrument_editable;
    const direction = selectedSide();

    $('input[name="wtm-inst"]').each(function () {
      const val = String($(this).val() || "").trim().toUpperCase();
      const enabledByAvailability = !available.length || available.includes(val);
      const enabledByDirection = isInstrumentApplicable(val, direction);
      const enabled = instEditable ? (enabledByAvailability && enabledByDirection) : false;
      $(this).prop("disabled", !enabled);

      const block = getInstrumentBlock(val);
      const reason = block?.disabled_reason || "Not applicable for selected direction";
      const $label = $(`label[for="${this.id}"]`);
      if ($label.length) {
        $label.attr("title", enabled ? "" : reason);
      }
    });

    const current = String($('input[name="wtm-inst"]:checked').val() || "").trim().toUpperCase();
    const preferred =
      String(form?.selected_instrument || "").trim().toUpperCase() ||
      (available.length ? available[0] : "EQ");

    const $current = current ? $(`input[name="wtm-inst"][value="${current}"]`) : $();
    const $preferred = $(`input[name="wtm-inst"][value="${preferred}"]`);

    // Preserve the user's selected instrument when it is still valid for the
    // current direction. Earlier logic always reselected backend preferred
    // instrument on every refresh, which made watchlist rows appear locked to
    // FUT and prevented switching to EQ/CE/PE.
    if ($current.length && !$current.prop("disabled")) {
      $current.prop("checked", true);
    } else if ($preferred.length && !$preferred.prop("disabled")) {
      $preferred.prop("checked", true);
    } else if ($preferred.length && !instEditable) {
      $preferred.prop("checked", true);
    } else {
      const $firstEnabled = $('input[name="wtm-inst"]:not(:disabled)').first();
      if ($firstEnabled.length) {
        $firstEnabled.prop("checked", true);
      }
    }

    if (!instEditable) {
      const selected = String(form?.selected_instrument || "").trim().toUpperCase();
      if (selected) {
        $(`input[name="wtm-inst"][value="${selected}"]`).prop("checked", true);
      }
    }
  }

  function populateSymbolDropdown() {
    const inst = selectedInst();
    const $sel = $("#wtm-trading-symbol");
    const block = getInstrumentBlock(inst);
    const rawOptions = Array.isArray(block?.options) ? block.options : [];
    const currentVal = String($sel.val() || "").trim();
    const fields = getFields();

    const options = (inst === "CE" || inst === "PE")
      ? sortByStrike(rawOptions)
      : rawOptions.slice();

    $sel.empty();

    if (!options.length) {
      $sel.append(new Option("No symbol available", ""));
      $sel.prop("disabled", true);
      return;
    }

    options.forEach((optRow) => {
      const text = String(optRow.display || optRow.symbol || "").trim();
      const value = String(optRow.symbol || "").trim();
      $sel.append(new Option(text, value, false, false));
    });

    // Preserve user's dropdown selection when the symbol is still available.
    // Earlier logic preferred the backend default/selected option on every
    // refresh, so changing CE/PE/FUT contracts in the dropdown immediately
    // snapped back to the default instrument symbol.
    const selectedOpt =
      options.find(x => String(x.symbol || "").trim() === currentVal) ||
      options.find(x => x.selected) ||
      options[0];

    $sel.val(String(selectedOpt?.symbol || "").trim());

    let forceReadonlySymbol = !!fields.symbol_locked || !!cfg().lockTradingSymbol;

    if (inst === "CE" || inst === "PE") {
      if (fields.option_symbol_editable === false) {
        forceReadonlySymbol = true;
      }
    } else {
      if (fields.symbol_editable === false) {
        forceReadonlySymbol = true;
      }
    }

    $sel.prop("disabled", forceReadonlySymbol);
  }

  function refreshHeader() {
    const ctx = getContext();
    const item = ctx.item || {};
    const form = getForm();
    const opt = getCurrentOption();
    const side = selectedSide();

    const symbolText =
      String($("#wtm-trading-symbol").val() || opt?.symbol || item.symbol || form.symbol || "—").trim();

    const priceVal =
      Number(
        opt?.entry_price ??
        form?.meta?.entry_price ??
        form?.meta?.ltp ??
        item?.ltp ??
        item?.last_price ??
        item?.avg_price ??
        window._wtmBaseLtp ??
        window._wtmRiskRefPrice ??
        0
      ) || 0;

    $("#wtm-symbol-head").text(symbolText || "—");
    $("#wtm-side-head").text(side || "BUY");
    $("#wtm-price-head").text(wtmMoney(priceVal));

    const titleText = symbolText
      ? `${symbolText} · ${side || "BUY"}`
      : `Create Trade · ${side || "BUY"}`;

    $("#" + modalLabelId()).text(titleText);
  }

  function refreshModeFields() {
    const form = getForm();
    const C = cfg();

    const currentProduct = String($("#wtm-product").val() || "").trim().toUpperCase();
    const productValue = currentProduct || String(C.defaultProduct || form.product || "MIS").toUpperCase();
    const lockProduct = !!C.lockProduct || !!form?.fields?.product_locked || (form?.fields?.product_editable === false);

    $("#wtm-product")
      .val(productValue)
      .prop("disabled", lockProduct)
      .toggleClass("bg-light text-muted", lockProduct);

    if (lockProduct) $("#wtm-product").attr("tabindex", "-1");
    else $("#wtm-product").removeAttr("tabindex");
  }

  function refreshQtyAndRequired() {
    const inst = selectedInst();
    const block = getCurrentBlock();
    const opt = getCurrentOption();

    const lotsize = Math.max(1, parseInt(opt?.lotsize || block?.lotsize || "1", 10) || 1);
    const lots = Math.max(1, parseInt($("#wtm-lots").val() || "1", 10) || 1);

    let qty = 1;
    let baseQty = 1;
    let note = "";

    if (inst === "EQ") {
      baseQty = Math.max(1, parseInt(opt?.base_qty || opt?.qty || block?.base_qty || block?.qty || "1", 10) || 1);
      qty = baseQty * lots;
      note = `EQ selected · 1 lot = ${baseQty} qty from default capital · ${lots} lot(s) = qty ${qty}`;
    } else {
      qty = lotsize * lots;
      note = `${inst} selected · 1 lot = ${lotsize} qty · ${lots} lot(s) = qty ${qty}`;
    }

    const entryPrice = Number(opt?.entry_price || block?.entry_price || 0) || 0;
    const required = entryPrice * qty;

    $("#wtm-lots").val(String(lots));
    $("#wtm-qty").val(String(qty));
    $("#wtm-required").val(wtmMoney(required));
    $("#wtm-inst-note").text(note);
  }

  function refreshReadonlyFields() {
    const form = getForm();
    const block = getCurrentBlock();
    const opt = getCurrentOption();

    $("#wtm-execution-mode").val(String(form.execution_mode || "VIRTUAL"));
    $("#wtm-entry-price").val(wtmMoney(opt?.entry_price || block?.entry_price || 0));
  }

  function refreshBasicAdvanced() {
    const mode = $('input[name="wtm-mode"]:checked').val() || "BASIC";
    const isBasic = String(mode).toUpperCase() === "BASIC";

    if (isBasic) {
      $("#wtm-basic-summary").removeClass("d-none");
      $("#wtm-advanced-fields").addClass("d-none");
    } else {
      $("#wtm-basic-summary").addClass("d-none");
      $("#wtm-advanced-fields").removeClass("d-none");
    }
  }

  function refreshModal() {
    populateInstrumentOptions(getForm());
    populateSymbolDropdown();
    enforceSideRules();
    refreshHeader();
    refreshModeFields();
    refreshReadonlyFields();
    refreshQtyAndRequired();
    refreshBasicAdvanced();
    refreshTradeWarnings();
    updatePrimaryButton();
  }

  function resetModalState() {
    $("#wtm-lots").val("1");
    $("#wtm-save-for-later").prop("checked", false);
    $("#wtm-mode-basic").prop("checked", true);
    $("#wtm-product").val(String(cfg().defaultProduct || "MIS"));
    $("#wtm-status").text("Ready.");
    $("#wtm-status-footer").text("");
    $("#wtm-submit-btn").prop("disabled", false);
    $("#wtm-warning-card").addClass("d-none");
    $("#wtm-warning-list").html("");
    $("#wtm-warning-confirm").prop("checked", false);
  }

  function renderTradeableUsers(users) {
    const $list = $("#wtm-users-list");
    const $status = $("#wtm-users-status");
    const $count = $("#wtm-user-count");
    const $selectAll = $("#wtm-users-select-all");
    const $clearAll = $("#wtm-users-clear-all");

    $list.empty();

    const rows = Array.isArray(users) ? users : [];
    window._wtmTradeableUsers = rows;

    if (!rows.length) {
      $status.text("No users available.");
      $count.text("0");
      $list.html('<div class="small text-muted">No users available.</div>');
      $selectAll.prop("disabled", true);
      $clearAll.prop("disabled", true);
      return;
    }

    const anyEnabled = rows.some(u => !u.disabled && String(u?.validation?.decision || "").toUpperCase() !== "BLOCK" && u?.validation?.ok !== false);

    $status.text(anyEnabled ? "Select one or more accounts." : "Current user is preselected.");
    $selectAll.prop("disabled", !anyEnabled);
    $clearAll.prop("disabled", !anyEnabled);

    rows.forEach((u, idx) => {
      const userid = String(u.userid || "").trim().toUpperCase();
      const validation = u.validation || {};
      const decision = String(validation.decision || "").trim().toUpperCase();
      const blocked = decision === "BLOCK" || validation.ok === false;
      const selected = !!u.selected && !blocked;
      const disabled = !!u.disabled || blocked;
      const reason = validationReasonText(validation)[0] || humanizeReason(validation.error || "");
      const badge = decision === "WAIT"
        ? '<span class="badge bg-warning text-dark ms-1">Override</span>'
        : decision === "ALLOW"
          ? '<span class="badge bg-success ms-1">Allowed</span>'
          : blocked
            ? '<span class="badge bg-danger ms-1">Blocked</span>'
            : "";

      $list.append(`
        <div class="form-check mb-1">
          <input class="form-check-input wtm-user-check"
                 type="checkbox"
                 value="${wtmEsc(userid)}"
                 id="wtm-user-${idx}"
                 ${selected ? "checked" : ""}
                 ${disabled ? "disabled" : ""}>
          <label class="form-check-label" for="wtm-user-${idx}" title="${wtmEsc(reason)}">
            ${wtmEsc(userid)}
            ${u.disabled ? '<span class="text-muted small">— current</span>' : ""}
            ${badge}
            ${reason ? `<span class="text-muted small ms-1">— ${wtmEsc(reason)}</span>` : ""}
          </label>
        </div>
      `);
    });

    updateSelectedUserCount();
  }

  function applyUserValidation(validations) {
    const rows = Array.isArray(window._wtmTradeableUsers) ? window._wtmTradeableUsers : [];
    const map = new Map(
      (Array.isArray(validations) ? validations : []).map(v => [String(v?.userid || "").trim().toUpperCase(), v])
    );
    const merged = rows.map(u => ({
      ...u,
      validation: map.get(String(u?.userid || "").trim().toUpperCase()) || null,
    }));

    if (!merged.some(u => !!u.selected && String(u?.validation?.decision || "").toUpperCase() !== "BLOCK" && u?.validation?.ok !== false)) {
      const firstEligible = merged.find(u => String(u?.validation?.decision || "").toUpperCase() !== "BLOCK" && u?.validation?.ok !== false);
      if (firstEligible) firstEligible.selected = true;
    }
    renderTradeableUsers(merged);
  }

  function getSelectedUserIds() {
    return $(".wtm-user-check:checked")
      .map(function () {
        return String($(this).val() || "").trim().toUpperCase();
      })
      .get()
      .filter(Boolean);
  }

  function updateSelectedUserCount() {
    const count = getSelectedUserIds().length;
    $("#wtm-user-count").text(String(count));
    return count;
  }

  async function loadTradeableUsers(source) {
    const C = cfg(source);
    if (!C.usersUrl) return [];

    $("#wtm-users-status").text("Loading users…");
    $("#wtm-users-list").html('<div class="small text-muted">Loading users…</div>');
    $("#wtm-user-count").text("0");

    try {
      const resp = await fetch(C.usersUrl, {
        method: "GET",
        credentials: "same-origin"
      });

      const data = await resp.json().catch(() => ({}));
      const ok = data.ok || data.status === "success";
      if (!resp.ok || !ok) {
        renderTradeableUsers([]);
        $("#wtm-users-status").text(data.reason || data.error || "Unable to load users.");
        return [];
      }

      const users = Array.isArray(data.data) ? data.data : [];
      renderTradeableUsers(users);
      return users;
    } catch (e) {
      console.error("wtmLoadTradeableUsers failed:", e);
      renderTradeableUsers([]);
      $("#wtm-users-status").text("Unable to load users.");
      return [];
    }
  }

  function openModal(ctx) {
    window._wtmContext = ctx || {};
    const form = ctx?.form || {};

    window._wtmBaseLtp = Number(ctx?.item?.ltp || ctx?.item?.last_price || 0) || 0;
    window._wtmRiskRefPrice = Number(
      form?.meta?.risk_ref_price ||
      ctx?.item?.ltp ||
      ctx?.item?.last_price ||
      ctx?.item?.avg_price ||
      0
    ) || 0;

    resetModalState();

    const initialSide = String(form?.side || "BUY").toUpperCase();
    $(`input[name="wtm-side"][value="${initialSide}"]`).prop("checked", true);

    const sideEditable = !!(form?.fields?.side_editable);
    $('input[name="wtm-side"]').prop("disabled", !sideEditable);

    if (Array.isArray(form.user_validation)) {
      applyUserValidation(form.user_validation);
    }
    populateInstrumentOptions(form);
    refreshModal();

    const modalEl = document.getElementById(modalId());
    const modal = bootstrap.Modal.getOrCreateInstance(modalEl, { focus: true });
    modal.show();
  }

  function buildPlanBody(info, source, userids, primaryUserid) {
    const body = { source };
    if (primaryUserid) body.primary_userid = primaryUserid;

    if (isSignalLikeSource(source)) {
      body.signal_id = String(info?.signal_id || info?.details?.signal?.signal_id || "").trim();
      body.instrument_choice = "MULTI";
    } else if (source === "position") {
      body.symbol = String(info?.symbol || "").trim().toUpperCase();
      body.equity_ref = String(info?.equity_ref || info?.details?.equity_ref || info?.symbol || "").trim().toUpperCase();
      body.instrument_type = String(info?.instrument_type || info?.type || "EQ").trim().toUpperCase();
      body.side = String(info?.entry_side || info?.side || info?.trade_type || "BUY").trim().toUpperCase();
    } else {
      body.symbol = String(info?.symbol || "").trim().toUpperCase();
      body.side = String(info?.side || info?.trade_type || "BUY").trim().toUpperCase();
    }

    if (Array.isArray(userids) && userids.length) {
      body.userids = userids;
    }

    return body;
  }

  async function openFromRow(info, source = "watchlist") {
    _currentSource = String(source || "watchlist").trim().toLowerCase();
    const C = cfg(_currentSource);
    const source0 = _currentSource;

    const users = await loadTradeableUsers(source0);
    const userids = Array.isArray(users)
      ? users.map(u => String(u.userid || "").trim().toUpperCase()).filter(Boolean)
      : [];
    const selectedUserids = Array.isArray(users)
      ? users.filter(u => !!u.selected).map(u => String(u.userid || "").trim().toUpperCase()).filter(Boolean)
      : [];

    if (!userids.length) {
      await showAlert("warning", "Create Trade", "No users available.");
      return;
    }

    const body = buildPlanBody(info, source0, userids, selectedUserids[0] || userids[0]);

    if (isSignalLikeSource(source0) && !body.signal_id) {
      console.error("Missing signal_id in row payload", info);
      await showAlert("error", "Create Trade", "Missing signal id.");
      return;
    }

    if ((source0 === "watchlist" || source0 === "position") && !body.symbol) {
      console.error("Missing symbol in row payload", info);
      await showAlert("error", "Create Trade", "Missing symbol.");
      return;
    }

    const resp = await fetch(C.planUrl, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });

    const data = await resp.json().catch(() => ({}));

    if (!resp.ok || !data.ok || !data.trade_form) {
      console.error("trading plan failed:", data);
      await showAlert("error", "Create Trade", extractErrorMessage(data) || "Unable to build trade form.");
      return;
    }

    openModal({
      item: info,
      form: data.trade_form,
      source: source0
    });
  }

  async function submit(source = null) {
    const ctx = getContext();
    const source0 = String(source || ctx.source || _currentSource || "watchlist").trim().toLowerCase();
    _currentSource = source0;

    const C = cfg(source0);
    const form = ctx.form || {};
    const item = ctx.item || {};

    const symbol = String(form.symbol || item.symbol || "").trim().toUpperCase();
    const signalId = String(item.signal_id || item?.details?.signal?.signal_id || "").trim();
    const equityRef = String(item.equity_ref || form.equity_ref || form.symbol || symbol).trim().toUpperCase();

    const inst = selectedInst();
    const opt = getCurrentOption();
    const tradeSymbol = String($("#wtm-trading-symbol").val() || opt?.symbol || "").trim().toUpperCase();
    const qty = parseInt($("#wtm-qty").val() || "0", 10) || 0;
    const lots = parseInt($("#wtm-lots").val() || "1", 10) || 1;
    const directionSide = selectedSide();
    const side = entrySideFor(inst, directionSide);
    const saveForLater = isSaveForLater();
    const userids = getSelectedUserIds();

    if (isSideBlocked(inst, side)) {
      const msg = (inst === "CE" || inst === "PE")
        ? "Option selling is not allowed. Options are created as BUY trades only."
        : "Selected side is not allowed for this instrument.";
      setStatus(msg, "text-danger");
      return;
    }

    if (isSignalLikeSource(source0)) {
      if (!signalId) {
        setStatus("Missing signal id.", "text-danger");
        return;
      }
      if (!tradeSymbol || qty <= 0) {
        setStatus("Missing trade symbol or quantity.", "text-danger");
        return;
      }
    } else {
      if (!symbol || !tradeSymbol || qty <= 0) {
        setStatus("Missing symbol or quantity.", "text-danger");
        return;
      }
    }

    if (!userids.length) {
      setStatus("Select at least one user.", "text-danger");
      return;
    }

    const warningInfo = getTradeWarningInfo();
    if (!warningConfirmedIfNeeded()) {
      setStatus("Please confirm the trade warning before creating the trade.", "text-warning");
      return;
    }

    const $btn = $("#wtm-submit-btn");
    $btn.prop("disabled", true);
    setStatus(`Creating trade for ${userids.length} user(s)...`, "text-muted");

    try {
      const body = {
        source: source0,
        symbol,
        side,
        trade_type: side,
        instrument_type: inst,
        instrument_choice: inst,
        trade_symbol: tradeSymbol,
        product: ($("#wtm-product").val() || C.defaultProduct || form.product || "MIS").toUpperCase(),
        execution_mode: ($("#wtm-execution-mode").val() || form.execution_mode || "VIRTUAL").toUpperCase(),
        lotsize: parseInt(opt?.lotsize || 1, 10) || 1,
        lots,
        quantity: qty,
        qty,
        entry_price: $("#wtm-entry-price").val() || opt?.entry_price || "",
        risk_ref_price: window._wtmRiskRefPrice || item?.ltp || item?.last_price || item?.avg_price || "",
        message: saveForLater ? "SAVE_FOR_LATER" : "MANUAL_TRADE",
        override_validation: isSignalLikeSource(source0) && !!warningInfo.requiresOverride,
        userids
      };

      if (isSignalLikeSource(source0)) {
        body.signal_id = signalId;
      }

      if (source0 === "position") {
        body.equity_ref = equityRef;
      }

      const createResp = await fetch(C.createUrl, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
      });

      const createData = await createResp.json().catch(() => ({}));
      const results = Array.isArray(createData.results) ? createData.results : [];

      if (!createResp.ok || !createData.ok) {
        setStatus(extractErrorMessage(createData), "text-danger");
        $btn.prop("disabled", false);
        return;
      }
      
      const tradeIds = Array.isArray(createData.trade_ids)
        ? createData.trade_ids.map(x => parseInt(x, 10)).filter(x => Number.isFinite(x) && x > 0)
        : [];

      const okUsers = results.filter(r => !!r.ok).length || (tradeIds.length ? 1 : 0);
      const failUsers = Math.max(results.length - okUsers, 0);

      if (!saveForLater && tradeIds.length > 0) {
        setStatus(`Created for ${okUsers} user(s). Queuing ${tradeIds.length} trade(s)...`, "text-muted");

        const submitResp = await fetch(C.submitUrl, {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ids: tradeIds })
        });

        const submitData = await submitResp.json().catch(() => ({}));

        if (!submitResp.ok || !submitData.ok) {
          if (failUsers > 0) {
            setStatus(`Created for ${okUsers} user(s), ${failUsers} failed. Queueing failed.`, "text-warning");
          } else {
            setStatus("Trade created, but queueing failed.", "text-warning");
          }
        } else {
          if (failUsers > 0) {
            setStatus(`Queued successfully for ${okUsers} user(s); ${failUsers} failed.`, "text-warning");
          } else {
            setStatus(`Trade created and queued for ${okUsers} user(s).`, "text-success");
          }
        }
      } else {
        if (failUsers > 0) {
          setStatus(`Created for ${okUsers} user(s); ${failUsers} failed. Review in Orders.`, "text-warning");
        } else {
          setStatus(`Trade created successfully for ${okUsers} user(s). Review in Orders.`, "text-success");
        }
      }

      if (Array.isArray(C.refreshTargets)) {
        C.refreshTargets.forEach(refreshTarget);
      }

      setTimeout(() => {
        const modalEl = document.getElementById(modalId());
        const modal = bootstrap.Modal.getInstance(modalEl);
        if (modal) modal.hide();
      }, 1100);

    } catch (e) {
      console.error("trade create failed:", e);
      setStatus("Create trade failed.", "text-danger");
      $btn.prop("disabled", false);
    }
  }

  function bindGenericCreateClicks() {
    $(document).on("click", ".js-trade-create", async function (e) {
      e.preventDefault();
      e.stopPropagation();

      const btn = this;
      const source = String($(btn).attr("data-source") || "watchlist").trim().toLowerCase();
      const enc = $(btn).attr("data-record") || "";

      let info = null;
      try {
        info = JSON.parse(decodeURIComponent(enc));
      } catch (err) {
        console.error("Invalid shared trade-create payload", err);
        return;
      }

      btn.disabled = true;
      try {
        await openFromRow(info, source);
      } catch (err) {
        console.error("shared create-trade failed:", err);
      } finally {
        btn.disabled = false;
      }
    });
  }

  function bindEvents() {
    $(document).on("change", 'input[name="wtm-inst"]', refreshModal);
    $(document).on("change", "#wtm-trading-symbol", refreshModal);
    $(document).on("change", 'input[name="wtm-side"]', refreshModal);
    $(document).on("change", 'input[name="wtm-mode"]', refreshBasicAdvanced);
    $(document).on("input change", "#wtm-lots", refreshQtyAndRequired);
    $(document).on("change", "#wtm-product", function () {
      enforceSideRules();
      refreshTradeWarnings();
    });

    $(document).on("change", "#wtm-save-for-later", function () {
      updatePrimaryButton();
      if ($(this).prop("checked")) {
        setStatus("Trade will be created in CREATED status for later review.", "text-muted");
      } else {
        setStatus("Trade will be created and queued immediately.", "text-muted");
      }
    });

    $(document).on("change", ".wtm-user-check", function () {
      updateSelectedUserCount();
      refreshTradeWarnings();
    });

    $(document).on("click", "#wtm-users-select-all", function () {
      $(".wtm-user-check:not(:disabled)").prop("checked", true);
      updateSelectedUserCount();
      refreshTradeWarnings();
    });

    $(document).on("click", "#wtm-users-clear-all", function () {
      $(".wtm-user-check:not(:disabled)").prop("checked", false);
      updateSelectedUserCount();
      refreshTradeWarnings();
    });

    $(document).on("click", "#wtm-submit-btn", async function (e) {
      e.preventDefault();
      await submit();
    });

    bindGenericCreateClicks();
  }

  $(document).ready(() => {
    initState();
    bindEvents();
  });

  window.TradeCreateModal = {
    openFromRow,
    submit,
    openModal,
    refreshModal,
    setStatus
  };
})();