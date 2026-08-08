// ПачоЛогистик — общи скриптове за формите

// Автоматично попълване от адресната книга.
// Селект с class="client-select" и data-target="префикс" попълва полетата
// с имена: префикс_name, префикс_address, ... от window.CLIENTS.
function bindClientSelect(select) {
  select.addEventListener("change", function () {
    var id = parseInt(select.value, 10);
    var client = (window.CLIENTS || []).find(function (c) { return c.id === id; });
    if (!client) return;
    var p = select.dataset.target;
    var map = {
      name: client.name,
      address: client.address,
      city: [client.postcode, client.city].filter(Boolean).join(" "),
      country: client.country,
      eik: client.eik,
      vat: client.vat,
      phone: client.phone,
      contact: client.contact
    };
    Object.keys(map).forEach(function (k) {
      var el = document.querySelector('[name="' + p + '_' + k + '"]');
      if (el && map[k] !== undefined) el.value = map[k] || "";
    });
    // По избор: селект с data-autofill-country="поле" попълва И друго,
    // отделно поле (различно от {target}_country) само с държавата на
    // избрания клиент — напр. декларацията за двойна употреба показва
    // само поле "Държава на износ" (destination_country), без пълен блок
    // с адресни полета за получателя.
    if (select.dataset.autofillCountry) {
      var target = document.querySelector('[name="' + select.dataset.autofillCountry + '"]');
      if (target) target.value = client.country || "";
    }
  });
}

// Предварително попълва форма за издаване с данните на вече издаден
// документ (за редакция) — задава стойност на всеки елемент по неговия
// name атрибут от подадения обект. Редовете на динамичните таблици с
// артикули (items) се пълнят отделно, чрез initItemsTable(..., items) —
// затова "items"/"items_format" тук се пропускат нарочно.
function prefillForm(form, data) {
  if (!form || !data) return;
  Object.keys(data).forEach(function (key) {
    if (key === "items" || key === "items_format") return;
    var val = data[key];
    if (val === null || val === undefined) return;
    var els = form.querySelectorAll('[name="' + key + '"]');
    Array.prototype.forEach.call(els, function (el) {
      if (el.type === "checkbox") {
        el.checked = !!val;
      } else if (el.type === "radio") {
        el.checked = (el.value === String(val));
      } else {
        el.value = val;
      }
    });
  });
}

// Динамична таблица с артикули: добавяне/махане на редове и сериализация
// към скрито поле items_json при изпращане на формата.
function initItemsTable(table, columns, initialItems, hiddenFieldName) {
  hiddenFieldName = hiddenFieldName || "items_json";
  var tbody = table.querySelector("tbody");

  // Стойности по подразбиране за НОВ/празен ред — от data-row-defaults
  // (JSON) на самата таблица. Ползва се от фактурите: заявка „в фактурите
  // по подразбиране винаги да се поставя автоматично HS code 85389099“ —
  // и началният празен ред, и „+ Добави ред“ идват с попълнен HS code.
  // Ред с вече зададена стойност (зареден от палетна карта/Excel/редакция)
  // я запазва — подразбирането се прилага само върху празно поле.
  var rowDefaults = {};
  if (table.dataset.rowDefaults) {
    try { rowDefaults = JSON.parse(table.dataset.rowDefaults) || {}; } catch (e) { rowDefaults = {}; }
  }

  function addRow(item) {
    item = item || {};
    var tr = document.createElement("tr");
    var idxTd = document.createElement("td");
    idxTd.className = "row-idx";
    tr.appendChild(idxTd);
    columns.forEach(function (col) {
      var td = document.createElement("td");
      var input = document.createElement("input");
      input.type = "text";
      input.dataset.field = col;
      input.value = item[col] || rowDefaults[col] || "";
      td.appendChild(input);
      tr.appendChild(td);
    });
    var delTd = document.createElement("td");
    var delBtn = document.createElement("button");
    delBtn.type = "button";
    delBtn.textContent = "✕";
    delBtn.className = "btn-danger btn-small";
    delBtn.title = "Премахни реда";
    delBtn.addEventListener("click", function () {
      tr.remove();
      renumber();
    });
    delTd.appendChild(delBtn);
    tr.appendChild(delTd);
    tbody.appendChild(tr);
    renumber();
  }

  function renumber() {
    Array.prototype.forEach.call(tbody.querySelectorAll(".row-idx"), function (td, i) {
      td.textContent = i + 1;
    });
  }

  function collect() {
    var items = [];
    Array.prototype.forEach.call(tbody.querySelectorAll("tr"), function (tr) {
      var item = {};
      var empty = true;
      Array.prototype.forEach.call(tr.querySelectorAll("input[data-field]"), function (inp) {
        item[inp.dataset.field] = inp.value.trim();
        if (inp.value.trim()) empty = false;
      });
      if (!empty) items.push(item);
    });
    return items;
  }

  (initialItems || []).forEach(addRow);
  if (!initialItems || !initialItems.length) addRow();

  var addBtn = document.querySelector('[data-add-row="' + table.id + '"]');
  if (addBtn) addBtn.addEventListener("click", function () { addRow(); });

  var form = table.closest("form");
  if (form) {
    form.addEventListener("submit", function () {
      // Чете АКТУАЛНОТО име на скритото поле от table.dataset.hiddenField
      // (не константата, прихваната при извикването на initItemsTable) —
      // при палетната карта то може да се преномерира по-късно (напр.
      // "items_json" → "items_json_2"), след като потребителят добави
      // ощ карта чрез "+ Добави следваща палетна карта" (виж
      // initPalletMultiCard) — самото изпращане на формата става МНОГО
      // по-късно, след като преномерирането вече е приключило.
      //
      // Бъг (поправен): резервната стойност беше твърдо закодираното
      // "items_json" — вярно САМО за initPalletMultiCard композитора,
      // който винаги го записва в table.dataset.hiddenField. Таблиците в
      // pallet_bulk_review.html/pallet_bulk_preview.html обаче се
      // инициализират директно с други имена ("items_json_1",
      // "items_json_2", ...) и НИКОГА не задават table.dataset.hiddenField
      // — резервната стойност трябваше да сочи към РЕАЛНОТО поле с това
      // име, но сочеше към несъществуващо "items_json", form.querySelector
      // не намираше нищо и заредените/въведените редове изобщо не се
      // изпращаха към сървъра. Резултат: „Предварителен преглед“ (и
      // самото издаване) на импортирани от Excel палетни карти показваше
      // „Няма палетни карти за преглед“ — данните изглеждаха изтрити,
      // макар да си стояха видими в таблицата на екрана. Резервната
      // стойност сега е ПАРАМЕТЪРЪТ, подаден при самото извикване на
      // initItemsTable (винаги коректен за всички страници), не низов
      // литерал.
      var currentHiddenName = table.dataset.hiddenField || hiddenFieldName;
      var hidden = form.querySelector('input[name="' + currentHiddenName + '"]');
      if (hidden) hidden.value = JSON.stringify(collect());
    });
  }
  return { collect: collect, addRow: addRow };
}

