/* Заявка (06.11.2026, PARTIES): проверка във VIES, избор на превозвач от
   регистъра в ЧМР (+ бърз запис), „последният превозвач за този клиент“.

   Ползва общите функции от app.js (зарежда се преди този файл):
   fetchJsonSafe, markAutofilled, SESSION_EXPIRED_MSG. Текстовете идват от
   data-t-* атрибутите на блоковете (templates/_parties.html) — преведени на
   сървъра, без речника js_i18n.

   Конвенция за автоматично попълване (както в app.js): поле се попълва само
   ако е празно ИЛИ още държи стойността, попълнена автоматично преди това
   (data-autofilled-value); ръчно въведеното никога не се презаписва.

   За група SHIP (шаблони по клиент): window.PHParties.lastForClient(key)
   → Promise с последния превозвач/шофьор/камион за клиента (key = id от
   адресната книга или име), window.PHParties.applyCarrier(rec, opts) —
   попълва полетата 16–18 по същата конвенция. */
(function () {
  "use strict";

  function $(sel, root) { return (root || document).querySelector(sel); }
  function $all(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }
  function fmt(text, values) {
    return String(text || "").replace(/\{(\w+)\}/g, function (m, k) {
      return values && values[k] !== undefined ? values[k] : m;
    });
  }
  function postJson(url, csrf, body) {
    return fetchJsonSafe(url, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrf || "",
                 "X-Requested-With": "fetch" },
      body: JSON.stringify(body)
    });
  }
  function errorText(err, fallback) {
    if (err && err.sessionExpired) return SESSION_EXPIRED_MSG;
    if (err && err.serverMessage) return err.serverMessage;
    return fallback;
  }

  // ------------------------------------------------------------ VIES
  function normVat(v) {
    var s = String(v || "").toUpperCase().replace(/[\s.\-\/_,:;  ]+/g, "");
    if (s.slice(0, 2) === "GR") s = "EL" + s.slice(2);
    return s;
  }

  function renderViesResult(box, r, checkedAt) {
    var out = box.querySelector("[data-vies-result]");
    out.innerHTML = "";
    var badge = document.createElement("span");
    badge.className = "vies-badge " + (r.valid ? "is-valid" : "is-invalid");
    badge.textContent = (r.valid ? "✔ " : "✖ ") + (r.valid ? box.dataset.tValid : box.dataset.tInvalid);
    out.appendChild(badge);
    var meta = document.createElement("span");
    meta.className = "vies-meta";
    meta.textContent = " " + r.vat + " · " + box.dataset.tChecked + " " + (checkedAt || r.checked_at);
    out.appendChild(meta);
    [["b", r.name, "vies-line"], ["span", r.address, "vies-line vies-address"]].forEach(function (p) {
      if (!p[1]) return;
      var div = document.createElement("div");
      div.className = p[2];
      var el = document.createElement(p[0]);
      el.textContent = p[1];
      div.appendChild(el);
      out.appendChild(div);
    });
  }

  function initViesBox(box) {
    var btn = box.querySelector("[data-vies-btn]");
    var note = box.querySelector("[data-vies-note]");
    var vatInput = box.dataset.vatInput ? $(box.dataset.vatInput) : null;
    var countryInput = box.dataset.countryInput ? $(box.dataset.countryInput) : null;
    if (!btn || !vatInput) return;
    // Enter в собственото поле не изпраща формата на записа.
    vatInput.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && vatInput.hasAttribute("data-vies-own-input")) {
        e.preventDefault();
        btn.click();
      }
    });
    function staleCheck() {
      var cached = box.dataset.cachedVat;
      var current = normVat(vatInput.value);
      var stale = cached && current && current !== normVat(cached) &&
        current !== normVat(cached).slice(2);
      box.classList.toggle("is-stale", !!stale);
      if (stale) note.textContent = box.dataset.tOther;
      else if (note.dataset.kind === "stale") note.textContent = "";
      note.dataset.kind = stale ? "stale" : "";
    }
    vatInput.addEventListener("input", staleCheck);
    staleCheck();
    btn.addEventListener("click", function () {
      var vat = vatInput.value.trim();
      note.dataset.kind = "";
      if (!vat) { note.textContent = box.dataset.tEmpty; vatInput.focus(); return; }
      btn.disabled = true;
      btn.classList.add("btn-busy");
      note.textContent = box.dataset.tChecking;
      postJson(box.dataset.url, box.dataset.csrf, {
        vat: vat, country: countryInput ? countryInput.value : "",
        target: box.dataset.target, target_id: box.dataset.targetId || ""
      }).then(function (data) {
        if (!data || !data.ok) {
          note.textContent = (data && data.error) || box.dataset.tFailed;
          box.classList.add("is-error");
          return;
        }
        box.classList.remove("is-error", "is-stale");
        renderViesResult(box, data.result, data.checked_at_display);
        box.dataset.cachedVat = data.result.vat;
        // Без запис (нов клиент) резултатът не се пази — казваме го.
        note.textContent = data.saved ? "" : box.dataset.tNotSaved;
      }).catch(function (err) {
        note.textContent = errorText(err, box.dataset.tFailed);
        box.classList.add("is-error");
      }).then(function () {
        btn.disabled = false;
        btn.classList.remove("btn-busy");
      });
    });
  }

  // ------------------------------------------------------------ превозвач в ЧМР
  var picker = null;      // блокът [data-carrier-picker]
  var carriers = [];      // вградените + намерените превозвачи

  function carrierBoxText(c) {
    // Същото сглобяване като carriers.carrier_text (сървъра).
    var place = [c.postcode, c.city].filter(Boolean).join(" ");
    var text = [c.name, c.address, place, c.country].filter(function (x) {
      return x && String(x).trim();
    }).join(", ");
    if (c.vat) text += ", VAT: " + c.vat;
    return text;
  }
  function field(name) { return document.querySelector('#main-doc-form [name="' + name + '"]'); }

  /* Попълва полето, ако е празно или още държи автоматично попълнено.
     Връща true, ако е попълнено. force=true — изрично „Попълни“ от оператора. */
  function setAuto(el, value, force) {
    if (!el) return false;
    value = value || "";
    var current = el.value.trim();
    var auto = el.dataset.autofilledValue;
    if (current && !force && (auto === undefined || current !== auto)) return false;
    el.value = value;
    if (value) { el.dataset.autofilledValue = value.trim(); markAutofilled(el); }
    else delete el.dataset.autofilledValue;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    return true;
  }

  function setDatalist(input, id, values) {
    if (!input) return;
    var dl = document.getElementById(id);
    if (!dl) {
      dl = document.createElement("datalist");
      dl.id = id;
      document.body.appendChild(dl);
    }
    dl.innerHTML = "";
    values.forEach(function (v) {
      if (!v) return;
      var opt = document.createElement("option");
      opt.value = v;
      dl.appendChild(opt);
    });
    if (values.length) input.setAttribute("list", id); else input.removeAttribute("list");
  }

  function mergeCarrier(c) {
    if (!c) return;
    for (var i = 0; i < carriers.length; i++) {
      if (carriers[i].id === c.id) { carriers[i] = c; return; }
    }
    carriers.push(c);
  }
  function findCarrier(id) {
    id = parseInt(id, 10);
    for (var i = 0; i < carriers.length; i++) if (carriers[i].id === id) return carriers[i];
    return null;
  }
  function ensureOption(select, c) {
    if (!select || !c) return;
    var found = $all("option", select).some(function (o) { return o.value === String(c.id); });
    if (!found) {
      var opt = document.createElement("option");
      opt.value = c.id;
      opt.textContent = c.name + (c.city || c.country ? " — " + [c.city, c.country].filter(Boolean).join(", ") : "");
      select.appendChild(opt);
    }
  }

  /* Попълва 16–18 от запис на превозвач (и по избор конкретни стойности от
     последното ЧМР — opts.values). opts.force — изричен избор на оператора. */
  function applyCarrier(c, opts) {
    opts = opts || {};
    var values = opts.values || {};
    var select = picker ? picker.querySelector("[data-carrier-select]") : null;
    var hidden = field("carrier_id");
    if (c) {
      mergeCarrier(c);
      ensureOption(select, c);
      if (select) select.value = String(c.id);
      if (hidden) hidden.value = String(c.id);
    } else if (hidden && opts.clearId) {
      hidden.value = "";
      if (select) select.value = "";
    }
    var trucks = c ? (c.trucks || []).map(function (v) { return v.plate; }) : [];
    var trailers = c ? (c.trailers || []).map(function (v) { return v.plate; }) : [];
    var drivers = c ? (c.drivers || []).map(function (d) { return d.name; }) : [];
    setDatalist(field("truck_reg"), "dl-carrier-trucks", trucks);
    setDatalist(field("trailer_reg"), "dl-carrier-trailers", trailers);
    setDatalist(field("driver"), "dl-carrier-drivers", drivers);
    var filled = 0;
    var text = values.carrier || (c ? carrierBoxText(c) : "");
    if (setAuto(field("carrier"), text, opts.force)) filled++;
    var pick = function (key, list) {
      if (values[key]) return values[key];
      return list.length === 1 ? list[0] : "";
    };
    if (setAuto(field("truck_reg"), pick("truck_reg", trucks), opts.force)) filled++;
    if (setAuto(field("trailer_reg"), pick("trailer_reg", trailers), opts.force)) filled++;
    if (setAuto(field("driver"), pick("driver", drivers), opts.force)) filled++;
    return filled;
  }

  function memoryBox() { return picker ? picker.querySelector("[data-carrier-memory]") : null; }
  function showMemory(text, applyFn) {
    var box = memoryBox();
    if (!box) return;
    box.innerHTML = "";
    if (!text) { box.hidden = true; return; }
    var span = document.createElement("span");
    span.textContent = text;
    box.appendChild(span);
    if (applyFn) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "btn btn-secondary btn-small";
      b.textContent = picker.dataset.tLastApply;
      b.addEventListener("click", function () { applyFn(); box.hidden = true; });
      box.appendChild(document.createTextNode(" "));
      box.appendChild(b);
    }
    box.hidden = false;
  }

  function lastForClient(key) {
    if (!picker || !key) return Promise.resolve(null);
    var param = /^\d+$/.test(String(key)) ? "client_id=" : "name=";
    return fetchJsonSafe(picker.dataset.lastUrl + "?" + param + encodeURIComponent(key))
      .then(function (data) { return data && data.found ? data.last : null; });
  }

  /* Клиентът е избран от адресната книга → предлага превозвача от последното
     ЧМР за него. Попълва само празни/автоматични полета; ако има ръчно
     въведени — показва предложение с бутон „Попълни“. */
  var memorySeq = 0;
  function offerLastForClient(key) {
    var mine = ++memorySeq;
    if (!key) { showMemory(""); return Promise.resolve(null); }
    return lastForClient(key).then(function (last) {
      if (mine !== memorySeq) return null;
      if (!last) { showMemory(""); return null; }
      var values = { carrier: last.carrier, truck_reg: last.truck_reg,
                     trailer_reg: last.trailer_reg, driver: last.driver };
      var manual = ["carrier", "truck_reg", "trailer_reg", "driver"].some(function (k) {
        var el = field(k);
        if (!el || !el.value.trim() || !values[k]) return false;
        var auto = el.dataset.autofilledValue;
        return (auto === undefined || el.value.trim() !== auto) && el.value.trim() !== values[k];
      });
      var dateText = last.doc_date ? last.doc_date.split("-").reverse().join(".") : "";
      if (!manual) {
        applyCarrier(last.carrier_record, { values: values, clearId: !last.carrier_record });
        showMemory(fmt(picker.dataset.tLast, { number: last.doc_number, date: dateText }));
      } else {
        var vehicle = [last.truck_reg, last.trailer_reg, last.driver].filter(Boolean).join(" / ");
        showMemory(fmt(picker.dataset.tLastOffer, {
          carrier: (last.carrier || "").split(",")[0], vehicle: vehicle ? " — " + vehicle : "",
          number: last.doc_number
        }), function () {
          applyCarrier(last.carrier_record, { values: values, force: true, clearId: !last.carrier_record });
        });
      }
      return last;
    }).catch(function () { return null; });
  }

  function initCarrierSearch(select) {
    var total = parseInt(picker.dataset.total, 10) || 0;
    if (total <= 12) return;
    var embedded = carriers.slice();
    var input = document.createElement("input");
    input.type = "search";
    input.autocomplete = "off";
    input.className = "carrier-search";
    input.placeholder = picker.dataset.tSearch;
    input.setAttribute("aria-label", input.placeholder);
    input.addEventListener("keydown", function (e) { if (e.key === "Enter") e.preventDefault(); });
    select.parentNode.parentNode.insertBefore(input, select.parentNode);
    var placeholder = select.options[0];
    var timer = null, seq = 0;
    function render(list) {
      var keep = select.value;
      select.innerHTML = "";
      select.appendChild(placeholder);
      list.forEach(function (c) { ensureOption(select, c); });
      select.value = keep;
      if (!select.value) select.value = "";
    }
    input.addEventListener("input", function () {
      var q = input.value.trim().toLowerCase();
      if (timer) clearTimeout(timer);
      if (!q) { seq++; render(embedded); return; }
      if (total <= embedded.length) {
        render(embedded.filter(function (c) {
          var hay = [c.name, c.vat, c.city, c.country].concat(
            (c.trucks || []).concat(c.trailers || []).map(function (v) { return v.plate; }),
            (c.drivers || []).map(function (d) { return d.name; })).join(" ").toLowerCase();
          return hay.indexOf(q) !== -1;
        }));
        return;
      }
      timer = setTimeout(function () {
        var mine = ++seq;
        fetchJsonSafe(picker.dataset.lookupUrl + "?q=" + encodeURIComponent(input.value.trim()))
          .then(function (data) {
            if (mine !== seq) return;
            (data.carriers || []).forEach(mergeCarrier);
            render(data.carriers || []);
          }).catch(function () {});
      }, 300);
    });
  }

  function initQuickAdd() {
    var panel = picker.querySelector("[data-carrier-quick]");
    var openBtn = picker.querySelector("[data-carrier-quick-open]");
    var status = picker.querySelector("[data-carrier-quick-status]");
    if (!panel || !openBtn) return;
    function inputs() { return $all("[data-cq]", panel); }
    function toggle(show) {
      panel.hidden = !show;
      openBtn.setAttribute("aria-expanded", show ? "true" : "false");
      if (show) {
        // Името — от кутия 16 (до първата запетая), ако още няма избран превозвач.
        var name = panel.querySelector('[data-cq="name"]');
        var box = field("carrier");
        if (name && !name.value && box && box.value && !field("carrier_id").value) {
          name.value = box.value.split(",")[0].trim();
        }
        if (name) name.focus();
      }
    }
    openBtn.addEventListener("click", function () { toggle(panel.hidden); });
    picker.querySelector("[data-carrier-quick-cancel]").addEventListener("click", function () {
      toggle(false);
    });
    inputs().forEach(function (el) {
      el.addEventListener("keydown", function (e) { if (e.key === "Enter") e.preventDefault(); });
    });
    picker.querySelector("[data-carrier-quick-save]").addEventListener("click", function () {
      var body = {};
      inputs().forEach(function (el) { body[el.dataset.cq] = el.value.trim(); });
      if (!body.name) { status.textContent = picker.dataset.tQuickName; return; }
      ["truck_reg", "trailer_reg", "driver"].forEach(function (k) {
        var el = field(k);
        body[k] = el ? el.value.trim() : "";
      });
      status.textContent = "…";
      postJson(picker.dataset.quickUrl, picker.dataset.csrf, body).then(function (data) {
        if (!data || !data.ok) { status.textContent = (data && data.error) || picker.dataset.tFailed; return; }
        // Кутия 16 се попълва от записа, ако е празна/автоматична; ръчният текст остава.
        applyCarrier(data.carrier, {});
        status.textContent = data.message || picker.dataset.tQuickSaved;
        inputs().forEach(function (el) { el.value = ""; });
        setTimeout(function () { toggle(false); status.textContent = ""; }, 1800);
      }).catch(function (err) {
        status.textContent = errorText(err, picker.dataset.tFailed);
      });
    });
  }

  function initCarrierPicker() {
    picker = $("[data-carrier-picker]");
    if (!picker) return;
    try { carriers = JSON.parse(picker.dataset.carriers || "[]"); } catch (e) { carriers = []; }
    var select = picker.querySelector("[data-carrier-select]");
    var hidden = field("carrier_id");
    // Редакция/връщане от прегледа: prefillForm (app.js) вече е сложил carrier_id.
    if (hidden && hidden.value) {
      var c = findCarrier(hidden.value);
      if (c) {
        select.value = String(c.id);
        setDatalist(field("truck_reg"), "dl-carrier-trucks", (c.trucks || []).map(function (v) { return v.plate; }));
        setDatalist(field("trailer_reg"), "dl-carrier-trailers", (c.trailers || []).map(function (v) { return v.plate; }));
        setDatalist(field("driver"), "dl-carrier-drivers", (c.drivers || []).map(function (d) { return d.name; }));
      }
    }
    select.addEventListener("change", function () {
      var c = findCarrier(select.value);
      showMemory("");
      if (!c) {
        if (hidden) hidden.value = "";
        applyCarrier(null, {});
        return;
      }
      applyCarrier(c, {});
    });
    // Ръчна промяна на кутия 16 след избор — изборът от регистъра вече не важи
    // (палетната сметка/поръчката не бива да сочат друга фирма).
    var box = field("carrier");
    if (box) {
      box.addEventListener("input", function (e) {
        if (!e.isTrusted || !hidden || !hidden.value) return;
        var c = findCarrier(hidden.value);
        var first = (box.value.split(",")[0] || "").trim().toLowerCase();
        if (c && first !== (c.name || "").trim().toLowerCase()) {
          hidden.value = "";
          select.value = "";
        }
      });
    }
    initCarrierSearch(select);
    initQuickAdd();
    // Клиентът (получател) от адресната книга → последният превозвач за него.
    var clientSelect = document.querySelector('#main-doc-form select.client-select[data-target="consignee"]');
    if (clientSelect) {
      clientSelect.addEventListener("change", function () { offerLastForClient(clientSelect.value); });
    }
  }

  window.PHParties = {
    lastForClient: lastForClient,
    offerLastForClient: offerLastForClient,
    applyCarrier: applyCarrier,
    carrierBoxText: carrierBoxText
  };

  document.addEventListener("DOMContentLoaded", function () {
    $all("[data-vies]").forEach(initViesBox);
    initCarrierPicker();
  });
})();
