// PH Logistics — единен адресник и глобално търсене.
// Заявка (06.11.2026, BOOK): зарежда се СЛЕД app.js и ползва неговите
// fetchJsonSafe/SESSION_EXPIRED_MSG. Текстовете идват преведени от data-*
// атрибутите на маркъпа (base.html, _invoice_macros.html), без вграден <script>.
(function () {
  "use strict";

  function txt(el, name, fallback) {
    var v = el && el.getAttribute("data-t-" + name);
    return v || fallback;
  }

  function fill(tpl, params) {
    return String(tpl).replace(/\{(\w+)\}/g, function (whole, k) {
      return Object.prototype.hasOwnProperty.call(params, k) ? String(params[k]) : whole;
    });
  }

  function isEditable(el) {
    if (!el) return false;
    var tag = el.tagName;
    return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || el.isContentEditable;
  }

  /* ------------------------------------------------------------ глобално търсене */
  function initGlobalSearch() {
    var form = document.getElementById("gs-form");
    var input = document.getElementById("gs-input");
    if (!form || !input || typeof window.fetch !== "function") return;
    var liveUrl = form.getAttribute("data-live-url");
    var panel = document.createElement("div");
    panel.id = "gs-results";
    panel.className = "gs-panel no-print";
    panel.setAttribute("role", "listbox");
    panel.setAttribute("aria-label", input.getAttribute("aria-label") || "");
    panel.hidden = true;
    document.body.appendChild(panel);
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-controls", panel.id);
    input.setAttribute("aria-expanded", "false");
    input.setAttribute("aria-autocomplete", "list");

    var timer = null, controller = null, seq = 0, options = [], active = -1, lastQuery = null;

    function position() {
      var r = input.closest(".sidebar-search").getBoundingClientRect();
      var vw = document.documentElement.clientWidth;
      var narrow = vw < 700;
      var left = narrow ? 8 : r.left;
      var width = narrow ? vw - 16 : Math.min(620, vw - left - 16);
      panel.style.left = left + "px";
      panel.style.top = (r.bottom + 6) + "px";
      panel.style.width = width + "px";
      panel.style.maxHeight = Math.max(160, window.innerHeight - r.bottom - 18) + "px";
    }

    function open() {
      if (!panel.hidden) return;
      position();
      panel.hidden = false;
      input.setAttribute("aria-expanded", "true");
    }

    function close() {
      panel.hidden = true;
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-activedescendant");
      setActive(-1);
    }

    function setActive(i) {
      options.forEach(function (o, n) { o.classList.toggle("is-active", n === i); o.setAttribute("aria-selected", n === i ? "true" : "false"); });
      active = i;
      if (i >= 0 && options[i]) {
        input.setAttribute("aria-activedescendant", options[i].id);
        options[i].scrollIntoView({ block: "nearest" });
      } else {
        input.removeAttribute("aria-activedescendant");
      }
    }

    function message(text) {
      panel.textContent = "";
      var p = document.createElement("div");
      p.className = "gs-msg";
      p.textContent = text;
      panel.appendChild(p);
      options = [];
      active = -1;
      open();
    }

    function option(href, cls) {
      var a = document.createElement("a");
      a.href = href;
      a.className = cls;
      a.setAttribute("role", "option");
      a.id = "gs-opt-" + options.length;
      a.tabIndex = -1;
      a.addEventListener("mousedown", function (e) { e.preventDefault(); });  // фокусът остава в полето
      options.push(a);
      return a;
    }

    function render(data) {
      panel.textContent = "";
      options = [];
      active = -1;
      var groups = (data && data.groups) || [];
      if (!groups.length) {
        message(txt(form, "none", "Няма намерени резултати."));
        return;
      }
      groups.forEach(function (g) {
        var head = document.createElement("div");
        head.className = "gs-group";
        head.setAttribute("role", "presentation");
        head.textContent = g.label;
        panel.appendChild(head);
        g.items.forEach(function (it) {
          var a = option(it.url || "#", "gs-item");
          var title = document.createElement("span");
          title.className = "gs-title";
          title.textContent = it.title;
          a.appendChild(title);
          if (it.subtitle) {
            var sub = document.createElement("span");
            sub.className = "gs-sub";
            sub.textContent = it.subtitle;
            a.appendChild(sub);
          }
          if (it.where && it.where.length) {
            var w = document.createElement("span");
            w.className = "gs-where";
            w.textContent = txt(form, "found-in", "Намерено в:") + " " + it.where.map(function (x) {
              return x.field + (x.row ? " (" + txt(form, "row", "ред") + " " + x.row + ")" : "") + ": " + x.value;
            }).join(" · ");
            a.appendChild(w);
          }
          panel.appendChild(a);
        });
        if (g.more && g.more_url) {
          var more = option(g.more_url, "gs-more");
          more.textContent = txt(form, "more", "Виж всички в раздела") + " →";
          panel.appendChild(more);
        }
      });
      var all = option(data.page_url || (form.action + "?q=" + encodeURIComponent(input.value.trim())), "gs-all");
      all.textContent = fill(txt(form, "all", "Всички резултати за „{q}“ (Enter)"), { q: data.query || input.value.trim() });
      panel.appendChild(all);
      open();
    }

    function run() {
      var q = input.value.trim();
      if (q === lastQuery && !panel.hidden) return;
      lastQuery = q;
      var mySeq = ++seq;
      if (controller) { try { controller.abort(); } catch (e) {} }
      controller = (typeof AbortController === "function") ? new AbortController() : null;
      if (q.length < 2) { close(); return; }
      if (panel.hidden || !options.length) message(txt(form, "searching", "Търсене…"));
      panel.classList.add("is-loading");
      fetchJsonSafe(liveUrl + "?q=" + encodeURIComponent(q),
        { signal: controller ? controller.signal : undefined })
        .then(function (data) {
          if (mySeq !== seq) return;   // закъснял отговор
          render(data);
        })
        .catch(function (err) {
          if (err && err.name === "AbortError") return;
          if (mySeq !== seq) return;
          message(err && err.sessionExpired ? SESSION_EXPIRED_MSG
            : txt(form, "error", "Търсенето не успя — опитайте пак."));
        })
        .then(function () { if (mySeq === seq) panel.classList.remove("is-loading"); });
    }

    input.addEventListener("input", function () {
      clearTimeout(timer);
      timer = setTimeout(run, 200);
    });
    input.addEventListener("focus", function () {
      if (input.value.trim().length >= 2 && options.length) open();
    });
    input.addEventListener("blur", function () {
      setTimeout(function () { if (document.activeElement !== input) close(); }, 150);
    });
    input.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        if (panel.hidden) { lastQuery = null; run(); return; }
        if (!options.length) return;
        e.preventDefault();
        var n = options.length;
        setActive(e.key === "ArrowDown" ? (active + 1) % n : (active <= 0 ? n - 1 : active - 1));
      } else if (e.key === "Enter") {
        if (!panel.hidden && active >= 0 && options[active]) {
          e.preventDefault();
          window.location.href = options[active].href;
        } else if (input.value.trim().length < 2) {
          e.preventDefault();
        }
        // иначе — обикновено изпращане към /search?q=
      } else if (e.key === "Escape") {
        if (!panel.hidden) { e.preventDefault(); close(); }
        else if (input.value) { e.preventDefault(); input.value = ""; lastQuery = null; }
        else { input.blur(); }
      }
    });
    window.addEventListener("resize", function () { if (!panel.hidden) position(); });
    document.addEventListener("mousedown", function (e) {
      if (!panel.hidden && e.target !== input && !panel.contains(e.target)) close();
    });

    /* Отворен диалог (потвърждение, камера, вид палет) — без пряк път. */
    function modalOpen() {
      return Array.prototype.some.call(document.querySelectorAll(".pl-modal, .camera-modal"), function (m) {
        return m.style.display && m.style.display !== "none";
      });
    }

    function focusSearch() {
      var sidebar = document.getElementById("sidebar");
      var hiddenDrawer = sidebar && window.matchMedia && window.matchMedia("(max-width: 980px)").matches
        && !sidebar.classList.contains("open");
      if (hiddenDrawer) {
        var toggle = document.getElementById("sidebar-toggle");
        if (toggle) toggle.click();
      }
      input.focus();
      input.select();
    }

    /* Ctrl+K (Cmd+K) от всяка страница; „/“ — само извън поле за въвеждане. */
    document.addEventListener("keydown", function (e) {
      if (e.defaultPrevented) return;
      var k = (e.key || "").toLowerCase();
      var isCtrlK = (e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey && (k === "k" || e.code === "KeyK");
      var isSlash = e.key === "/" && !e.ctrlKey && !e.metaKey && !e.altKey && !isEditable(document.activeElement);
      if (!isCtrlK && !isSlash) return;
      if (modalOpen()) return;
      e.preventDefault();
      focusSearch();
    });
    var mobileBtn = document.getElementById("gs-mobile-btn");
    if (mobileBtn) mobileBtn.addEventListener("click", focusSearch);
  }

  /* ------------------------------------------------------------ фактура: друг клиент */
  function initInvoiceMismatch() {
    var box = document.querySelector(".book-mismatch");
    var select = document.querySelector(".invoice-client-select");
    if (!box || !select) return;
    var form = select.closest("form") || document;
    var names = [];
    var controller = null, seq = 0;

    function hide() { box.hidden = true; box.textContent = ""; }

    function check() {
      var mySeq = ++seq;
      if (controller) { try { controller.abort(); } catch (e) {} }
      if (!select.value || !names.length) { hide(); return; }
      controller = (typeof AbortController === "function") ? new AbortController() : null;
      var params = new URLSearchParams();
      params.set("entry_id", select.value);
      names.forEach(function (n) { params.append("name", n); });
      fetchJsonSafe(box.getAttribute("data-check-url") + "?" + params.toString(),
        { signal: controller ? controller.signal : undefined })
        .then(function (data) {
          if (mySeq !== seq) return;
          if (!data || !data.mismatch) { hide(); return; }
          box.textContent = "";
          var b = document.createElement("b");
          b.textContent = box.getAttribute("data-title");
          box.appendChild(b);
          box.appendChild(document.createTextNode(" " + fill(box.getAttribute("data-detail"),
            { shipment: data.shipment_names.join(", "), linked: data.linked_name })));
          box.hidden = false;
        })
        .catch(function (err) {
          if (err && err.name === "AbortError") return;
          /* Предупреждението е помощно — при грешка просто не се показва. */
        });
    }

    form.addEventListener("ph:shipment-clients", function (e) {
      var list = (e.detail && e.detail.names) || [];
      list.forEach(function (n) { if (n && names.indexOf(n) < 0) names.push(n); });
      check();
    });
    select.addEventListener("change", check);
    /* Друг модул (напр. връзка с пратка) може да подаде клиента и чрез атрибут. */
    var preset = form.getAttribute && form.getAttribute("data-shipment-clients");
    if (preset) {
      try { names = JSON.parse(preset) || []; } catch (e) { names = []; }
      check();
    }
  }

  function init() {
    initGlobalSearch();
    initInvoiceMismatch();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