// ---------------------------------------------------------------- палетна
// карта: „Общ брой“ (жива сума на количеството от таблицата на картата,
// заменя старото ръчно въвеждано „Нето, кг“ — виж appcore.pallet_total_qty
// за СЪЩАТА сметка на сървъра, ползвана при печат/Excel износ).
function sumQtyForDisplay(items) {
  var total = 0, any = false;
  (items || []).forEach(function (it) {
    var raw = it && it.qty;
    if (raw === undefined || raw === null || raw === "") return;
    var n = parseFloat(String(raw).replace(",", "."));
    if (!isNaN(n)) { total += n; any = true; }
  });
  if (!any) return "—";
  var rounded = Math.round(total * 1000) / 1000;
  return String(rounded);
}

function bindPalletQtyTotal(block, tableApi) {
  var out = block.querySelector(".pallet-total-qty");
  var table = block.querySelector("table.items");
  if (!out || !table || !tableApi) return;
  function update() { out.textContent = sumQtyForDisplay(tableApi.collect()); }
  table.addEventListener("input", update);
  table.addEventListener("click", function () { setTimeout(update, 0); });
  update();
}

// „Тип палет“ → „Друг“: истински модален прозорец за ръчно въвеждане на
// размери (Дължина × Ширина), вместо свободен текст в самата форма —
// потвърденото от модала се добавя като нова опция в списъка и се избира
// автоматично; при отказ селектът се връща на предишната си стойност.
function openPalletTypeModal(callback) {
  var modal = document.getElementById("pallet-type-modal");
  if (!modal) { callback(null); return; }
  var lengthInput = document.getElementById("pallet-type-modal-length");
  var widthInput = document.getElementById("pallet-type-modal-width");
  var confirmBtn = document.getElementById("pallet-type-modal-confirm");
  var cancelBtn = document.getElementById("pallet-type-modal-cancel");
  var closeBtn = document.getElementById("pallet-type-modal-close");
  lengthInput.value = "";
  widthInput.value = "";
  modal.style.display = "flex";
  lengthInput.focus();

  function finish(result) {
    modal.style.display = "none";
    confirmBtn.removeEventListener("click", onConfirm);
    cancelBtn.removeEventListener("click", onCancel);
    closeBtn.removeEventListener("click", onCancel);
    callback(result);
  }
  function onConfirm() {
    var l = lengthInput.value.trim();
    var w = widthInput.value.trim();
    if (!l || !w) { (l ? widthInput : lengthInput).focus(); return; }
    finish(l + "×" + w);
  }
  function onCancel() { finish(null); }
  confirmBtn.addEventListener("click", onConfirm);
  cancelBtn.addEventListener("click", onCancel);
  closeBtn.addEventListener("click", onCancel);
}

function injectAndSelectPalletType(select, value) {
  if (!select || !value) return;
  var exists = Array.prototype.some.call(select.options, function (o) { return o.value === value; });
  if (!exists) {
    var other = select.querySelector('option[value="__other__"]');
    var opt = document.createElement("option");
    opt.value = value;
    opt.textContent = value;
    if (other) select.insertBefore(opt, other); else select.appendChild(opt);
  }
  select.value = value;
}

