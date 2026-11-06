/* Заявка (06.11.2026, SHIP): статус на пратката и шаблон по клиент.
 *  1) Лентата на пратката (templates/_shipment_status_bar.html): отворено
 *     меню на етап затваря другите; Escape/клик извън менюто го затваря.
 *     Без JS лентата работи (формите са в <details>).
 *  2) Шаблон по клиент във формите за ЧМР и опаковъчен лист: при избор на
 *     клиент празните полета се попълват по последния издаден документ за
 *     него (превозвач, шофьор, места, стока, Incoterms…) със значка „авто“ —
 *     предложение, не задължение: попълненото на ръка не се пипа, а
 *     „Махни предложенията“ връща празните полета. Текстовете идват
 *     преведени от сървъра (/shipment/client-template). */
(function () {
  "use strict";
  var script = document.currentScript;
  var templateUrl = (script && script.dataset.templateUrl) || "/shipment/client-template";

  function initMenus() {
    var menus = document.querySelectorAll("details[data-ship-menu]");
    if (!menus.length) return;
    Array.prototype.forEach.call(menus, function (d) {
      d.addEventListener("toggle", function () {
        if (!d.open) return;
        Array.prototype.forEach.call(menus, function (o) { if (o !== d) o.open = false; });
        var first = d.querySelector("input:not([type=hidden]), select, button");
        if (first && first.type !== "file") { try { first.focus({ preventScroll: true }); } catch (e) { /* стар браузър */ } }
      });
    });
    document.addEventListener("click", function (e) {
      Array.prototype.forEach.call(menus, function (d) {
        if (d.open && !d.contains(e.target)) d.open = false;
      });
    });
    document.addEventListener("keydown", function (e) {
      if (e.key !== "Escape") return;
      Array.prototype.forEach.call(menus, function (d) {
        if (d.open) {
          d.open = false;
          var s = d.querySelector("summary");
          if (s) s.focus();
        }
      });
    });
  }

  var FORM_TYPES = { "/cmr/new": ["cmr", "consignee_name"], "/packing/new": ["packing", "receiver_name"] };

  function initClientTemplate() {
    var conf = FORM_TYPES[window.location.pathname];
    var form = document.getElementById("main-doc-form");
    if (!conf || !form || form.dataset.edit) return;
    var docType = conf[0];
    var nameInput = form.querySelector('[name="' + conf[1] + '"]');
    if (!nameInput) return;
    var note = null;
    var filled = [];
    var seq = 0;
    var lastClient = "";

    // Етикетът на полето — по for=id, иначе първият в същия блок (напр.
    // „Разтоварен пункт“: етикетът сочи падащия списък над полето).
    function labelOf(el) {
      var label = el.id ? form.querySelector('label[for="' + el.id + '"]') : null;
      return label || (el.parentNode && el.parentNode.querySelector("label"));
    }

    function badgeFor(el, text, title) {
      var label = labelOf(el);
      if (!label || label.querySelector(".ship-template-badge")) return;
      var b = document.createElement("span");
      b.className = "badge ship-template-badge";
      b.style.marginLeft = "6px";
      b.textContent = text;
      b.title = title;
      label.appendChild(b);
    }

    function dropBadge(el) {
      var label = labelOf(el);
      var b = label && label.querySelector(".ship-template-badge");
      if (b) b.parentNode.removeChild(b);
    }

    function clearSuggestions() {
      filled.forEach(function (item) {
        if (item.el.value === item.value) item.el.value = item.before;
        dropBadge(item.el);
      });
      filled = [];
      if (note && note.parentNode) note.parentNode.removeChild(note);
      note = null;
    }

    function setField(el, value, before, texts) {
      el.value = value;
      el.classList.remove("autofilled");
      void el.offsetWidth;
      el.classList.add("autofilled");
      badgeFor(el, texts.badge, texts.badge_title);
      filled.push({ el: el, value: value, before: before });
      el.addEventListener("input", function onInput() {
        dropBadge(el);
        el.removeEventListener("input", onInput);
      });
    }

    function apply(data) {
      var texts = data.texts || {};
      Object.keys(data.fields || {}).forEach(function (key) {
        var el = form.querySelector('[name="' + key + '"]');
        if (!el || el.type === "hidden" || el.readOnly || el.disabled) return;
        if (String(el.value || "").trim()) return;  // ръчно попълненото не се пипа
        setField(el, data.fields[key], el.value, texts);
      });
      // Езикът на изпращача (БГ/EN), както при последния документ — само
      // ако полетата още са с данните на другия език от Настройки.
      var lang = data.sender_lang, sender = data.sender || {};
      if (lang && sender.bg && sender.en) {
        var other = sender[lang === "en" ? "bg" : "en"], want = sender[lang];
        var nameEl = form.querySelector('[name="sender_name"]');
        if (nameEl && other.name && nameEl.value === other.name && want.name) {
          Object.keys(want).forEach(function (f) {
            var el = form.querySelector('[name="sender_' + f + '"]');
            if (el && want[f] && el.value === (other[f] || "")) setField(el, want[f], el.value, texts);
          });
        }
      }
      if (!filled.length) return;
      note = document.createElement("div");
      note.className = "ship-template-note";
      note.setAttribute("role", "status");
      var span = document.createElement("span");
      span.textContent = (texts.note || "").replace("{source}", data.source || "").replace("{count}", filled.length);
      var undo = document.createElement("button");
      undo.type = "button";
      undo.className = "btn-small btn-secondary";
      undo.style.marginLeft = "8px";
      undo.textContent = texts.undo || "×";
      undo.addEventListener("click", clearSuggestions);
      note.appendChild(span);
      note.appendChild(undo);
      var place = form.querySelector("fieldset, .card");
      if (place && place.parentNode) place.parentNode.insertBefore(note, place);
      else form.insertBefore(note, form.firstChild);
    }

    function lookup() {
      var client = String(nameInput.value || "").trim();
      if (!client || client === lastClient) return;
      lastClient = client;
      clearSuggestions();
      var mine = ++seq;
      fetch(templateUrl + "?type=" + encodeURIComponent(docType) + "&client=" + encodeURIComponent(client),
            { credentials: "same-origin", headers: { "Accept": "application/json" } })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (data) {
          if (mine !== seq || !data || !data.found) return;
          apply(data);
        })
        .catch(function () { /* предложението е удобство — без грешка за оператора */ });
    }

    var select = form.querySelector("select.client-select");
    if (select) select.addEventListener("change", function () { setTimeout(lookup, 0); });
    nameInput.addEventListener("change", lookup);
  }

  function init() {
    initMenus();
    initClientTemplate();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
