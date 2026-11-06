/* Заявка (06.11.2026, CUSTOMS): митнически помощници в браузъра.
 *  - CN/TARIC код: 8 или 10 цифри; интервалите/точките се махат при излизане
 *    от полето; невалидният се огражда в червено; линк към TARIC (сайт на ЕК).
 *  - EORI / MRN: главни букви без интервали, предупреждение при грешен формат.
 *  - Калкулатор LDM (ЧМР, опаковъчен лист, /customs/ldm) — СЪЩИТЕ формули като
 *    customs.compute_ldm в Python (виж докстринга на customs.py).
 *  - Предложенията за контролни позиции (двойна употреба).
 * Текстовете идват преведени от шаблона (data-msg-* атрибути). */
(function () {
  "use strict";

  // Текстовете идват от <span data-customs-msgs> (_customs.html → customs_msgs).
  var holder = null;
  function msg(name, fallback) {
    if (!holder) holder = document.querySelector("[data-customs-msgs]");
    var v = holder && holder.getAttribute("data-msg-" + name);
    return v || fallback;
  }
  var MATERIALS_LOOKUP_URL = "/materials/lookup";
  function fmt(text, params) {
    return String(text).replace(/\{(\w+)\}/g, function (m, k) {
      return params[k] !== undefined ? params[k] : m;
    });
  }

  var TARIC_URL = "https://ec.europa.eu/taxation_customs/dds2/taric/measures.jsp";
  var CN_SELECTOR = 'input[data-field="hs_code"], input[name="hs_code"], input[name="stat_no"], input[data-cn-field]';

  function normalizeCn(value) { return String(value || "").replace(/[\s.  ]+/g, ""); }
  function cnValid(value) { return /^\d{8}(\d{2})?$/.test(normalizeCn(value)); }
  function taricUrl(value) {
    return cnValid(value) ? TARIC_URL + "?Lang=bg&Taric=" + normalizeCn(value) + "&LangDescr=bg" : "";
  }

  function markCn(input, normalize) {
    var value = input.value;
    if (normalize && cnValid(value) && normalizeCn(value) !== value) {
      input.value = normalizeCn(value);
      value = input.value;
    }
    var bad = !!normalizeCn(value) && !cnValid(value);
    input.classList.toggle("cn-invalid", bad);
    if (bad) {
      input.setAttribute("aria-invalid", "true");
      input.title = msg("cn-invalid", "CN кодът трябва да е 8 или 10 цифри (напр. 85389099).");
    } else {
      input.removeAttribute("aria-invalid");
      if (input.title && input.classList.contains("cn-checked")) input.title = "";
    }
    input.classList.add("cn-checked");
    updateTaric(input);
    return !bad;
  }

  // Линк „TARIC ↗“ до самостоятелно поле; за таблица — един ред под нея.
  function taricLinkFor(input) {
    var table = input.closest("table");
    if (table && input.dataset.field) {
      var hint = table._cnHint;
      if (!hint) {
        hint = document.createElement("p");
        hint.className = "field-hint cn-taric-hint";
        hint.setAttribute("aria-live", "polite");
        var wrap = table.parentNode;
        wrap.insertBefore(hint, table.nextSibling);
        table._cnHint = hint;
      }
      return hint;
    }
    var link = input._taricLink;
    if (!link) {
      link = document.createElement("a");
      link.className = "taric-link";
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = "TARIC ↗";
      link.title = msg("taric", "Отвори в TARIC (сайт на ЕК)");
      input.insertAdjacentElement("afterend", link);
      input._taricLink = link;
    }
    return link;
  }

  function updateTaric(input) {
    var target = taricLinkFor(input);
    if (target.tagName === "A") {
      var url = taricUrl(input.value);
      target.style.display = url ? "" : "none";
      if (url) target.href = url;
      return;
    }
    // Таблица: последният пипнат код + брой невалидни редове.
    var table = input.closest("table");
    var bad = 0;
    Array.prototype.forEach.call(table.querySelectorAll('input[data-field="hs_code"]'), function (el) {
      if (normalizeCn(el.value) && !cnValid(el.value)) bad++;
    });
    target.textContent = "";
    var url2 = taricUrl(input.value);
    if (url2) {
      var a = document.createElement("a");
      a.href = url2;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      a.className = "taric-link";
      a.textContent = "TARIC: " + normalizeCn(input.value) + " ↗";
      a.title = msg("taric", "Отвори в TARIC (сайт на ЕК)");
      target.appendChild(a);
    }
    if (bad) {
      var warn = document.createElement("span");
      warn.className = "cn-taric-warn";
      warn.textContent = fmt(msg("cn-count", "Редове с невалиден CN код (8 или 10 цифри): {count}."), {count: bad});
      target.appendChild(document.createTextNode(url2 ? " · " : ""));
      target.appendChild(warn);
    }
  }

  var EORI_RE = /^[A-Z]{2}[A-Z0-9]{1,15}$/;
  var MRN_RE = /^\d{2}[A-Z]{2}[A-Z0-9]{13}\d$/;
  function normalizeId(value) { return String(value || "").replace(/[\s.\-  ]+/g, "").toUpperCase(); }
  function markId(input, re, text) {
    if (input.value.trim()) input.value = normalizeId(input.value);
    var bad = !!input.value && !re.test(input.value);
    input.classList.toggle("cn-invalid", bad);
    if (bad) { input.setAttribute("aria-invalid", "true"); input.title = text; }
    else { input.removeAttribute("aria-invalid"); input.title = ""; }
  }

  document.addEventListener("focusout", function (e) {
    var el = e.target;
    if (!el || !el.matches) return;
    if (el.matches(CN_SELECTOR)) markCn(el, true);
    else if (el.matches("[data-eori-field]"))
      markId(el, EORI_RE, msg("eori-invalid", "EORI: код на държава + до 15 букви/цифри (напр. BG123456789)."));
    else if (el.matches("[data-mrn-field]"))
      markId(el, MRN_RE, msg("mrn-invalid", "MRN: 18 знака (напр. 26BG…), проверете номера."));
  });
  document.addEventListener("input", function (e) {
    var el = e.target;
    if (el && el.matches && el.matches(CN_SELECTOR) && (el.classList.contains("cn-invalid") || cnValid(el.value)))
      markCn(el, false);
  });
  document.addEventListener("focusin", function (e) {
    var el = e.target;
    if (el && el.matches && el.matches('table input[data-field="hs_code"]')) updateTaric(el);
  });

  // CN код от справочника материали при въведен код на материал (фактурите):
  // попълва се само празен HS code или подразбиращият се (85389099).
  document.addEventListener("change", function (e) {
    var el = e.target;
    if (!el || !el.matches || !el.matches('table.items input[data-field="material_code"]')) return;
    var table = el.closest("table");
    var tr = el.closest("tr");
    var hs = tr && tr.querySelector('input[data-field="hs_code"]');
    var url = table && (table.dataset.lookupUrl || MATERIALS_LOOKUP_URL);
    var code = el.value.trim();
    if (!hs || !url || !code || !window.fetch) return;
    var def = "";
    try { def = (JSON.parse(table.dataset.rowDefaults || "{}") || {}).hs_code || ""; } catch (err) { def = ""; }
    fetch(url + (url.indexOf("?") >= 0 ? "&" : "?") + "code=" + encodeURIComponent(code),
          {credentials: "same-origin", headers: {"Accept": "application/json"}})
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data || !data.cn_code || el.value.trim() !== code) return;
        var cur = hs.value.trim();
        if (cur && cur !== def) return;
        hs.value = data.cn_code;
        markCn(hs, true);
      })
      .catch(function () { /* справката е само удобство */ });
  });

  // ------------------------------------------------------------ LDM
  var TRAILER_LENGTH = 13.6, TRAILER_WIDTH = 2.4, TRAILER_EUR = 33, MAX_STACK = 3;
  var PALLETS = {eur: [1200, 800], ind: [1200, 1000]};
  var NUM_RE = /^\d+([.,]\d+)?$/;

  function num(value) {
    var text = String(value === undefined || value === null ? "" : value).replace(/\s+/g, "");
    if (!text || !NUM_RE.test(text)) return null;
    var n = parseFloat(text.replace(",", "."));
    return isFinite(n) && n >= 0 ? n : null;
  }
  function roundHalfUp(x, places) {
    var f = Math.pow(10, places);
    return Math.round(x * f + 1e-7) / f;
  }

  function ldmCompute(lines, gross, factor) {
    var area = 0, used = 0;
    lines.forEach(function (line) {
      var dims = PALLETS[line.type];
      var L = dims ? dims[0] : num(line.length), W = dims ? dims[1] : num(line.width);
      var qty = num(line.qty);
      if (!L || !W || !qty) return;
      var stack = Math.min(Math.max(Math.floor(num(line.stack) || 1), 1), MAX_STACK);
      var footprints = Math.ceil(qty / stack - 1e-9);
      area += footprints * (L / 1000) * (W / 1000);
      used++;
    });
    var raw = area / TRAILER_WIDTH;
    var ldm = roundHalfUp(raw, 2);
    var places = raw > 0 ? Math.ceil(raw * TRAILER_EUR / TRAILER_LENGTH - 1e-9) : 0;
    var volumetric = Math.round(roundHalfUp(ldm * factor, 0));
    var g = num(gross);
    var chargeable = g === null ? volumetric : Math.max(Math.round(roundHalfUp(g, 0)), volumetric);
    return {lines: used, ldm: ldm, places: places, pct: Math.round(roundHalfUp(raw / TRAILER_LENGTH * 100, 0)),
            over: raw > TRAILER_LENGTH + 1e-9, volumetric: volumetric, chargeable: chargeable};
  }
  window.customsLdmCompute = ldmCompute;

  function fmtLdm(x) { return x.toFixed(2).replace(".", ","); }

  function initLdm(box) {
    var form = box.closest("form") || document;
    var mode = box.dataset.mode || "lines";
    var factorSel = box.querySelector('[name="ldm_factor"]');
    var summary = box.querySelector("[data-ldm-summary]");
    var hidden = box.querySelector('input[name="ldm_lines"]');
    var tbody = box.querySelector(".ldm-lines tbody");
    var tpl = box.querySelector("template[data-ldm-row-template]");
    var outs = {};

    function grossInput() {
      return box.dataset.grossField ? form.querySelector('[name="' + box.dataset.grossField + '"]') : null;
    }

    Array.prototype.forEach.call(box.querySelectorAll("[data-ldm-out]"), function (input) {
      var badge = document.createElement("span");
      badge.className = "badge packing-auto-badge";
      badge.style.display = "none";
      badge.style.marginLeft = "6px";
      badge.textContent = box.dataset.msgAuto || "авто";
      badge.title = box.dataset.msgAutoHint || "";
      var label = box.querySelector('label[for="' + input.id + '"]');
      if (label) label.appendChild(badge);
      var state = {input: input, badge: badge, manual: false};
      outs[input.dataset.ldmOut] = state;
      input.addEventListener("input", function () {
        state.manual = !!input.value.trim();
        if (!state.manual) recompute(); else badge.style.display = "none";
      });
    });

    function readLines() {
      if (mode === "rows") {
        var table = document.getElementById(box.dataset.rowsTable);
        if (!table) return [];
        var stackSel = box.querySelector('[name="ldm_stack"]');
        var perQty = (box.querySelector('[name="ldm_row_mode"]') || {}).value === "qty";
        var stack = stackSel ? stackSel.value : 1;
        var out = [];
        Array.prototype.forEach.call(table.querySelectorAll("tbody tr"), function (tr) {
          function val(f) { var el = tr.querySelector('[data-field="' + f + '"]'); return el ? el.value : ""; }
          out.push({type: "custom", qty: perQty ? val("qty") : 1, length: val("length"),
                    width: val("width"), stack: stack});
        });
        return out;
      }
      var lines = [];
      Array.prototype.forEach.call(tbody.querySelectorAll("tr"), function (tr) {
        function v(f) { var el = tr.querySelector('[data-ldm="' + f + '"]'); return el ? el.value.trim() : ""; }
        lines.push({type: v("type"), qty: v("qty"), length: v("length"), width: v("width"), stack: v("stack")});
      });
      return lines;
    }

    function factor() { return num(factorSel && factorSel.value) || 1850; }

    function recompute() {
      var lines = readLines();
      if (hidden) {
        hidden.value = JSON.stringify(lines.filter(function (l) { return num(l.qty); }));
      }
      var g = grossInput();
      var res = ldmCompute(lines, g ? g.value : "", factor());
      var values = {ldm: res.lines ? fmtLdm(res.ldm) : "", pallet_places: res.lines ? String(res.places) : "",
                    chargeable_weight: res.lines ? String(res.chargeable) : ""};
      Object.keys(outs).forEach(function (key) {
        var st = outs[key];
        if (st.manual) { st.badge.style.display = "none"; return; }
        st.input.value = values[key];
        st.badge.style.display = values[key] ? "" : "none";
      });
      if (summary) {
        if (!res.lines) {
          summary.textContent = box.dataset.msgEmpty || "";
          summary.classList.remove("field-hint--warn");
        } else {
          summary.textContent = fmt(box.dataset.msgSummary || "{pct}", {
            pct: res.pct + " %", vol: String(res.volumetric), factor: String(factor())}) +
            (res.over ? " " + (box.dataset.msgOver || "") : "");
          summary.classList.toggle("field-hint--warn", res.over);
        }
      }
      return res;
    }

    function syncType(tr) {
      var type = tr.querySelector('[data-ldm="type"]').value;
      var dims = PALLETS[type];
      ["length", "width"].forEach(function (f, i) {
        var el = tr.querySelector('[data-ldm="' + f + '"]');
        if (dims) { el.value = String(dims[i]); el.readOnly = true; }
        else { el.readOnly = false; }
      });
    }

    function addLine(data) {
      var tr = tpl.content.firstElementChild.cloneNode(true);
      data = data || {};
      ["type", "qty", "length", "width", "stack"].forEach(function (f) {
        var el = tr.querySelector('[data-ldm="' + f + '"]');
        if (el && data[f] !== undefined && data[f] !== null && data[f] !== "") el.value = String(data[f]);
      });
      var remove = tr.querySelector("[data-ldm-remove]");
      remove.title = box.dataset.msgRemove || "";
      remove.setAttribute("aria-label", box.dataset.msgRemove || "✕");
      remove.addEventListener("click", function () {
        tr.remove();
        if (!tbody.querySelector("tr")) addLine();
        recompute();
      });
      tr.querySelector('[data-ldm="type"]').addEventListener("change", function () { syncType(tr); recompute(); });
      tbody.appendChild(tr);
      syncType(tr);
      return tr;
    }

    if (mode === "lines") {
      var saved = [];
      try { saved = JSON.parse((hidden && hidden.value) || "[]") || []; } catch (e) { saved = []; }
      if (!Array.isArray(saved)) saved = [];
      saved.slice(0, 50).forEach(function (l) { if (l && typeof l === "object") addLine(l); });
      if (!tbody.querySelector("tr")) addLine();
      box.querySelector("[data-ldm-add]").addEventListener("click", function () {
        var tr = addLine();
        var q = tr.querySelector('[data-ldm="qty"]');
        if (q) q.focus();
        recompute();
      });
      tbody.addEventListener("input", recompute);
      tbody.addEventListener("change", recompute);
    } else {
      var table = document.getElementById(box.dataset.rowsTable);
      if (table) {
        table.addEventListener("input", recompute);
        if (window.MutationObserver) {
          new MutationObserver(function () { recompute(); })
            .observe(table.querySelector("tbody"), {childList: true});
        }
      }
      document.addEventListener("items-row-added", recompute);
    }
    Array.prototype.forEach.call(box.querySelectorAll("[data-ldm-opt]"), function (el) {
      el.addEventListener("change", recompute);
    });
    var g = grossInput();
    if (g) g.addEventListener("input", recompute);

    // Запазена (при редакция) стойност, различна от изчислената, е ръчна.
    var pre = {};
    Object.keys(outs).forEach(function (k) { pre[k] = outs[k].input.value.trim(); });
    var first = recompute();
    var computed = {ldm: first.lines ? fmtLdm(first.ldm) : "", pallet_places: first.lines ? String(first.places) : "",
                    chargeable_weight: first.lines ? String(first.chargeable) : ""};
    Object.keys(outs).forEach(function (k) {
      if (pre[k] && pre[k] !== computed[k]) {
        outs[k].manual = true;
        outs[k].input.value = pre[k];
        outs[k].badge.style.display = "none";
      }
    });
    box._ldmRecompute = recompute;
  }

  // ------------------------------------------------------------ контролни позиции
  document.addEventListener("click", function (e) {
    var btn = e.target && e.target.closest ? e.target.closest("[data-cc-code]") : null;
    if (!btn) return;
    var wrap = btn.closest("[data-cc-target]");
    var input = wrap && document.querySelector('[name="' + wrap.dataset.ccTarget + '"]');
    if (!input) return;
    var code = btn.dataset.ccCode;
    var parts = input.value.split(/\s*,\s*/).filter(function (p) { return p; });
    if (parts.indexOf(code) < 0) parts.push(code);
    input.value = parts.join(", ");
    input.dispatchEvent(new Event("input", {bubbles: true}));
  });

  function init() {
    Array.prototype.forEach.call(document.querySelectorAll("[data-ldm-calc]"), initLdm);
    Array.prototype.forEach.call(document.querySelectorAll(CN_SELECTOR), function (el) {
      if (el.value) markCn(el, false);
      else if (!el.dataset.field) updateTaric(el);
    });
  }
  // app.js се закача на DOMContentLoaded по-рано (редовете на таблиците и
  // предзареждането при редакция са готови, когато стигнем дотук).
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