function initPalletTypeSelect(select) {
  if (!select || select.dataset.otherBound) return;
  select.dataset.otherBound = "1";
  var prevValue = select.value;
  select.addEventListener("change", function () {
    if (select.value === "__other__") {
      openPalletTypeModal(function (dims) {
        if (dims) { injectAndSelectPalletType(select, dims); prevValue = select.value; }
        else { select.value = prevValue; }
      });
    } else {
      prevValue = select.value;
    }
  });
}

// „+ Добави следваща палетна карта“ — композиране на НЯКОЛКО палетни
// карти в ЕДНА сесия, преди общо издаване. По подразбиране (само 1 карта)
// формата се държи ТОЧНО както досега — праща се направо към
// pallet_new/pallet_preview, с непроменени (несуфиксирани) имена на
// полетата. Едва при добавяне на втора карта полетата се преномерират
// (напр. name="pallet_type" → "pallet_type_1"/"pallet_type_2"...) и
// формата минава през pallet_bulk_issue/pallet_bulk_preview — точно
// същата машина, която вече издава палетни карти от импортирана справка
// за поръчки (виж _collect_bulk_pallet_drafts в routes_pallet_extra.py).
// При връщане обратно до 1 карта всичко се връща в изходно състояние —
// нарочно, за да не се променя поведението на обичайния единичен случай.
function initPalletMultiCard(form, itemsTables) {
  var root = document.getElementById("pallet-cards");
  if (!root || !form) return;

  // Тия две (общ брой + "Друг" размери) важат ВИНАГИ за картата, дори при
  // редакция на вече издадена палетна карта (когато „+ Добави следваща
  // карта“ изобщо не се показва) — само машината за композиране на
  // НЯКОЛКО карти по-долу изисква бутона/<template>-а да съществуват.
  Array.prototype.forEach.call(root.querySelectorAll(".pallet-card"), function (block) {
    initPalletTypeSelect(block.querySelector("select.pallet-type-select"));
    var table = block.querySelector("table.items");
    if (table && itemsTables[table.id]) bindPalletQtyTotal(block, itemsTables[table.id]);
  });

  var addBtn = document.getElementById("pallet-add-card-btn");
  var template = document.getElementById("pallet-card-template");
  if (!addBtn || !template) return;  // редакция на съществуваща карта — без композитор

  var singleAction = form.dataset.palletNewAction;
  var singlePreviewAction = form.dataset.palletPreviewAction;
  var bulkAction = form.dataset.palletBulkIssueAction;
  var bulkPreviewAction = form.dataset.palletBulkPreviewAction;
  var previewBtn = singlePreviewAction && form.querySelector('button[formaction="' + singlePreviewAction + '"]');
  var groupsInput = null;

  function cardBlocks() {
    return Array.prototype.slice.call(root.querySelectorAll(".pallet-card"));
  }

  function suffixBlock(block, n, multi) {
    // ВАЖНО: суфиксът зависи от ОБЩИЯ брой карти (multi), не само от
    // позицията n — при 2+ карти дори ПЪРВАТА карта трябва да получи
    // суфикс "_1" (иначе _collect_bulk_pallet_drafts, което търси
    // "items_json_1" за група "1", не намира нищо и цялата първа карта
    // тихо отпада от издаването).
    block.dataset.card = n;
    Array.prototype.forEach.call(block.querySelectorAll("[data-field]"), function (el) {
      el.name = multi ? (el.dataset.field + "_" + n) : el.dataset.field;
    });
    var table = block.querySelector("table.items");
    if (table) {
      if (!table.dataset.baseId) table.dataset.baseId = table.id || "pallet-items";
      var baseId = table.dataset.baseId;
      var newId = multi ? (baseId + "-" + n) : baseId;
      var addRowBtn = block.querySelector("[data-add-row]");
      table.id = newId;
      table.dataset.hiddenField = multi ? ("items_json_" + n) : "items_json";
      if (addRowBtn) addRowBtn.setAttribute("data-add-row", newId);
    }
  }

  function renumber() {
    var blocks = cardBlocks();
    var multi = blocks.length > 1;
    blocks.forEach(function (block, i) { suffixBlock(block, i + 1, multi); });
    blocks.forEach(function (block, i) {
      var title = block.querySelector(".pallet-card-title");
      if (title) title.textContent = multi ? ("Карта " + (i + 1) + " / Card " + (i + 1) + " — ") : "";
      var removeBtn = block.querySelector(".pallet-card-remove");
      if (removeBtn) removeBtn.style.display = multi ? "" : "none";
      var noInput = block.querySelector('[data-field="pallet_no"]');
      if (noInput) {
        if (multi) {
          noInput.readOnly = true;
          noInput.value = (i + 1) + " от " + blocks.length;
        } else {
          noInput.readOnly = false;
          if (/^\d+ от \d+$/.test(noInput.value)) noInput.value = "";
        }
      }
    });
    if (multi) {
      if (!groupsInput) {
        groupsInput = document.createElement("input");
        groupsInput.type = "hidden";
        groupsInput.name = "groups";
        form.appendChild(groupsInput);
      }
      groupsInput.value = blocks.map(function (_b, i) { return i + 1; }).join(",");
      if (bulkAction) form.action = bulkAction;
      if (previewBtn && bulkPreviewAction) previewBtn.setAttribute("formaction", bulkPreviewAction);
    } else {
      if (groupsInput) { groupsInput.remove(); groupsInput = null; }
      if (singleAction) form.action = singleAction;
      if (previewBtn && singlePreviewAction) previewBtn.setAttribute("formaction", singlePreviewAction);
    }
  }

  function wireRemove(block) {
    var removeBtn = block.querySelector(".pallet-card-remove");
    if (!removeBtn) return;
    removeBtn.addEventListener("click", function () {
      var table = block.querySelector("table.items");
      if (table) delete itemsTables[table.id];
      block.remove();
      renumber();
    });
  }

  function addCard() {
    var frag = template.content.cloneNode(true);
    root.appendChild(frag);
    var block = root.lastElementChild;
    wireRemove(block);
    initPalletTypeSelect(block.querySelector("select.pallet-type-select"));
    renumber();
    var table = block.querySelector("table.items");
    if (table) {
      var columns = table.dataset.columns.split(",");
      itemsTables[table.id] = initItemsTable(table, columns, [], table.dataset.hiddenField);
      bindPalletQtyTotal(block, itemsTables[table.id]);
    }
  }

  cardBlocks().forEach(wireRemove);

  addBtn.addEventListener("click", addCard);
  renumber();
}

