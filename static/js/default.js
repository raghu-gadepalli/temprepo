// static/js/default.js

/* ---------- shared UI helpers for all pages ---------- */
window.UI = (function () {
  function initTooltips(scope) {
    const sel = scope || "body";
    $(`${sel} [data-bs-toggle="tooltip"]`)
      .tooltip("dispose")
      .tooltip({ container: "body", boundary: "window", trigger: "hover" });
  }

  function renderBadge(type, tooltip) {
    const s = (type || "NO_TREND").toString().toUpperCase();
    const cls = s === "BUY" ? "traffic-light-green3"
      : s === "SELL" ? "traffic-light-red3"
        : "traffic-light-grey";
    return `<span class="traffic-light ${cls}" data-bs-toggle="tooltip" title="${tooltip || s}"></span>`;
  }

  function renderStrength(val, label) {
    if (label == null || label === "N/A") return `<span class="text-muted">N/A</span>`;
    const lower = String(label).toLowerCase();
    let cls = "text-secondary";
    if (lower.startsWith("strong")) cls = "text-success";
    else if (lower.startsWith("medium")) cls = "text-warning";
    else if (lower.startsWith("weak")) cls = "text-danger";
    const tip = (val != null && val !== "") ? val : label;
    return `<span data-bs-toggle="tooltip" title="${tip}" class="${cls}">${label}</span>`;
  }

  function renderConviction(text) {
    const val = text || "N/A";
    const cls = val.includes("UP") ? "text-success"
      : val.includes("DOWN") ? "text-danger"
        : "text-secondary";
    return `<span class="conviction ${cls} text-capitalize">${val.replace(/_/g, " ")}</span>`;
  }

  return { initTooltips, renderBadge, renderStrength, renderConviction };
})();

/* ---------- DashboardAPI + boot logic ---------- */
; (function (window, $) {
  const URLS = {
    watchlist: "/dashboard/watchlist/data",
    signals: "/dashboard/signals/data",
    orders: "/dashboard/orders/data",
    positions: "/dashboard/positions/data",
    notifications: "/notifications/data"
  };

  // Custom fetchers registered by pages that need stateful refresh.
  const REGISTRY = {};

  // -------- Notifications helpers --------
  function escapeHtml(str) {
    return String(str || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  function firstLine(text) {
    const s = (text || "").toString();
    const idx = s.indexOf("\n");
    return (idx >= 0 ? s.slice(0, idx) : s).trim();
  }

  function bindNotificationClicksOnce() {
    if (window.__notifClickBound) return;
    window.__notifClickBound = true;

    $(document).on("click", "#notifications-menu a.notification-link", function (e) {
      e.preventDefault();

      const msg = decodeURIComponent($(this).attr("data-message") || "");
      const title = $(this).attr("data-title") || "Alert";

      if (typeof Swal !== "undefined") {
        Swal.fire({
          title,
          html: `<pre style="text-align:left;white-space:pre-wrap;margin:0;">${escapeHtml(msg)}</pre>`,
          width: 650,
          confirmButtonText: "OK"
        });
      } else {
        alert(msg);
      }
    });
  }

  function renderNotifications(resp) {
    bindNotificationClicksOnce();

    const rows = resp.data || [];
    const menu = $("#notifications-menu").empty();

    const alerts = rows.map(r => {
      if (typeof r === "string") {
        return { id: 0, etime: "", message: r, processed: 0 };
      }
      return {
        id: r.id ?? 0,
        etime: r.etime ?? "",
        message: r.message ?? (r.text ?? ""),
        processed: Number(r.processed ?? 0)
      };
    });

    const unread = alerts.filter(a => !a.processed).length;
    $("#notification-badge").text(unread);

    menu.append(`
      <li><h6 class="dropdown-header">Notifications</h6></li>
      <li><hr class="dropdown-divider" /></li>
    `);

    if (!alerts.length) {
      menu.append('<li class="notification-item text-center px-3">No new notifications</li>');
      return;
    }

    alerts.forEach(a => {
      const preview = firstLine(a.message) || "Alert";
      const when = (a.etime || "").trim();
      const title = when ? when : "Alert";
      const unreadDot = a.processed ? "" : `<span class="badge bg-danger ms-2">new</span>`;

      menu.append(`
        <li>
          <a href="#"
             class="dropdown-item notification-link"
             data-id="${a.id}"
             data-title="${escapeHtml(title)}"
             data-message="${encodeURIComponent(a.message || "")}">
            <div class="d-flex justify-content-between align-items-start">
              <div class="me-2">
                <div class="fw-semibold">${escapeHtml(preview)}</div>
                ${when ? `<div class="small text-muted">${escapeHtml(when)}</div>` : ""}
              </div>
              ${unreadDot}
            </div>
          </a>
        </li>
      `);
    });
  }

  // -------- Registry --------
  function registerDataHandler(key, fn) {
    if (!key || typeof fn !== "function") return;
    REGISTRY[String(key).trim().toLowerCase()] = fn;
  }

  function fetchData(key) {
    const k = String(key || "").trim().toLowerCase();
    if (!k) return;

    if (REGISTRY[k]) {
      try {
        return REGISTRY[k]();
      } catch (e) {
        console.error(`DashboardAPI custom fetch failed for ${k}:`, e);
        return;
      }
    }

    const url = URLS[k];
    if (!url) {
      console.warn("DashboardAPI.fetch: unknown key", k);
      return;
    }

    return $.ajax({
      url,
      method: "GET",
      dataType: "text",
      success(respText) {
        let resp;

        try {
          resp = JSON.parse(respText);
        } catch (e) {
          console.warn(`Non-JSON response for ${k}, likely logged out.`);
          return;
        }

        if (!resp || resp.status !== "success") {
          return;
        }

        if (k === "notifications") {
          renderNotifications(resp);
          return;
        }

        const fnName = "populate" + k.charAt(0).toUpperCase() + k.slice(1);
        const fn = window[fnName];

        if (typeof fn === "function") {
          fn(resp.data);
        }
      },
      error(xhr, status, err) {
        if (xhr.status === 401 || xhr.status === 302) {
          console.warn(`Session expired while fetching ${k}`);
          return;
        }

        console.error(`Failed to fetch ${k} from ${url}:`, status, err);
      }
    });
  }

  window.DashboardAPI = {
    fetch: fetchData,
    register: registerDataHandler
  };

})(window, jQuery);

$(function () {
  DashboardAPI.fetch("notifications");

  setInterval(() => {
    DashboardAPI.fetch("notifications");
  }, 30000);
});