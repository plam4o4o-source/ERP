/* Заявка (09.11.2026): подредба на палети в камиона — редове на формата,
   готови размери на палетите и „Друг“ камион (templates/load_plan.html).
   Самата подредба се смята на сървъра (load_plan.py). */
(function () {
  "use strict";
  var form = document.querySelector("form[data-load-plan]");
  if (!form) return;
  var rows = form.querySelector("[data-lp-rows]");
  var tpl = form.querySelector("template[data-lp-row-template]");
  var addBtn = form.querySelector("[data-lp-add]");
  var MAX = 60;

  function syncAdd() {
    if (addBtn) addBtn.disabled = rows.children.length >= MAX;
  }

  if (addBtn && tpl) {
    addBtn.addEventListener("click", function () {
      if (rows.children.length >= MAX) return;
      var row = tpl.content.firstElementChild.cloneNode(true);
      rows.appendChild(row);
      var first = row.querySelector("input");
      if (first) first.focus();
      syncAdd();
    });
  }

  form.addEventListener("click", function (e) {
    var btn = e.target.closest ? e.target.closest("[data-lp-remove]") : null;
    if (!btn) return;
    var tr = btn.closest("tr");
    if (!tr) return;
    if (rows.children.length > 1) {
      tr.parentNode.removeChild(tr);
    } else {
      Array.prototype.forEach.call(tr.querySelectorAll("input"), function (i) { i.value = ""; });
    }
    syncAdd();
  });

  form.addEventListener("change", function (e) {
    var el = e.target;
    if (el.matches && el.matches("[data-lp-preset]") && el.value) {
      var tr = el.closest("tr");
      var parts = el.value.split("x");
      tr.querySelector("[data-lp-l]").value = parts[0];
      tr.querySelector("[data-lp-w]").value = parts[1];
    } else if (el.matches && (el.matches("[data-lp-l]") || el.matches("[data-lp-w]"))) {
      var row = el.closest("tr");
      var sel = row.querySelector("[data-lp-preset]");
      var val = row.querySelector("[data-lp-l]").value.trim() + "x" + row.querySelector("[data-lp-w]").value.trim();
      var found = Array.prototype.some.call(sel.options, function (o) { return o.value === val; });
      sel.value = found ? val : "";
    } else if (el.matches && el.matches("[data-lp-vehicle]")) {
      var custom = form.querySelector("[data-lp-custom]");
      if (custom) custom.classList.toggle("is-hidden", el.value !== "custom");
    }
  });
  syncAdd();
})();

/* Ред на товарене стъпка по стъпка (плъзгач) и посочване на палет — един и
   същи № се осветява в 3D изгледа, изгледа отгоре и таблицата. */
(function () {
  "use strict";
  var root = document.querySelector("[data-lp-result]");
  if (!root) return;
  var range = root.querySelector("[data-lp-range]");
  var out = root.querySelector("[data-lp-out]");
  var total = parseInt(root.getAttribute("data-total"), 10) || 0;
  var items = root.querySelectorAll("[data-no]");

  function show(n) {
    Array.prototype.forEach.call(items, function (el) {
      var no = parseInt(el.getAttribute("data-no"), 10);
      el.classList.toggle("is-future", no > n);
    });
    if (out) out.textContent = n + " / " + total;
  }
  if (range) {
    range.addEventListener("input", function () { show(parseInt(range.value, 10) || 0); });
    root.addEventListener("click", function (e) {
      var btn = e.target.closest ? e.target.closest("[data-lp-step]") : null;
      if (!btn) return;
      var n = Math.max(0, Math.min(total, (parseInt(range.value, 10) || 0) + parseInt(btn.getAttribute("data-lp-step"), 10)));
      range.value = n;
      show(n);
    });
  }

  function highlight(no, on) {
    Array.prototype.forEach.call(root.querySelectorAll('[data-no="' + no + '"]'), function (el) {
      el.classList.toggle("is-hl", on);
    });
  }
  ["mouseover", "focusin"].forEach(function (type) {
    root.addEventListener(type, function (e) {
      var el = e.target.closest ? e.target.closest("[data-no]") : null;
      if (el) highlight(el.getAttribute("data-no"), true);
    });
  });
  ["mouseout", "focusout"].forEach(function (type) {
    root.addEventListener(type, function (e) {
      var el = e.target.closest ? e.target.closest("[data-no]") : null;
      if (el) highlight(el.getAttribute("data-no"), false);
    });
  });
})();