// ---------------------------------------------------------------- форми за
// издаване/редакция на документ (ЧМР, опаковъчен лист, палетна карта,
// декларациите) — общата инициализация (window.CLIENTS, таблици с
// артикули, предварително попълване при редакция), задвижена от data-*
// атрибути на #main-doc-form, вместо всеки шаблон да я дублира в
// собствен вграден <script> блок. Специфичната за отделните документи
// бизнес логика (ЧМР: избор на пункт за товарене/разтоварване от
// адресната книга; опаковъчен лист: добавяне на ред от палетна карта)
// си остава отделна функция по-долу — извиква се условно, само ако
// съответните ѝ HTML елементи присъстват на страницата.
function initDocumentForm() {
  var form = document.getElementById("main-doc-form");
  if (!form) return;

  if (form.dataset.clients) {
    try { window.CLIENTS = JSON.parse(form.dataset.clients); } catch (e) { window.CLIENTS = []; }
  }

  var itemsTables = {};
  Array.prototype.forEach.call(
    form.querySelectorAll("table.items[data-columns]"),
    function (table) {
      var columns = table.dataset.columns.split(",");
      var initial = [];
      if (table.dataset.items) {
        try { initial = JSON.parse(table.dataset.items); } catch (e) { initial = []; }
      }
      itemsTables[table.id] = initItemsTable(table, columns, initial, table.dataset.hiddenField);
    }
  );

  initCmrPlaces();
  initPullFromPallet(itemsTables["packing-items"]);
  initPalletMultiCard(form, itemsTables);
  initInvoiceForm(form, itemsTables);

  if (form.dataset.edit) {
    var editData = null;
    try { editData = JSON.parse(form.dataset.edit); } catch (e) {}
    if (editData) {
      // "Тип палет" може да пази стойност от модала за "Друг" (напр.
      // "150×100"), която НЕ е сред статичните <option>-и — трябва да се
      // добави ръчно, иначе select.value=... по-долу тихо не избира нищо.
      var ptSelect = form.querySelector('select[name="pallet_type"]');
      if (ptSelect && editData.pallet_type) injectAndSelectPalletType(ptSelect, editData.pallet_type);
      prefillForm(form, editData);
    }
  }
}

