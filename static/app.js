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

document.addEventListener("DOMContentLoaded", function () {
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
