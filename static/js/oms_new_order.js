/* =========================
OMS NEW ORDER
========================= */

(function () {
    "use strict";

    /* =========================
    CONFIG
    ========================= */

    const CFG = window.OMS_NEW_ORDER_CONFIG || {
        clientsUrl: "/dashboard/users/tradeable"
    };

    /* =========================
    GLOBAL STATE
    ========================= */

    let equityMap = {};
    let currentLotSize = 1;
    let lots = 1;
    let side = "BUY";
    let marketDataErrorShown = false;

    /* =========================
    UI HELPERS
    ========================= */

    function showToast(message, type = "success") {
        const toastEl = document.getElementById("omsToast");
        const msgEl = document.getElementById("toastMessage");

        if (!toastEl || !msgEl) return;

        msgEl.textContent = message || "Done";

        toastEl.classList.remove(
            "text-bg-success",
            "text-bg-danger",
            "text-bg-warning",
            "text-bg-secondary"
        );

        const clsMap = {
            success: "text-bg-success",
            error: "text-bg-danger",
            warning: "text-bg-warning",
            info: "text-bg-secondary"
        };

        toastEl.classList.add(clsMap[type] || clsMap.success);

        const toast = new bootstrap.Toast(toastEl);
        toast.show();
    }

    function text(v, fallback = "") {
        const s = String(v ?? "").trim();
        return s || fallback;
    }

    function updatePlaceOrderButton() {
        const btn = document.getElementById("placeOrderBtn");
        if (!btn) return;

        const hasClient = document.querySelectorAll(".client-cb:checked").length > 0;
        btn.disabled = !hasClient;
    }

    function renderCapital(value) {
        const capitalEl = document.getElementById("capital");
        if (!capitalEl) return;

        const n = Number(value);
        if (!Number.isFinite(n)) {
            capitalEl.value = "₹ 0.00";
            return;
        }

        capitalEl.value = "₹ " + n.toFixed(2);
    }

    /* =========================
    CLIENT PANEL
    ========================= */

    async function loadClients() {
        try {
            const res = await fetch(CFG.clientsUrl, {
                method: "GET",
                headers: { "Accept": "application/json" },
                credentials: "same-origin"
            });

            const payload = await res.json();
            const rows = payload?.data || [];

            const container = document.getElementById("client-list");
            if (!container) return;

            container.innerHTML = "";

            rows.forEach(c => {
                const userid = text(c.userid);
                const selected = c.selected ? "checked" : "";
                const disabled = c.disabled ? "disabled" : "";

                const row = document.createElement("div");
                row.className = "d-flex justify-content-between align-items-center client-row";

                row.innerHTML = `
                    <span class="client-name">${userid}</span>
                    <input type="checkbox" class="client-cb" value="${userid}" ${selected} ${disabled}>
                `;

                container.appendChild(row);
            });

            container.querySelectorAll(".client-cb").forEach(cb => {
                cb.addEventListener("change", updatePlaceOrderButton);
            });

            updatePlaceOrderButton();

        } catch (err) {
            console.error("loadClients failed", err);
            showToast("Unable to load clients", "error");
        }
    }

    function selectAllClients(master) {
        const clients = document.querySelectorAll(".client-cb");
        clients.forEach(cb => {
            if (!cb.disabled) cb.checked = master.checked;
        });
        updatePlaceOrderButton();
    }

    function filterClients() {
        const search = document
            .getElementById("client-search")
            .value
            .toLowerCase()
            .trim();

        const rows = document.querySelectorAll(".client-row");

        rows.forEach(row => {
            const name = row
                .querySelector(".client-name")
                .textContent
                .toLowerCase();

            row.style.display = name.includes(search) ? "flex" : "none";
        });
    }

    /* =========================
    LOTS / QUANTITY
    ========================= */

    function updateQuantity() {
        const segment = document.querySelector('input[name="segment"]:checked').value;

        if (segment === "equity") {
            updateCapital();
            return;
        }

        lots = parseInt(document.getElementById("lots")?.value, 10) || 1;

        const qty = lots * currentLotSize;
        const qtyEl = document.getElementById("qty");
        if (qtyEl) qtyEl.value = qty;

        updateCapital();
    }

    function incLots() {
        lots++;
        const lotsEl = document.getElementById("lots");
        if (lotsEl) lotsEl.value = lots;
        updateQuantity();
    }

    function decLots() {
        if (lots > 1) lots--;
        const lotsEl = document.getElementById("lots");
        if (lotsEl) lotsEl.value = lots;
        updateQuantity();
    }

    /* =========================
    CAPITAL
    ========================= */

    function updateCapital() {
        const qty = parseFloat(document.getElementById("qty")?.value) || 0;
        const price = parseFloat(document.getElementById("price")?.value) || 0;
        const capital = qty * price;
        renderCapital(capital);
    }

    /* =========================
    BUY / SELL
    ========================= */

    function selectSide(type) {
        const buyBtn = document.getElementById("buyBtn");
        const sellBtn = document.getElementById("sellBtn");

        if (!buyBtn || !sellBtn) return;

        buyBtn.classList.remove("active-side", "inactive-side");
        sellBtn.classList.remove("active-side", "inactive-side");

        if (type === "BUY") {
            buyBtn.classList.add("active-side");
            sellBtn.classList.add("inactive-side");
            side = "BUY";
        } else {
            sellBtn.classList.add("active-side");
            buyBtn.classList.add("inactive-side");
            side = "SELL";
        }
    }

    /* =========================
    SEGMENT SWITCH
    ========================= */

    function toggleSegment() {
        const segment = document.querySelector('input[name="segment"]:checked').value;
        const product = document.getElementById("product");

        if (segment === "equity") {
            document.getElementById("equityFields").style.display = "block";
            document.getElementById("foFields").style.display = "none";

            if (document.getElementById("fo-symbol-container")) {
                document.getElementById("fo-symbol-container").style.display = "none";
            }
            if (document.getElementById("lots-container")) {
                document.getElementById("lots-container").style.display = "none";
            }

            document.getElementById("qty").removeAttribute("readonly");

            if (product) {
                product.innerHTML = `
                    <option value="CNC" selected>CNC</option>
                    <option value="MIS">MIS</option>
                `;
            }

            loadEquitySymbols();
        } else {
            document.getElementById("equityFields").style.display = "none";
            document.getElementById("foFields").style.display = "block";

            if (document.getElementById("fo-symbol-container")) {
                document.getElementById("fo-symbol-container").style.display = "block";
            }
            if (document.getElementById("lots-container")) {
                document.getElementById("lots-container").style.display = "block";
            }

            document.getElementById("qty").setAttribute("readonly", true);

            if (product) {
                product.innerHTML = `
                    <option value="NRML" selected>NRML</option>
                    <option value="MIS">MIS</option>
                `;
            }

            loadUnderlying();
        }
    }

    /* =========================
    EQUITY SYMBOLS
    ========================= */

    async function loadEquitySymbols() {
        const res = await fetch("/oms/equity-symbols", {
            credentials: "same-origin"
        });

        const payload = await res.json();
        const rows = payload?.data || [];

        const dropdown = document.getElementById("eq-symbol");
        if (!dropdown) return;

        dropdown.innerHTML = "";
        equityMap = {};

        rows.forEach(item => {
            equityMap[item.symbol] = item.price;

            const opt = document.createElement("option");
            opt.value = item.symbol;
            opt.text = item.symbol;
            dropdown.appendChild(opt);
        });

        if (dropdown.options.length > 0) {
            dropdown.selectedIndex = 0;
            onEquityChange();
        }
    }

    async function onEquityChange() {
        const symbol = document.getElementById("eq-symbol")?.value;
        if (!symbol) return;

        try {
            await loadLTP(symbol);
        } catch (e) {
            if (symbol in equityMap) {
                document.getElementById("price").value = equityMap[symbol];
                updateCapital();
            }
        }
    }

    /* =========================
    F&O DATA
    ========================= */

    async function loadUnderlying() {
        const res = await fetch("/oms/fo-underlyings", {
            credentials: "same-origin"
        });

        const payload = await res.json();
        const rows = payload?.data || [];

        const dd = document.getElementById("fo-underlying");
        if (!dd) return;

        dd.innerHTML = "";

        rows.forEach(v => {
            const opt = document.createElement("option");
            opt.value = v;
            opt.text = v;
            dd.appendChild(opt);
        });

        if (dd.options.length > 0) {
            dd.selectedIndex = 0;
            await loadExpiry();
        }
    }

    async function loadExpiry() {
        const symbol = document.getElementById("fo-underlying")?.value;
        if (!symbol) return;

        const res = await fetch(`/oms/fo-expiry?symbol=${encodeURIComponent(symbol)}`, {
            credentials: "same-origin"
        });

        const payload = await res.json();
        const rows = payload?.data || [];

        const dd = document.getElementById("fo-expiry");
        if (!dd) return;

        dd.innerHTML = "";

        rows.forEach(v => {
            const d = new Date(v);

            const year = d.getFullYear();
            const month = String(d.getMonth() + 1).padStart(2, "0");
            const day = String(d.getDate()).padStart(2, "0");

            const isoDate = `${year}-${month}-${day}`;
            const labelMonth = d.toLocaleString("en", { month: "short" }).toUpperCase();

            const opt = document.createElement("option");
            opt.value = isoDate;
            opt.text = `${day} ${labelMonth} ${year}`;
            dd.appendChild(opt);
        });

        if (dd.options.length > 0) {
            dd.selectedIndex = 0;
            await loadStrike();
        }
    }

    async function loadStrike() {
        const symbol = document.getElementById("fo-underlying")?.value;
        const expiry = document.getElementById("fo-expiry")?.value;
        const type = document.getElementById("fo-type")?.value;
        const strikeDD = document.getElementById("fo-strike");

        if (!symbol || !expiry || !type || !strikeDD) return;

        if (type === "FUT") {
            strikeDD.disabled = true;
            strikeDD.innerHTML = "";
            await loadInstrument();
            return;
        }

        strikeDD.disabled = false;

        const res = await fetch(
            `/oms/fo-strikes?symbol=${encodeURIComponent(symbol)}&expiry=${encodeURIComponent(expiry)}&type=${encodeURIComponent(type)}`,
            { credentials: "same-origin" }
        );

        const payload = await res.json();
        const rows = payload?.data || [];

        strikeDD.innerHTML = "";

        rows.forEach(v => {
            const opt = document.createElement("option");
            opt.value = v;
            opt.text = v;
            strikeDD.appendChild(opt);
        });

        if (strikeDD.options.length > 0) {
            strikeDD.selectedIndex = 0;
            await loadInstrument();
        }
    }

    async function loadInstrument() {
        const symbol = document.getElementById("fo-underlying")?.value;
        const expiry = document.getElementById("fo-expiry")?.value;
        const type = document.getElementById("fo-type")?.value;
        const strike = document.getElementById("fo-strike")?.value || "";

        if (!symbol || !expiry || !type) return;

        const res = await fetch(
            `/oms/fo-instrument?symbol=${encodeURIComponent(symbol)}&expiry=${encodeURIComponent(expiry)}&type=${encodeURIComponent(type)}&strike=${encodeURIComponent(strike)}`,
            { credentials: "same-origin" }
        );

        const payload = await res.json();
        const data = payload?.data || payload || {};

        if (!data.symbol) return;

        const symbolEl = document.getElementById("fo-symbol");
        if (symbolEl) symbolEl.value = data.symbol;

        currentLotSize = data.lotsize || 1;
        lots = 1;

        const lotsEl = document.getElementById("lots");
        if (lotsEl) lotsEl.value = 1;

        updateQuantity();
        await loadLTP(data.symbol);
    }

    /* =========================
    PRICE FIELD
    ========================= */

    function togglePriceField() {
        const type = document.getElementById("order-type")?.value;
        const price = document.getElementById("price");

        if (!price) return;

        if (type === "MARKET") {
            price.disabled = true;
            price.value = "";
        } else {
            price.disabled = false;
        }

        updateCapital();
    }

    /* =========================
    PLACE ORDER
    ========================= */

    async function placeOrder() {
        if (!validateOrder()) return;

        const segment = document.querySelector('input[name="segment"]:checked').value;
        const exchange = (segment === "equity") ? "NSE" : "NFO";

        let symbol = "";
        if (segment === "equity") {
            symbol = document.getElementById("eq-symbol")?.value || "";
        } else {
            symbol = document.getElementById("fo-symbol")?.value || "";
        }

        const qty = document.getElementById("qty")?.value;
        let price = document.getElementById("price")?.value;
        const product = document.getElementById("product")?.value;
        let orderType = document.getElementById("order-type")?.value;

        const clients = [];
        document.querySelectorAll(".client-cb:checked").forEach(cb => {
            clients.push(cb.value);
        });

        if (clients.length === 0) {
            showToast("Please select at least one client.", "warning");
            return;
        }

        if (!symbol) {
            showToast("Please select an instrument.", "warning");
            return;
        }

        if (!qty || Number(qty) <= 0) {
            showToast("Enter valid quantity.", "warning");
            return;
        }

        if (orderType === "LIMIT" && (!price || Number(price) <= 0)) {
            showToast("Enter valid price.", "warning");
            return;
        }

        if (segment === "fo" && orderType === "MARKET") {
            orderType = "LIMIT";
            price = document.getElementById("price")?.value;
        }

        const payload = {
            exchange: exchange,
            symbol: symbol,
            qty: qty,
            price: price,
            order_type: orderType,
            product: product,
            transaction_type: side,
            clients: clients
        };

        const btn = document.getElementById("placeOrderBtn");
        const originalText = btn ? btn.textContent : "Place Order";

        try {
            if (btn) {
                btn.disabled = true;
                btn.textContent = "Placing...";
            }

            const res = await fetch("/oms/place-order", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                credentials: "same-origin",
                body: JSON.stringify(payload)
            });

            const payloadResp = await res.json();
            const results = payloadResp?.data?.results || payloadResp?.results || [];

            if (!res.ok) {
                throw new Error(payloadResp.message || "Order failed");
            }

            let successCount = 0;
            let failCount = 0;

            results.forEach(r => {
                if (r.status === "success") successCount++;
                else failCount++;
            });

            if (failCount === 0) {
                showToast(`All orders placed (${successCount})`, "success");
            } else if (successCount === 0) {
                showToast(`All orders failed (${failCount})`, "error");
            } else {
                showToast(`Success: ${successCount}, Failed: ${failCount}`, "warning");
            }

        } catch (err) {
            console.error(err);
            showToast(err.message || "Order failed", "error");
        } finally {
            if (btn) {
                btn.textContent = originalText;
                updatePlaceOrderButton();
            }
        }
    }

    /* =========================
    GET LTP
    ========================= */

    async function loadLTP(symbol) {
        if (!symbol) return;

        const segment = document.querySelector('input[name="segment"]:checked').value;
        const exchange = (segment === "equity") ? "NSE" : "NFO";

        const res = await fetch(
            `/oms/ltp?symbol=${encodeURIComponent(symbol)}&exchange=${encodeURIComponent(exchange)}`,
            { credentials: "same-origin" }
        );

        const payload = await res.json();
        const data = payload?.data || payload || {};

        if (payload.status === "error" || data.error) {
            if (!marketDataErrorShown) {
                showToast("Live market data unavailable", "warning");
                marketDataErrorShown = true;
            }
            return;
        }

        if (data.ltp) {
            document.getElementById("price").value = data.ltp;
            updateCapital();
        }
    }

    /* =========================
    VALIDATION
    ========================= */

    function validateOrder() {
        const qty = document.getElementById("qty")?.value;

        if (!Number.isInteger(Number(qty)) || Number(qty) <= 0) {
            showToast("Quantity must be a positive integer", "warning");
            return false;
        }

        return true;
    }

    /* =========================
    BIND EVENTS
    ========================= */

    function bindEvents() {
        document.getElementById("buyBtn")?.addEventListener("click", function () {
            selectSide("BUY");
        });

        document.getElementById("sellBtn")?.addEventListener("click", function () {
            selectSide("SELL");
        });

        document.querySelectorAll('input[name="segment"]').forEach(el => {
            el.addEventListener("change", toggleSegment);
        });

        document.getElementById("order-type")?.addEventListener("change", togglePriceField);
        document.getElementById("eq-symbol")?.addEventListener("change", onEquityChange);
        document.getElementById("price")?.addEventListener("input", updateCapital);

        document.getElementById("qty")?.addEventListener("input", function () {
            this.value = this.value.replace(/[^0-9]/g, "");
            updateCapital();
        });

        document.getElementById("fo-underlying")?.addEventListener("change", loadExpiry);
        document.getElementById("fo-expiry")?.addEventListener("change", loadStrike);
        document.getElementById("fo-type")?.addEventListener("change", loadStrike);
        document.getElementById("fo-strike")?.addEventListener("change", loadInstrument);

        document.getElementById("selectAllClients")?.addEventListener("change", function () {
            selectAllClients(this);
        });

        document.getElementById("client-search")?.addEventListener("input", filterClients);
        document.getElementById("placeOrderBtn")?.addEventListener("click", placeOrder);

        document.querySelectorAll(".btn-lots-inc").forEach(btn => {
            btn.addEventListener("click", incLots);
        });

        document.querySelectorAll(".btn-lots-dec").forEach(btn => {
            btn.addEventListener("click", decLots);
        });

        document.getElementById("cancelBtn")?.addEventListener("click", function () {
            window.location.href = "/oms/orders";
        });
    }

    /* =========================
    INIT
    ========================= */

    document.addEventListener("DOMContentLoaded", async function () {
        bindEvents();
        selectSide("BUY");
        renderCapital(0);
        await loadClients();
        toggleSegment();
    });

    /* =========================
    EXPOSE FOR INLINE FALLBACKS
    ========================= */

    window.incLots = incLots;
    window.decLots = decLots;
})();