// ЧМР (cmr_form.html): 4. Товарен пункт — бутон „Зареди от изпращача“
// копира текущите стойности на поле 1 „Изпращач“ (фирма/адрес/град/
// държава) в текстовото поле за мястото на натоварване (place_loading).
// Заявка: „товарен пункт да се зарежда от фирма изпращач, но да има
// опция и ръчно въвеждане“ — САМО при изрично натискане на бутона (не
// автоматично при всяка промяна на изпращача, за да не изтрива тихомълком
// вече ръчно въведен различен адрес за товарене), а самото поле си остава
// обикновен текстови вход и може да се пише в него свободно по всяко
// време — преди, след или вместо бутона.
//
// По-рано (v3.39.0) тук имаше падащо меню за избор на ПРОИЗВОЛНА фирма от
// адресната книга, което променяше и мястото на товарене, и самия
// изпращач — премахнато по изрична заявка (link отпадна и в
// templates/cmr_form.html), защото посоката вече е обратна: изпращачът е
// изходната точка, не товарният пункт.
//
// 3. Разтоварен пункт — списъкът зависи от избрания клиент получател
// (поле 2): всеки клиент може да има неограничен брой запаметени пунктове
// за разтоварване (адресна книга → редакция на клиент). Задейства се само
// ако страницата има тези елементи (само cmr_form.html ги съдържа).
function initCmrPlaces() {
  var loadFromSenderBtn = document.getElementById("load-place-from-sender-btn");
  var placeLoading = document.getElementById("place_loading");
  var consigneeSelect = document.querySelector('select.client-select[data-target="consignee"]');
  var unloadSelect = document.getElementById("unload-point-select");
  var placeDelivery = document.getElementById("place_delivery");
  if (!loadFromSenderBtn && !unloadSelect) return;

  function fmtAddress(o) {
    return [o.address, [o.postcode, o.city].filter(Boolean).join(" "), o.country]
      .filter(Boolean).join(", ");
  }

  if (loadFromSenderBtn && placeLoading) {
    loadFromSenderBtn.addEventListener("click", function () {
      var senderName = document.querySelector('[name="sender_name"]');
      var senderAddress = document.querySelector('[name="sender_address"]');
      var senderCity = document.querySelector('[name="sender_city"]');
      var senderCountry = document.querySelector('[name="sender_country"]');
      var addr = [senderAddress && senderAddress.value, senderCity && senderCity.value,
                  senderCountry && senderCountry.value].filter(Boolean).join(", ");
      placeLoading.value = [senderName && senderName.value, addr].filter(Boolean).join(" — ");
    });
  }

  if (consigneeSelect && unloadSelect && placeDelivery) {
    function refreshUnloadPoints() {
      var id = parseInt(consigneeSelect.value, 10);
      var client = (window.CLIENTS || []).find(function (c) { return c.id === id; });
      unloadSelect.innerHTML = "";
      var placeholder = document.createElement("option");
      placeholder.value = "";
      placeholder.textContent = "— избери пункт за разтоварване (по клиента получател) —";
      unloadSelect.appendChild(placeholder);
      if (!client) return;
      var mainAddr = fmtAddress(client);
      if (mainAddr) {
        var mainOpt = document.createElement("option");
        mainOpt.value = mainAddr;
        mainOpt.textContent = "Централен адрес — " + mainAddr;
        unloadSelect.appendChild(mainOpt);
      }
      (client.unload_points || []).forEach(function (p) {
        var addr = fmtAddress(p);
        if (!addr) return;
        var opt = document.createElement("option");
        opt.value = addr;
        opt.textContent = (p.label ? p.label + " — " : "") + addr;
        unloadSelect.appendChild(opt);
      });
    }
    consigneeSelect.addEventListener("change", refreshUnloadPoints);
    unloadSelect.addEventListener("change", function () {
      if (unloadSelect.value) placeDelivery.value = unloadSelect.value;
    });
    refreshUnloadPoints();
  }
}

// Опаковъчен лист (packing_form.html): добавяне на ред в таблицата с
// артикули директно от вече издадена палетна карта (по номер или
// баркод), вместо ръчно преписване на съдържанието ѝ. tableApi е
// резултатът от initItemsTable за таблицата "packing-items" — undefined
// на страници без такава таблица, затова функцията излиза веднага.
function initPullFromPallet(tableApi) {
  var btn = document.getElementById("pull-pallet-btn");
  var input = document.getElementById("pull-pallet-code");
  var msg = document.getElementById("pull-pallet-msg");
  if (!btn || !input || !tableApi) return;
  var form = btn.closest("form");
  var csrfInput = form ? form.querySelector('[name="csrf_token"]') : null;

  function pull() {
    var code = input.value.trim();
    if (!code) return;
    msg.textContent = "Търсене…";
    var body = new URLSearchParams();
    body.set("code", code);
    body.set("csrf_token", csrfInput ? csrfInput.value : "");
    fetch(btn.dataset.url, { method: "POST", body: body })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.ok) {
          tableApi.addRow(data.row);
          msg.textContent = "Добавен ред от палетна карта № " + data.number + ".";
          input.value = "";
          input.focus();
        } else {
          msg.textContent = data.error || "Грешка.";
        }
      })
      .catch(function () { msg.textContent = "Грешка при заявката."; });
  }

  btn.addEventListener("click", pull);
  input.addEventListener("keydown", function (e) {
    if (e.key === "Enter") { e.preventDefault(); pull(); }
  });
}

// ---------------------------------------------------------------- фактури
// (invoice_br_form.html / invoice_no_form.html). Три отделни поведения,
// всичките задвижени от data-* атрибути на самата таблица, за да работят
// еднакво и за двете фактури, въпреки че колоните им се различават:
//
//  1) автоматично попълване на теглото (Бразилия) или описанието
//     (Норвегия) от справочника материали по въведения Material code —
//     кое поле се пълни идва от data-lookup-fill;
//  2) живи суми под таблицата (общо количество/стойност/тегло) — същата
//     сметка като на сървъра (appcore.invoice_totals), за да няма изненада
//     между формата и готовата бланка;
//  3) зареждане на ВСИЧКИ редове от вече издадена палетна карта.

function invoiceNumber(value) {
  if (value === undefined || value === null) return null;
  var text = String(value).trim().replace(/\s/g, "");
  if (!text) return null;
  var n = parseFloat(text.replace(",", "."));
  return isNaN(n) ? null : n;
}

function invoiceFmt(value, decimals) {
  if (value === null) return "";
  var text = value.toFixed(decimals === undefined ? 2 : decimals);
  if (text.indexOf(".") >= 0) text = text.replace(/0+$/, "").replace(/\.$/, "");
  return text || "0";
}

/** Попълва поле на реда от справочника материали, само ако е ПРАЗНО —
 *  вече въведена ръчно стойност никога не се презаписва автоматично. */
