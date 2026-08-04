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

// Динамична таблица с артикули: добавяне/махане на редове и сериализация
// към скрито поле items_json при изпращане на формата.
function initItemsTable(table, columns, initialItems) {
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
      var hidden = form.querySelector('input[name="items_json"]');
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
});
