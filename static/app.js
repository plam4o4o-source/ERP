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
      input.value = item[col] || "";
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
      var hidden = form.querySelector('input[name="' + hiddenFieldName + '"]');
      if (hidden) hidden.value = JSON.stringify(collect());
    });
  }
  return { collect: collect, addRow: addRow };
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

  if (form.dataset.edit) {
    try { prefillForm(form, JSON.parse(form.dataset.edit)); } catch (e) {}
  }
}

// ЧМР (cmr_form.html): 4. Товарен пункт — избор от адресите на всички
// фирми в адресната книга (стоката може да се товари от адреса на всяка
// от тях, не само от изпращача). 3. Разтоварен пункт — списъкът зависи
// от избрания клиент получател (поле 2): всеки клиент може да има
// неограничен брой запаметени пунктове за разтоварване (адресна книга →
// редакция на клиент). Задейства се само ако страницата има тези
// елементи (само cmr_form.html ги съдържа).
function initCmrPlaces() {
  var loadSelect = document.getElementById("loading-point-select");
  var placeLoading = document.getElementById("place_loading");
  var consigneeSelect = document.querySelector('select.client-select[data-target="consignee"]');
  var unloadSelect = document.getElementById("unload-point-select");
  var placeDelivery = document.getElementById("place_delivery");
  if (!loadSelect && !unloadSelect) return;

  function fmtAddress(o) {
    return [o.address, [o.postcode, o.city].filter(Boolean).join(" "), o.country]
      .filter(Boolean).join(", ");
  }

  if (loadSelect && placeLoading) {
    (window.CLIENTS || []).forEach(function (c) {
      var addr = fmtAddress(c);
      if (!addr) return;
      var opt = document.createElement("option");
      opt.value = addr;
      opt.textContent = c.name + " — " + addr;
      loadSelect.appendChild(opt);
    });
    loadSelect.addEventListener("change", function () {
      if (loadSelect.value) placeLoading.value = loadSelect.value;
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