function bindInvoiceMaterialLookup(table) {
  var url = table.dataset.lookupUrl;
  var fillField = table.dataset.lookupFill;
  if (!url || !fillField) return;

  var cache = {};

  function fillRow(tr, code) {
    var target = tr.querySelector('input[data-field="' + fillField + '"]');
    if (!target || target.value.trim()) return;  // ръчното въведено печели
    var entry = cache[code.toUpperCase()];
    if (entry === undefined) return;
    if (entry && entry[fillField]) target.value = entry[fillField];
  }

  table.addEventListener("change", function (e) {
    var input = e.target;
    if (!input.dataset || input.dataset.field !== "material_code") return;
    var code = input.value.trim();
    if (!code) return;
    var tr = input.closest("tr");
    var key = code.toUpperCase();
    if (cache[key] !== undefined) { fillRow(tr, code); return; }
    fetch(url + "?code=" + encodeURIComponent(code))
      .then(function (r) { return r.json(); })
      .then(function (data) {
        cache[key] = data.ok ? data : null;
        fillRow(tr, code);
      })
      .catch(function () { /* мрежова грешка — полето просто остава за ръчно попълване */ });
  });
}

/** Живи суми под таблицата — общо количество, обща стойност и (само за
 *  Бразилия, където има колона с тегло) общо нето тегло. */
function bindInvoiceTotals(table, tableApi) {
  var box = document.querySelector('.invoice-totals[data-table="' + table.id + '"]');
  if (!box || !tableApi) return;
  var hasWeight = table.dataset.columns.split(",").indexOf("net_weight") >= 0;

  function update() {
    var items = tableApi.collect();
    var totalQty = 0, totalPrice = 0, totalWeight = 0;
    items.forEach(function (it) {
      var qty = invoiceNumber(it.qty);
      var price = invoiceNumber(it.unit_price);
      var weight = invoiceNumber(it.net_weight);
      if (qty !== null) totalQty += qty;
      // Сумите се трупат от ЗАКРЪГЛЕНИТЕ стойности на всеки ред — точно
      // както на сървъра (appcore.invoice_totals), защото това са числата,
      // които реално се отпечатват на бланката. Иначе живата сума тук би
      // се разминала и с редовете на екрана, и с готовия документ (напр.
      // 10 реда по 0.005: редове „0.01“ = 0.10, а суровият сбор = 0.05).
      if (qty !== null && price !== null) totalPrice += Number((qty * price).toFixed(2));
      if (qty !== null && weight !== null) totalWeight += Number((qty * weight).toFixed(3));
    });
    var parts = [
      "Редове: <b>" + items.length + "</b>",
      "Общо количество: <b>" + (invoiceFmt(totalQty, 3) || "—") + "</b>",
      "Обща стойност: <b>" + (invoiceFmt(totalPrice) || "—") + " €</b>",
    ];
    if (hasWeight) {
      parts.push("Общо нето тегло: <b>" + (invoiceFmt(totalWeight, 3) || "—") + " кг</b>");
    }
    box.innerHTML = parts.join(" · ");
  }

  table.addEventListener("input", update);
  table.addEventListener("click", function () { setTimeout(update, 0); });
  update();
  return update;
}

/** Зарежда ВСИЧКИ редове на издадена палетна карта във фактурата — за
 *  разлика от initPullFromPallet (опаковъчен лист), който добавя един
 *  обобщен ред. Виж routes_invoices.invoice_pull_pallet за съответствието
 *  между колоните на палетната карта и тези на фактурата. */
function bindInvoicePullPallet(box, tableApi, onChanged) {
  var btn = box.querySelector(".invoice-pull-btn");
  var input = box.querySelector(".invoice-pull-code");
  var msg = box.querySelector(".invoice-pull-msg");
  if (!btn || !input || !tableApi) return;
  var form = btn.closest("form");
  var csrfInput = form ? form.querySelector('[name="csrf_token"]') : null;

  function pull() {
    var code = input.value.trim();
    if (!code) return;
    msg.textContent = "Търсене…";
    var body = new URLSearchParams();
    body.set("code", code);
    body.set("csrf_token", csrfInput ? csrfInput.value : "");
    fetch(btn.dataset.url, { method: "POST", body: body })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data.ok) { msg.textContent = data.error || "Грешка."; return; }
        data.rows.forEach(function (row) { tableApi.addRow(row); });
        var text = "Заредени " + data.count + " реда от палетна карта № " + data.number + ".";
        if (data.matched < data.count) {
          text += " Тегло/описание от справочника е намерено за " + data.matched +
                  " от тях — останалите попълнете ръчно.";
        }
        msg.textContent = text;
        input.value = "";
        input.focus();
        if (onChanged) onChanged();
      })
      .catch(function () { msg.textContent = "Грешка при заявката."; });
  }

  btn.addEventListener("click", pull);
  input.addEventListener("keydown", function (e) {
    if (e.key === "Enter") { e.preventDefault(); pull(); }
  });
}

/** Зарежда редове от Excel файл — СЪЩИЯТ файлов формат като импорта в
 *  палетната карта. Качва се през fetch, а не с обикновен submit на
 *  формата, защото формата на фактурата вече съдържа въведени данни
 *  (получател, номер, редове), които обикновен submit към друг адрес би
 *  загубил. Виж routes_invoices.invoice_import_items. */
function bindInvoiceExcelImport(box, tableApi, onChanged) {
  var btn = box.querySelector(".invoice-excel-btn");
  var input = box.querySelector(".invoice-excel-file");
  var msg = box.querySelector(".invoice-excel-msg");
  if (!btn || !input || !tableApi) return;
  var form = btn.closest("form");
  var csrfInput = form ? form.querySelector('[name="csrf_token"]') : null;

  btn.addEventListener("click", function () {
    if (!input.files || !input.files.length) {
      msg.textContent = "Първо изберете .xlsx файл.";
      return;
    }
    msg.textContent = "Зареждане…";
    var body = new FormData();
    body.append("excel_file", input.files[0]);
    body.append("csrf_token", csrfInput ? csrfInput.value : "");
    fetch(btn.dataset.url, { method: "POST", body: body })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data.ok) { msg.textContent = data.error || "Грешка."; return; }
        data.rows.forEach(function (row) { tableApi.addRow(row); });
        var text = "Заредени " + data.count + " реда от „" + data.filename + "“.";
        if (data.matched < data.count) {
          text += " Тегло/описание от справочника е намерено за " + data.matched +
                  " от тях — останалите попълнете ръчно.";
        }
        msg.textContent = text;
        input.value = "";
        if (onChanged) onChanged();
      })
      .catch(function () { msg.textContent = "Грешка при заявката."; });
  });
}

/** Избор от адресната книга за фактури — попълва И блока за доставка
 *  (Consignee), И блока за фактуриране (Bill To) наведнъж, защото записът
 *  пази и двата адреса (виж invoice_clients_module). */
function bindInvoiceClientSelect(form) {
  var select = form.querySelector(".invoice-client-select");
  if (!select) return;
  var entries = [];
  try { entries = JSON.parse(select.dataset.entries || "[]"); } catch (e) { entries = []; }

  select.addEventListener("change", function () {
    var id = parseInt(select.value, 10);
    var entry = entries.find(function (e) { return e.id === id; });
    if (!entry) return;
    var map = {
      consignee_name: entry.delivery_name,
      consignee_address: entry.delivery_address,
      consignee_phone: entry.delivery_phone,
      billto_name: entry.billing_name,
      billto_address: entry.billing_address,
      billto_phone: entry.billing_phone
    };
    Object.keys(map).forEach(function (name) {
      var el = form.querySelector('[name="' + name + '"]');
      if (el) el.value = map[name] || "";
    });
  });
}

/** Копира данните на получателя в блока „Bill To“ — обичайният случай е
 *  фактурата да се плаща от същата фирма (виж образците, където двата
 *  блока често съвпадат). */
function bindCopyConsigneeToBillTo(form) {
  var btn = document.getElementById("copy-consignee-to-billto-btn");
  if (!btn) return;
  btn.addEventListener("click", function () {
    [["consignee_name", "billto_name"],
     ["consignee_address", "billto_address"],
     ["consignee_phone", "billto_phone"]].forEach(function (pair) {
      var from = form.querySelector('[name="' + pair[0] + '"]');
      var to = form.querySelector('[name="' + pair[1] + '"]');
      if (from && to) to.value = from.value;
    });
  });
}

function initInvoiceForm(form, itemsTables) {
  var tables = form.querySelectorAll("table.invoice-items");
  if (!tables.length) return;
  bindCopyConsigneeToBillTo(form);
  bindInvoiceClientSelect(form);
  Array.prototype.forEach.call(tables, function (table) {
    var tableApi = itemsTables[table.id];
    bindInvoiceMaterialLookup(table);
    var update = bindInvoiceTotals(table, tableApi);
    var pullBtn = form.querySelector('.invoice-pull-btn[data-table="' + table.id + '"]');
    if (pullBtn) bindInvoicePullPallet(pullBtn.closest(".card"), tableApi, update);
    var excelBtn = form.querySelector('.invoice-excel-btn[data-table="' + table.id + '"]');
    if (excelBtn) bindInvoiceExcelImport(excelBtn.closest(".card"), tableApi, update);
  });
}

document.addEventListener("DOMContentLoaded", function () {
  initDocumentForm();


  Array.prototype.forEach.call(
    document.querySelectorAll("select.client-select"),
    bindClientSelect
  );
  var scan = document.getElementById("scan-input");
  if (scan) scan.focus();

  // мобилно меню (странична навигация)
  var sidebar = document.getElementById("sidebar");
  var toggle = document.getElementById("sidebar-toggle");
  var backdrop = document.getElementById("sidebar-backdrop");
  function closeSidebar() {
    if (sidebar) sidebar.classList.remove("open");
    if (backdrop) backdrop.classList.remove("open");
  }
  if (toggle && sidebar) {
    toggle.addEventListener("click", function () {
      sidebar.classList.toggle("open");
      if (backdrop) backdrop.classList.toggle("open");
    });
  }
  if (backdrop) backdrop.addEventListener("click", closeSidebar);
  Array.prototype.forEach.call(
    document.querySelectorAll("#sidebar .nav-item"),
    function (a) { a.addEventListener("click", closeSidebar); }
  );

  // Глобално сканиране на баркод — работи от ВСЯКА страница, дори без
  // фокус в конкретно поле. Физическите баркод скенери „пишат“ символите
  // много бързо (клавиатурна емулация) и завършват с Enter. Засичаме този
  // модел и автоматично зареждаме съответния документ. Ако потребителят в
  // момента реално пише в поле за въвеждане (форма, търсене и т.н.), НЕ
  // се намесваме — само в противен случай сканирането се обработва глобално.
  var globalForm = document.getElementById("global-scan-form");
  var globalCode = document.getElementById("global-scan-code");
  if (globalForm && globalCode) {
    var buffer = "";
    var lastTime = 0;
    var MAX_GAP_MS = 60; // между символи при сканиране — обикновено <20мс
    var MAX_BUFFER_LEN = 64; // достатъчно за най-дългия реалистичен баркод/номер

    function isEditableFocus() {
      var el = document.activeElement;
      if (!el) return false;
      var tag = el.tagName;
      return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || el.isContentEditable;
    }

    function isModalOpen() {
      // Скенерът с камерата е активен отделен режим за въвеждане на баркод
      // — глобалният клавиатурен буфер не бива да се намесва междувременно
      // (напр. случайно натискане на клавиш зад отворен модал).
      var modal = document.getElementById("camera-scan-modal");
      return !!(modal && modal.style.display !== "none" && modal.style.display !== "");
    }

    document.addEventListener("keydown", function (e) {
      if (isEditableFocus() || isModalOpen()) { buffer = ""; return; }

      // Игнорираме комбинации с модификатор (Ctrl/Alt/Meta) — иначе браузърни
      // клавишни комбинации с еднобуквен клавиш (Ctrl+A, Cmd+R и т.н.) биха
      // замърсили буфера с случайни символи, различни от реално сканиран код.
      if (e.ctrlKey || e.altKey || e.metaKey) { buffer = ""; return; }

      var now = Date.now();
      if (now - lastTime > MAX_GAP_MS) buffer = "";
      lastTime = now;

      if (e.key === "Enter") {
        if (buffer.length >= 4) {
          globalCode.value = buffer;
          globalForm.submit();
        }
        buffer = "";
        return;
      }
      if (e.key.length === 1) {
        buffer += e.key; // печатаем символ
        if (buffer.length > MAX_BUFFER_LEN) buffer = buffer.slice(-MAX_BUFFER_LEN);
      }
    });
  }

  // Сканиране на баркод с камерата на телефон/компютър — използва
  // вградения в браузъра BarcodeDetector API (без външни библиотеки).
  // Изисква сигурна връзка (https:// или localhost) — иначе браузърите
  // блокират достъпа до камерата.
  var camBtn = document.getElementById("camera-scan-btn");
  var camModal = document.getElementById("camera-scan-modal");
  var camVideo = document.getElementById("camera-scan-video");
  var camClose = document.getElementById("camera-scan-close");
  var camMsg = document.getElementById("camera-scan-msg");
  if (camBtn && camModal && camVideo) {
    var camStream = null;
    var camDetecting = false;
    var camRaf = null;

    function stopCamera() {
      camDetecting = false;
      if (camRaf) cancelAnimationFrame(camRaf);
      camRaf = null;
      if (camStream) {
        camStream.getTracks().forEach(function (t) { t.stop(); });
        camStream = null;
      }
      camVideo.srcObject = null;
      camModal.style.display = "none";
    }

    function submitScanned(code) {
      stopCamera();
      if (globalForm && globalCode) {
        globalCode.value = code;
        globalForm.submit();
      }
    }

    function tick(detector) {
      if (!camDetecting) return;
      detector.detect(camVideo).then(function (codes) {
        if (codes && codes.length) {
          submitScanned(codes[0].rawValue);
          return;
        }
        camRaf = requestAnimationFrame(function () { tick(detector); });
      }).catch(function () {
        camRaf = requestAnimationFrame(function () { tick(detector); });
      });
    }

    camBtn.addEventListener("click", function () {
      camMsg.textContent = "";
      camModal.style.display = "flex";
      if (!window.isSecureContext) {
        camMsg.textContent = "Камерата изисква сигурна връзка (https://). "
          + "Ако сканирате от телефон извън офиса, използвайте отдалечения "
          + "адрес от „⚙ Настройки“ (само за администратори).";
        return;
      }
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        camMsg.textContent = "Този браузър не поддържа достъп до камера.";
        return;
      }
      if (!("BarcodeDetector" in window)) {
        camMsg.textContent = "Този браузър не поддържа вградено разпознаване "
          + "на баркод (напр. Safari на по-стар iPhone). Използвайте "
          + "физически скенер или полето за въвеждане.";
        return;
      }
      navigator.mediaDevices.getUserMedia({
        video: { facingMode: { ideal: "environment" } }
      }).then(function (stream) {
        camStream = stream;
        camVideo.srcObject = stream;
        var detector = new window.BarcodeDetector({ formats: ["code_128"] });
        camDetecting = true;
        camRaf = requestAnimationFrame(function () { tick(detector); });
      }).catch(function (err) {
        camMsg.textContent = "Достъпът до камерата е отказан или неуспешен: " + err.message;
      });
    });
    if (camClose) camClose.addEventListener("click", stopCamera);
  }
});
