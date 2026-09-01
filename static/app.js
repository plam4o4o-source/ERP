// ПачоЛогистик — общи скриптове за формите

// ---------------------------------------------------------------- превод на низовете (i18n)
// Одит (19.08.2026, находка №13): дотук ВСИЧКИ потребителски низове в този
// файл бяха твърдо на български — EN/TR потребителят виждаше кирилица в
// toast-овете, подсказките и (най-лошо) в title/aria-label, които стигат
// до екранния четец. Речникът се рендира СЪРВЪРНО в templates/base.html
// (стойностите минават през _() → влизат в двата каталога) и пътува като
// data-i18n атрибут на СЪЩИЯ <script> таг, който зарежда този файл —
// формите нямат право на вграден <script> блок извън app.js (виж
// tests/test_form_scripts). Тук го разопаковаме веднъж в window.I18N,
// който си остава публичният договор за останалия код.
//
// При БЪЛГАРСКИ интерфейс атрибутът изобщо липсва (българският е
// изходният език — msgstr би бил равен на msgid), затова window.I18N е
// празен обект и всичко пада към fallback-ите по-долу.
window.I18N = (function () {
  try {
    var tag = document.currentScript || document.querySelector("script[data-i18n]");
    var raw = tag && tag.getAttribute("data-i18n");
    return raw ? JSON.parse(raw) : {};
  } catch (e) {
    // Повреден/непълен JSON не бива да събаря целия скрипт — интерфейсът
    // просто остава на български (виж t() по-долу).
    return {};
  }
})();

// t(key, fallback) НАРОЧНО пада към българския fallback, а не към празен
// низ: при български интерфейс, при кеширана стара страница без речника
// или при новодобавен ключ преди преводът да е готов интерфейсът остава
// четим на български, вместо да покаже празни бутони и празни съобщения.
function t(key, fallback) {
  var dict = window.I18N || {};
  var value = dict[key];
  return (typeof value === "string" && value !== "") ? value : fallback;
}

// tf(key, fallback, params) — същото, но замества ИМЕНУВАНИ плейсхолдъри
// във ФИГУРНИ скоби: {name}. Именувани са, за да може преводът свободно
// да ги пренарежда или да ползва един и същ повторно (напр. двуезичното
// „Карта {n} / Card {n}“, което на EN е само „Card {n}“), без
// форматирането да се счупи. Фигурни скоби, а НЕ %(name)s: Jinja-версията
// на _() в base.html е „newstyle“ и сама прилага `%` върху резултата, при
// което %(name)s гърми с KeyError още при рендирането на страницата (виж
// коментара при js_i18n в templates/base.html). Липсващ ключ в params
// оставя плейсхолдъра непокътнат — по-добре видим дефект, отколкото тихо
// "undefined" в изречението.
function tf(key, fallback, params) {
  var p = params || {};
  return t(key, fallback).replace(/\{(\w+)\}/g, function (whole, name) {
    return Object.prototype.hasOwnProperty.call(p, name) ? String(p[name]) : whole;
  });
}

// „N от M“ в полето „Палет №“ при карта с няколко палета. Форматът е
// СЪЩИЯТ msgid, който ползва и сървърът (routes_pallet_extra._apply_bulk_
// fields → _("{n} от {total}")), за да съвпадат стойностите, издадени от
// формата и от bulk импорта. palletNoOfPattern() гради разпознаващия израз
// от същия (вече преведен) шаблон — иначе на EN/TR полето нямаше да се
// изчиства при връщане към един палет.
function palletNoOf(index, total) {
  return tf("pallet_no_of", "{n} от {total}", { n: index, total: total });
}

function palletNoOfPattern() {
  // Първо екранираме всичко като литерал, чак после връщаме двата
  // плейсхолдъра като \d+ — иначе фигурните скоби в шаблона биха се
  // изтълкували като квантификатор и изразът щеше да е невалиден.
  var escaped = t("pallet_no_of", "{n} от {total}").replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp("^" + escaped.replace(/\\\{(?:n|total)\\\}/g, "\\d+") + "$");
}

// ---------------------------------------------------------------- fetch + JSON помощник
// Одит (находка С6, среден риск): всички fetch(...).then(r => r.json())
// извиквания в тази страница НЕ проверяваха r.ok — при изтекла сесия
// login_required пренасочва (302) към /login; fetch АВТОМАТИЧНО следва
// пренасочването и получава HTML на входната страница с обикновен 200
// (r.ok си остава true!), затова r.json() гърми с SyntaxError (HTML не е
// валиден JSON) и попада в общия .catch — потребителят вижда "Грешка при
// заявката." и няма никаква представа, че всъщност трябва да влезе отново
// (продължава да пълни форма, която накрая ще го изхвърли). r.redirected
// (стандартно, широко поддържано свойство на Response) е true точно в
// такъв случай — крайният адрес (r.url) сочи /login. fetchJsonSafe тук
// разпознава изрично този случай и подава ясен маркер (err.sessionExpired)
// на извикващия .catch, вместо неразличимо генерично "Грешка".
function fetchJsonSafe(url, opts) {
  return fetch(url, opts).then(function (r) {
    if (r.redirected && /\/login(?:[/?]|$)/.test(r.url)) {
      var expiredErr = new Error("session-expired");
      expiredErr.sessionExpired = true;
      throw expiredErr;
    }
    if (!r.ok) {
      var httpErr = new Error("http-" + r.status);
      httpErr.httpStatus = r.status;
      throw httpErr;
    }
    return r.json();
  });
}

//: Текстът, показван при разпознато изтичане на сесията (виж fetchJsonSafe
//: по-горе) — общ за всички fetch извиквания на страницата, за да е
//: еднакво/разпознаваемо съобщението навсякъде.
// Одит (19.08.2026, находка №13): преведен през window.I18N (виж t() в
// началото на файла). Изчислява се при зареждане на скрипта — вграденият
// речник в base.html е ПРЕДИ <script src="app.js">, значи вече е наличен.
var SESSION_EXPIRED_MSG = t("session_expired",
  "Сесията е изтекла — презаредете страницата и влезте отново.");

// ---------------------------------------------------------------- toast съобщения
// Появяват се анимирано горе вдясно (виж .toasts в style.css). Успех/инфо
// (.toast-auto) се скриват сами след AUTO_DISMISS_MS; при посочване с
// мишката таймерът И прогрес-линийката се паузират, за да се дочете.
// Грешки/предупреждения остават до ръчно затваряне с ✕.
var TOAST_AUTO_DISMISS_MS = 5000;

function dismissToast(toast) {
  if (toast.dataset.hiding) return;
  toast.dataset.hiding = "1";
  toast.classList.add("toast-hide");
  // Изчакваме анимацията на скриване (200ms в style.css); резервният
  // таймер маха елемента и при prefers-reduced-motion, където
  // animationend не се случва (анимациите са изключени).
  var done = false;
  function remove() { if (!done) { done = true; toast.remove(); } }
  toast.addEventListener("animationend", remove);
  setTimeout(remove, 400);
}

function initToasts() {
  Array.prototype.forEach.call(document.querySelectorAll(".toasts .toast"), function (toast) {
    var closeBtn = toast.querySelector(".toast-close");
    if (closeBtn) closeBtn.addEventListener("click", function () { dismissToast(toast); });
    if (!toast.classList.contains("toast-auto")) return;

    var bar = toast.querySelector(".toast-bar");
    if (bar) bar.style.animationDuration = TOAST_AUTO_DISMISS_MS + "ms";
    var remaining = TOAST_AUTO_DISMISS_MS;
    var startedAt = Date.now();
    var timer = setTimeout(function () { dismissToast(toast); }, remaining);
    toast.addEventListener("mouseenter", function () {
      clearTimeout(timer);
      remaining -= Date.now() - startedAt;
      if (bar) bar.style.animationPlayState = "paused";
    });
    toast.addEventListener("mouseleave", function () {
      startedAt = Date.now();
      timer = setTimeout(function () { dismissToast(toast); }, Math.max(remaining, 400));
      if (bar) bar.style.animationPlayState = "running";
    });
  });
}

// ---------------------------------------------------------------- модали: фокус
// Одит (19.08.2026, находка №33): и трите модала в приложението
// (#confirm-modal, #camera-scan-modal, #pallet-type-modal) се обявяваха с
// role="dialog" aria-modal="true", но нямаха НИКАКВО управление на фокуса.
// Измерено: при отваряне фокусът оставаше върху бутона ЗАД наслагването, а
// 8 последователни Tab-а минаваха през елементите на страницата отдолу —
// тоест клавиатурен потребител „натиска“ бутони, които не вижда (те са под
// затъмнението), а екранен четец чете съдържание, което диалогът твърди, че
// е недостъпно (aria-modal="true"). След Escape фокусът увисваше в началото
// на документа, вместо да се върне на бутона, който е отворил диалога.
//
// activateModal прави четирите неща, които правят диалога истински модален:
//   1) премества фокуса ВЪТРЕ в модала;
//   2) затваря Tab в кръг между елементите му (циклира от последния към
//      първия и обратно при Shift+Tab);
//   3) изважда фона (.app-shell) от достъпното дърво с `inert` (резервно
//      aria-hidden за по-стари браузъри), така че нито мишка, нито
//      клавиатура, нито екранен четец да го достигат;
//   4) при затваряне връща фокуса ТОЧНО на елемента, който е отворил
//      модала — иначе следващият Tab тръгва от началото на страницата.
// Връща функция за деактивиране, която затварящият код вика вместо да
// повтаря тези стъпки на три места.
var MODAL_FOCUSABLE_SELECTOR = 'a[href], button:not([disabled]), ' +
  'input:not([disabled]):not([type="hidden"]), select:not([disabled]), ' +
  'textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

function modalFocusableItems(modal) {
  return Array.prototype.filter.call(
    modal.querySelectorAll(MODAL_FOCUSABLE_SELECTOR),
    function (el) { return el.offsetWidth > 0 || el.offsetHeight > 0; });
}

function activateModal(modal, initialFocus, restoreFocusTo) {
  // Елементът, от който е тръгнало отварянето — точката, в която фокусът
  // трябва да се върне при затваряне. Извикващият може да го подаде
  // изрично (напр. падащото меню „Тип палет“): при отваряне, задействано
  // от събитие БЕЗ фокус (програмна промяна, докосване на екран, което
  // някои браузъри не фокусират), document.activeElement е <body> и
  // връщането на фокуса не би имало къде да стане.
  var restoreTo = restoreFocusTo || document.activeElement;
  // .app-shell съдържа ЦЯЛОТО приложение без самите модали (виж base.html
  // — те стоят извън него именно за да може фонът да се неутрализира
  // наведнъж). Проверката contains() е предпазна: модал ВЪТРЕ в обвивката
  // би станал inert заедно с нея (inert се наследява и не се „отменя“
  // отвътре) и диалогът би останал без фокус изобщо.
  var shell = document.querySelector(".app-shell");
  if (shell && shell.contains(modal)) shell = null;
  if (shell) {
    shell.inert = true;
    if (!("inert" in HTMLElement.prototype)) shell.setAttribute("aria-hidden", "true");
  }

  function onKeydown(e) {
    if (e.key !== "Tab") return;
    var items = modalFocusableItems(modal);
    if (!items.length) { e.preventDefault(); return; }
    var first = items[0];
    var last = items[items.length - 1];
    var inside = modal.contains(document.activeElement);
    if (e.shiftKey && (!inside || document.activeElement === first)) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && (!inside || document.activeElement === last)) {
      e.preventDefault();
      first.focus();
    }
  }
  // capture фаза: прихващаме Tab-а преди страницата отдолу да реагира.
  document.addEventListener("keydown", onKeydown, true);

  var target = initialFocus || modalFocusableItems(modal)[0];
  if (target) target.focus();

  return function deactivateModal() {
    document.removeEventListener("keydown", onKeydown, true);
    if (shell) {
      shell.inert = false;
      shell.removeAttribute("aria-hidden");
    }
    // document.contains: бутонът, отворил модала, може вече да не
    // съществува (напр. редът с него е премахнат) — тогава поне махаме
    // фокуса от вече скрития модал, вместо да гърмим.
    if (restoreTo && restoreTo !== document.body &&
        typeof restoreTo.focus === "function" && document.contains(restoreTo)) {
      restoreTo.focus();
    } else if (modal.contains(document.activeElement)) {
      // Скриването с display:none НЕ размества само по себе си фокуса в
      // Chromium — активен остава невидим елемент и следващият Tab тръгва
      // от нищото. blur() го връща на <body>, откъдето Tab продължава
      // нормално от началото на страницата.
      document.activeElement.blur();
    }
  };
}

// ---------------------------------------------------------------- потвърждение
// Форма с data-confirm="съобщение" показва стилизирания модал #confirm-modal
// (маркъпът е в base.html, за да мине през преводите) вместо браузърния
// confirm(). При „Потвърди“ формата се изпраща наистина (флагът
// data-confirmed спира повторното прихващане).
function initConfirmModal() {
  var modal = document.getElementById("confirm-modal");
  if (!modal) return;
  var text = document.getElementById("confirm-modal-text");
  var okBtn = document.getElementById("confirm-modal-ok");
  var cancelBtn = document.getElementById("confirm-modal-cancel");
  var closeBtn = document.getElementById("confirm-modal-close");
  var pendingForm = null;
  // Одит (находка С8, среден риск): initBusyForms маркира .btn-busy
  // (pointer-events:none) на ВСЯКО подаване на data-busy форма —
  // включително това, прихванато ТУК от submit слушателя по-долу
  // (e.preventDefault() спира само РЕАЛНОТО изпращане, не другите
  // слушатели на СЪЩОТО събитие). pendingSubmitter пази кой точно
  // бутон е това, за да можем да го изчистим при "Отказ"/затваряне.
  var pendingSubmitter = null;
  // Одит (19.08.2026, находка №33): функцията за връщане на фокуса и
  // „отпускане“ на фона, върната от activateModal при отварянето.
  var deactivate = null;

  function closeModalUI() {
    modal.style.display = "none";
    document.removeEventListener("keydown", onKey);
    if (deactivate) { deactivate(); deactivate = null; }
  }

  function hide() {
    // Истинско "Отказ" (или затваряне/клик извън модала) — формата НЕ се
    // изпраща, затова .btn-busy трябва да се махне, иначе бутонът остава
    // НАВЕЧНО некликаем (pointer-events:none) до презареждане на
    // страницата. Засягаше най-вече "Изтегли от GitHub" — точно бутона,
    // при който човек най-вероятно ще се откаже поне веднъж.
    closeModalUI();
    if (pendingSubmitter) pendingSubmitter.classList.remove("btn-busy");
    pendingForm = null;
    pendingSubmitter = null;
  }
  function onKey(e) { if (e.key === "Escape") hide(); }

  okBtn.addEventListener("click", function () {
    if (!pendingForm) return hide();
    var form = pendingForm;
    // Формата РЕАЛНО ще се изпрати ей сега — НЕ минаваме през hide(),
    // за да НЕ махнем .btn-busy точно преди истинското подаване (иначе
    // бутонът за миг би изглеждал "неактивен", после пак busy).
    closeModalUI();
    pendingForm = null;
    pendingSubmitter = null;
    form.dataset.confirmed = "1";
    // requestSubmit минава през нормалния submit път (CSRF полето и
    // items_json сериализацията се пращат както при истински клик).
    if (form.requestSubmit) form.requestSubmit(); else form.submit();
  });
  cancelBtn.addEventListener("click", hide);
  closeBtn.addEventListener("click", hide);
  modal.addEventListener("click", function (e) { if (e.target === modal) hide(); });

  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!form.dataset || !form.dataset.confirm) return;
    if (form.dataset.confirmed === "1") { delete form.dataset.confirmed; return; }
    e.preventDefault();
    // Одит (16.08.2026, находка №25): initBusyForms (виж app.js по-горе)
    // вече е сложил form.dataset.submitting="1" за ТОЗИ опит (target-фаза
    // на form листенъра се изпълнява преди document-нивото тук) — но той
    // всъщност НЕ води до истинско подаване сега (модалът тепърва пита за
    // потвърждение). Нулираме флага (НЕ и визуалния .btn-busy клас — той
    // нарочно остава, докато модалът е отворен, виж hide()/находка С8 по-
    // долу), иначе следващото form.requestSubmit() (при "Прилагане") би
    // било блокирано от initBusyForms като "вече се подава".
    if (form.dataset) form.dataset.submitting = "";
    pendingForm = form;
    pendingSubmitter = e.submitter || form.querySelector('button[type="submit"]');
    text.textContent = form.dataset.confirm;
    modal.style.display = "flex";
    document.addEventListener("keydown", onKey);
    // Одит (19.08.2026, находка №33): фокусът отива върху „Отказ“ (не
    // върху „Потвърди“ — диалогът пита за НЕОБРАТИМО действие, затова
    // случайният Enter трябва да значи „не“), Tab се затваря в кръг в
    // модала, фонът става inert, а при затваряне фокусът се връща на
    // бутона, който е отворил диалога.
    deactivate = activateModal(modal, cancelBtn);
  });
}

// ---------------------------------------------------------------- зает бутон
// Форма с data-busy: при изпращане submit бутонът ѝ получава въртящ се
// индикатор и спира да приема кликове — видим знак, че бавната/фонова
// операция (GitHub качване, архив, отдалечен достъп) е започнала.

/* ============================================================================
   ЖИВО ТЪРСЕНЕ (заявка: „живо търсене, докато пишете“)
   ----------------------------------------------------------------------------
   Формата си остава обикновена GET форма — БЕЗ JS бутонът „Търси“ работи както
   винаги. Тук само добавяме: докато операторът пише, след кратка пауза
   изтегляме СЪЩИЯ адрес и подменяме единствено контейнера с резултатите.

   Защо така, а не с нов „API“ маршрут: сървърът вече рендира точно тази
   страница за точно тези GET параметри — цялата логика на търсенето
   (пагинация, групиране, права) остава на ЕДНО място. Изваждаме нужното парче
   от готовия HTML, вместо да поддържаме втори път, който би се разминал с
   първия при следваща промяна.

   Три неща, които правят разликата между „работи“ и „работи наистина“:
     · закъсняла заявка НИКОГА не презаписва по-нова (пореден номер + abort);
     · фокусът и позицията на курсора в полето остават непокътнати;
     · адресът в лентата се обновява, така че презареждане/споделяне/„назад“
       показват същия резултат.
   ========================================================================== */

var LIVE_SEARCH_DELAY = 300;   /* пауза след последната буква */
var LIVE_SEARCH_BUSY_AFTER = 400;  /* чак след толкова показваме индикатор */

function initLiveSearch() {
  Array.prototype.forEach.call(
    document.querySelectorAll("form[data-live-search]"),
    function (form) { bindLiveSearch(form); }
  );
}

/* Одит (25.08.2026, находка №8): съобщава на екранните четци какво се е
   променило след живо търсене — кратко, вместо цялата таблица да се чете
   наглас. Брои редовете с данни в подменения контейнер (заглавният ред и
   евентуалният групиращ ред не се броят); при празен резултат казва го
   изрично. Тихо не прави нищо, ако скритата област липсва (стар шаблон). */
function announceLiveSearch(results) {
  var region = document.getElementById("live-search-announce");
  if (!region) return;
  var msg;
  if (results.querySelector(".empty-state")) {
    msg = tf("live_search_none", "Няма намерени резултати.");
  } else {
    var table = results.querySelector("table.list");
    if (table) {
      var count = table.querySelectorAll("tr").length;
      /* минус заглавния ред и минус евентуалните групиращи редове */
      var headers = table.querySelectorAll("tr > th").length ? 1 : 0;
      var groups = table.querySelectorAll("tr.list-group-row").length;
      count = Math.max(0, count - headers - groups);
      msg = tf("live_search_found", "Намерени {count} резултата.", { count: count });
    } else {
      msg = tf("live_search_updated", "Резултатите са обновени.");
    }
  }
  /* Смяната на текста е сигналът за aria-live; ако е същият текст, кратко
     го изчистваме, за да се преизлъчи (иначе четецът мълчи). */
  if (region.textContent === msg) region.textContent = "";
  region.textContent = msg;
}

function bindLiveSearch(form) {
  var selector = form.dataset.liveSearch;
  var results = document.querySelector(selector);
  var input = form.querySelector(".searchbar-input");
  if (!results || !input) return;
  if (typeof window.fetch !== "function" || typeof window.DOMParser !== "function") return;

  var clearBtn = form.querySelector(".searchbar-clear");
  var busy = form.querySelector(".searchbar-busy");
  var timer = null, busyTimer = null, controller = null, seq = 0;

  function toggleClear() {
    if (clearBtn) clearBtn.classList.toggle("is-visible", input.value !== "");
  }

  function setBusy(on) {
    if (busy) busy.classList.toggle("is-visible", !!on);
    results.classList.toggle("is-loading", !!on);
  }

  function run() {
    var mySeq = ++seq;
    /* Прекъсваме предишната заявка: при бързо писане иначе се трупат
       заявки, а бавната мрежа ги връща разбъркано. */
    if (controller) { try { controller.abort(); } catch (e) {} }
    controller = (typeof AbortController === "function") ? new AbortController() : null;

    var params = new URLSearchParams(new FormData(form));
    var url = (form.getAttribute("action") || window.location.pathname);
    var qs = params.toString();
    if (qs) url += "?" + qs;

    clearTimeout(busyTimer);
    busyTimer = setTimeout(function () { if (mySeq === seq) setBusy(true); }, LIVE_SEARCH_BUSY_AFTER);

    fetch(url, {
      headers: {"X-Requested-With": "live-search"},
      signal: controller ? controller.signal : undefined
    }).then(function (r) {
      /* Одит (25.08.2026, находка №7): при изтекла сесия login_required
         пренасочва (302) към /login; fetch следва пренасочването сам, значи
         r.ok е true, а крайният адрес (r.url) сочи /login. Преди това кодът
         просто не намираше контейнера с резултатите в HTML-а на входа и
         тихо връщаше — операторът оставаше пред СТАР списък на страница,
         която ИЗГЛЕЖДА логната, и продължаваше да пише напразно. Сега го
         пренасочваме към входа (същата проверка като fetchJsonSafe по-горе),
         за да види ясно, че трябва да влезе пак. __handled спира form.submit
         в catch-а по-долу да не се надпреварва с навигацията. */
      if (r.redirected && /\/login(?:[/?]|$)/.test(r.url)) {
        window.location.href = r.url;
        var expired = new Error("session-expired");
        expired.__handled = true;
        throw expired;
      }
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.text();
    }).then(function (html) {
      if (mySeq !== seq) return;   /* закъсняла — по-нова вече е тръгнала */
      var doc = new DOMParser().parseFromString(html, "text/html");
      var fresh = doc.querySelector(selector);
      if (!fresh) return;          /* сървърът върна друга страница (напр. вход) */
      results.innerHTML = fresh.innerHTML;
      try { window.history.replaceState(null, "", url); } catch (e) {}
      announceLiveSearch(results);
    }).catch(function (err) {
      /* Прекъснатата заявка НЕ е грешка — тя е точно каквото искахме. */
      if (err && err.name === "AbortError") return;
      /* Изтекла сесия (находка №7) — вече пренасочваме към входа, не пипаме. */
      if (err && err.__handled) return;
      if (mySeq !== seq) return;
      /* При реален проблем не оставяме стар резултат да изглежда актуален:
         връщаме се към обикновено изпращане на формата. */
      form.submit();
    }).then(function () {
      if (mySeq === seq) { clearTimeout(busyTimer); setBusy(false); }
    });
  }

  function schedule() {
    toggleClear();
    clearTimeout(timer);
    timer = setTimeout(run, LIVE_SEARCH_DELAY);
  }

  input.addEventListener("input", schedule);
  /* Промяна на падащо меню/дата се прилага веднага — там няма писане. */
  Array.prototype.forEach.call(form.querySelectorAll("select, input[type=date]"), function (el) {
    el.addEventListener("change", function () { clearTimeout(timer); run(); });
  });

  if (clearBtn) {
    clearBtn.addEventListener("click", function () {
      input.value = "";
      toggleClear();
      input.focus();
      clearTimeout(timer);
      run();
    });
  }

  /* Enter не бива да презарежда цялата страница, щом вече филтрираме живо. */
  form.addEventListener("submit", function (e) {
    if (!controller && typeof AbortController !== "function") return;  /* стар браузър — нормално изпращане */
    e.preventDefault();
    clearTimeout(timer);
    run();
  });

  toggleClear();
}

function initBusyForms() {
  Array.prototype.forEach.call(document.querySelectorAll("form[data-busy]"), function (form) {
    form.addEventListener("submit", function (e) {
      // Одит (16.08.2026, находка №25, средна): .btn-busy (pointer-events:
      // none по-долу) е чисто ВИЗУАЛНА/CSS защита — блокира само МИШКА/
      // допир върху КОНКРЕТНИЯ вече кликнат бутон. НЕ пречи на: (а) Enter
      // в текстово поле, което задейства submit на подразбиращия се бутон
      // отново, докато СЪЩАТА заявка още виси; (б) много бърз двоен клик,
      // при който вторият click/submit евент може да се случи, преди
      // браузърът реално да е приложил новодобавения CSS клас в следващия
      // кадър на рендиране. form.dataset.submitting е проверка на нивото
      // на самото SUBMIT СЪБИТИЕ (не CSS) — e.preventDefault() тук спира
      // ВСЯКО повторно подаване на ТАЗИ форма, докато първото все още не е
      // довело до презареждане/пренасочване на страницата (което по
      // естествен начин нулира флага заедно с цялата JS state).
      if (form.dataset.submitting === "1") { e.preventDefault(); return; }
      form.dataset.submitting = "1";
      // Одит (находка С5, среден риск): формите за издаване на документи
      // (напр. cmr_form.html) имат ДВА (или повече) submit бутона в
      // ЕДНА форма — основният "Издай.../Запази промените" И "Предварителен
      // преглед" (различен formaction). e.submitter (стандартно, широко
      // поддържано свойство на SubmitEvent) сочи ТОЧНО кой бутон реално е
      // изпратил формата — иначе form.querySelector('button[type="submit"]')
      // винаги би хванал ПЪРВИЯ бутон в DOM реда, независимо кой всъщност
      // е бил натиснат, и щеше да остави РЕАЛНО натиснатия бутон напълно
      // кликаем за повторно изпращане. e.submitter е null само при
      // form.requestSubmit() без аргумент (виж initConfirmModal по-горе) —
      // тогава пада обратно към първия submit бутон, какъвто е бил преди.
      var btn = e.submitter || form.querySelector('button[type="submit"]');
      // След началото на submit-а е безопасно да маркираме бутона —
      // .btn-busy ползва pointer-events:none (не disabled), за да не
      // попречи на изпращането на стойността на самия бутон.
      if (btn) btn.classList.add("btn-busy");
    });
  });
}


// Кратко зелено премигване на автоматично попълнено поле (адресна книга/
// справочник материали) — операторът вижда какво точно се е попълнило.
function markAutofilled(el) {
  if (!el || el.type === "hidden") return;
  el.classList.remove("autofilled");
  void el.offsetWidth; // рестартира CSS анимацията при повторен избор
  el.classList.add("autofilled");
}

// ---------------------------------------------------------------- сървърно
// търсене на клиент във формите
// Одит (19.08.2026, находка №25, средна): всяка форма вграждаше ЦЯЛАТА
// адресна книга ДВА пъти — веднъж като <option>-и и веднъж като JSON за
// автоматичното попълване на полетата. Измерено при 5 000 клиента:
// /cmr/new → 2 695 KB, /invoice-br/new → 2 123 KB HTML на ВСЯКО отваряне
// на форма, при това често през тунел/бавна LAN.
//
// Сега сървърът вгражда най-много appcore.CLIENT_EMBED_LIMIT записа. При
// типична адресна книга (десетки записи) това са ВСИЧКИ — тогава нищо от
// кода по-долу не се задейства и поведението е буквално непроменено.
// Само когато книгата е по-голяма от вграденото, над падащото меню се
// появява поле за търсене, което пита сървъра (/clients/lookup).
//
// Автодовършването е ключова функция и не бива да зависи от мрежата:
// вграденият списък ОСТАВА напълно работещ през цялото време (изборът от
// него попълва полетата без нито една заявка), заявките са дебоунснати,
// закъснели отговори се изхвърлят по пореден номер, а при грешка/бавна
// мрежа менюто пази текущото си съдържание и се показва обяснение —
// никога не се изпразва под ръцете на оператора.
var CLIENT_SEARCH_DEBOUNCE_MS = 300;

function clientOptionLabel(c) {
  // Същият текст като сървърния макрос client_select_options в
  // templates/_macros.html — списъкът не бива да изглежда различно според
  // това дали клиентът е дошъл вграден, или от търсенето.
  var label = c.name || "";
  if (c.alias) label += " (" + c.alias + ")";
  label += " — " + (c.city || "");
  if (c.country) label += ", " + c.country;
  return label;
}

function mergeClients(list) {
  var all = window.CLIENTS || (window.CLIENTS = []);
  list.forEach(function (c) {
    for (var i = 0; i < all.length; i++) {
      if (all[i].id === c.id) { all[i] = c; return; }
    }
    all.push(c);
  });
}

function attachClientSearch(select) {
  var form = select.closest ? select.closest("form") : null;
  if (!form || !form.dataset.clientsUrl) return;
  var total = parseInt(form.dataset.clientsTotal, 10);
  var embedded = (window.CLIENTS || []).slice();
  // Цялата адресна книга вече е във формата — няма какво да се търси.
  if (!isFinite(total) || total <= embedded.length) return;

  var box = document.createElement("div");
  box.style.margin = "0 0 6px";
  var input = document.createElement("input");
  input.type = "search";
  input.autocomplete = "off";
  input.placeholder = t("client_search_placeholder",
    "Търсене на клиент (фирма, град, ЕИК)…");
  input.setAttribute("aria-label", input.placeholder);
  /* Одит (31.08.2026, находка №2, ВИСОКА): Enter в това поле НЕ бива да
     изпраща формата.

     Полето се вгражда ВЪТРЕ в <form id="main-doc-form"> (виж insertBefore
     по-долу), а формата има submit бутон — значи Enter задейства
     подразбиращото се HTML изпращане. Проверено в реален браузър (321
     клиента, /cmr/new с вече попълнен получател): пишеш част от името,
     списъкът се филтрира правилно, натискаш Enter, за да потвърдиш
     търсенето — и браузърът отива на /doc/1. Издадено е истинско ЧМР,
     изразходван е номер от годишната номерация, от полупопълнена форма, без
     изобщо да е избран клиент. Засяга всичките 6 документни форми и
     pallet_bulk_review.html.

     За оператора Enter в поле за търсене значи „покажи ми намереното“, не
     „издай документа“ — затова просто го спираме. Същото вече се прави при
     издърпването от палетна карта (initPullFromPallet). */
  input.addEventListener("keydown", function (e) {
    if (e.key === "Enter") e.preventDefault();
  });
  var status = document.createElement("div");
  status.setAttribute("aria-live", "polite");
  status.style.cssText = "color:var(--fg-soft);font-size:12.5px;margin-top:4px";
  status.textContent = tf("client_search_partial",
    "Показани са първите {shown} от {total} клиента — напишете няколко букви, за да намерите останалите.",
    { shown: embedded.length, total: total });
  box.appendChild(input);
  box.appendChild(status);
  select.parentNode.insertBefore(box, select);

  // Първата опция е „— изберете клиент —“ (рендира се от шаблона) и
  // остава на място при всяко пренареждане, за да може операторът винаги
  // да изчисти избора си.
  var placeholder = select.options.length ? select.options[0] : null;
  var timer = null;
  var seq = 0;

  function renderOptions(list) {
    var keep = select.value;
    select.innerHTML = "";
    if (placeholder) select.appendChild(placeholder);
    list.forEach(function (c) {
      var opt = document.createElement("option");
      opt.value = c.id;
      opt.textContent = clientOptionLabel(c);
      select.appendChild(opt);
    });
    // Ако вече избраният клиент е сред новите резултати, изборът се пази
    // (иначе смяната на текста в полето за търсене би „отбрала“ вече
    // попълнените адресни полета).
    select.value = keep;
    if (!select.value && placeholder) select.value = placeholder.value;
  }

  input.addEventListener("input", function () {
    var q = input.value.trim();
    if (timer) clearTimeout(timer);
    if (!q) {
      seq++;  // отменя всеки летящ отговор
      renderOptions(embedded);
      status.textContent = tf("client_search_partial",
        "Показани са първите {shown} от {total} клиента — напишете няколко букви, за да намерите останалите.",
        { shown: embedded.length, total: total });
      return;
    }
    status.textContent = t("searching", "Търсене…");
    timer = setTimeout(function () {
      var mine = ++seq;
      fetchJsonSafe(form.dataset.clientsUrl + "?q=" + encodeURIComponent(q))
        .then(function (data) {
          // Закъснял отговор от предишно търсене — изхвърля се, за да не
          // презапише резултата от по-новото (класически race при бавна
          // мрежа, където отговорите се разминават по ред).
          if (mine !== seq) return;
          var list = (data && data.clients) || [];
          mergeClients(list);
          renderOptions(list);
          if (!list.length) {
            status.textContent = t("client_search_none",
              "Няма намерени клиенти по това търсене.");
          } else if (data.truncated) {
            status.textContent = tf("client_search_truncated",
              "Показани са първите {count} съвпадения — уточнете търсенето.",
              { count: list.length });
          } else {
            status.textContent = tf("client_search_found",
              "Намерени: {count}", { count: list.length });
          }
        })
        .catch(function (err) {
          if (mine !== seq) return;
          // Менюто НЕ се пипа — операторът продължава да работи с това,
          // което вече вижда.
          status.textContent = err && err.sessionExpired
            ? SESSION_EXPIRED_MSG
            : t("client_search_failed",
                "Търсенето не успя (бавна или прекъсната връзка) — опитайте пак "
                + "или изберете от показаните клиенти.");
        });
    }, CLIENT_SEARCH_DEBOUNCE_MS);
  });
}

// Автоматично попълване от адресната книга.
// Селект с class="client-select" и data-target="префикс" попълва полетата
// с имена: префикс_name, префикс_address, ... от window.CLIENTS.
function bindClientSelect(select) {
  attachClientSearch(select);
  select.addEventListener("change", function () {
    var id = parseInt(select.value, 10);
    var client = (window.CLIENTS || []).find(function (c) { return c.id === id; });
    var p = select.dataset.target;
    // Дребни (одит): при "-- избери --" (празна опция) select.value е "",
    // parseInt("", 10) дава NaN, никой клиент не съвпада (NaN !== NaN) —
    // кодът преди поправката правеше `if (!client) return;` ТУК, без да
    // изчисти полетата, оставяйки данните на ПРЕДИШНО избрания клиент
    // видимо попълнени, макар падащото меню да показва "не е избран".
    // Сега при празен избор всички полета се ИЗЧИСТВАТ изрично.
    var map = client ? {
      name: client.name,
      address: client.address,
      city: [client.postcode, client.city].filter(Boolean).join(" "),
      country: client.country,
      eik: client.eik,
      vat: client.vat,
      phone: client.phone,
      contact: client.contact,
      email: client.email
    } : {
      name: "", address: "", city: "", country: "", eik: "", vat: "",
      phone: "", contact: "", email: ""
    };
    Object.keys(map).forEach(function (k) {
      var el = document.querySelector('[name="' + p + '_' + k + '"]');
      if (el && map[k] !== undefined) {
        el.value = map[k] || "";
        if (el.value) markAutofilled(el);
      }
    });
    // По избор: селект с data-autofill-country="поле" попълва И друго,
    // отделно поле (различно от {target}_country) само с държавата на
    // избрания клиент — напр. декларацията за двойна употреба показва
    // само поле "Държава на износ" (destination_country), без пълен блок
    // с адресни полета за получателя.
    if (select.dataset.autofillCountry) {
      var target = document.querySelector('[name="' + select.dataset.autofillCountry + '"]');
      if (target) target.value = client ? (client.country || "") : "";
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
  // Дребни (одит, достъпност): полетата на динамичните редове се
  // създаваха без name/id/aria-label — екранен четец обявяваше всяко
  // просто като „текстово поле, празно“, без връзка към заглавието на
  // колоната. Четем видимите <th> текстове (същите, които вижда зрящ
  // потребител) и ги ползваме за aria-label на всеки генериран <input>.
  var headerCells = table.querySelectorAll("thead th");

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

  // Редовете от първоначалното зареждане (редакция на документ) се
  // появяват без анимация — плавното появяване (.row-new в style.css) е
  // само за редове, добавени СЛЕД това („+ Добави ред“, палетна карта,
  // Excel), където операторът трябва да види какво точно се е добавило.
  var initialFillDone = false;

  function addRow(item) {
    item = item || {};
    var tr = document.createElement("tr");
    if (initialFillDone) tr.className = "row-new";
    var idxTd = document.createElement("td");
    idxTd.className = "row-idx";
    tr.appendChild(idxTd);
    columns.forEach(function (col, i) {
      var td = document.createElement("td");
      var input = document.createElement("input");
      input.type = "text";
      input.dataset.field = col;
      // +1 — thead има водеща <th>№</th> преди колоните с данни.
      var headerCell = headerCells[i + 1];
      input.setAttribute("aria-label", (headerCell && headerCell.textContent.trim()) || col);
      // Дребни (одит): `item[col] || rowDefaults[col] || ""` изяжда
      // числовата 0 — ако item[col] е ЧИСЛОТО 0 (не низ "0"), `0 || x`
      // дава x, защото 0 е falsy в JS: истинска въведена стойност "0"
      // (напр. количество/код) би се заменила мълчаливо с подразбиращата
      // се стойност. Проверяваме изрично за undefined/null/"", не за
      // falsy — 0 е валидна стойност, не липсваща.
      input.value = (item[col] !== undefined && item[col] !== null && item[col] !== "")
        ? item[col] : (rowDefaults[col] || "");
      td.appendChild(input);
      tr.appendChild(td);
    });
    var delTd = document.createElement("td");
    var delBtn = document.createElement("button");
    delBtn.type = "button";
    delBtn.textContent = "✕";
    delBtn.className = "btn-danger btn-small";
    delBtn.title = t("remove_row", "Премахни реда");
    delBtn.addEventListener("click", function () {
      tr.remove();
      renumber();
    });
    delTd.appendChild(delBtn);
    tr.appendChild(delTd);
    tbody.appendChild(tr);
    renumber();
    // Одит (29.08.2026, находка №6): известяваме, че е добавен ред.
    //
    // initPackingTotals вече слушаше точно за това събитие, но то НИКЪДЕ не
    // се излъчваше — мъртъв слушател. Затова след „Издърпай от палетна
    // карта“ (addRow се вика програмно, без input/change от потребител)
    // подсказките „Сбор от редовете“ под обобщаващите полета на опаковъчния
    // лист оставаха СТАРИ, докато операторът не пипне някоя клетка — тоест
    // точно проверката срещу разминаване мълчеше в момента, в който е
    // най-нужна. Излъчването на самото място на добавяне покрива всички
    // програмни пътища наведнъж (издърпване от палет, Excel импорт, бъдещи).
    if (initialFillDone && typeof CustomEvent === "function") {
      document.dispatchEvent(new CustomEvent("items-row-added"));
    }
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
        var field = inp.dataset.field;
        var value = inp.value.trim();
        item[field] = value;
        /* Одит (31.08.2026, находка №3, ВИСОКА): стойност, която още е
           равна на предварително попълнената по подразбиране, НЕ прави реда
           непразен.

           Трите фактурни таблици носят data-row-defaults
           '{"hs_code": "85389099"}', а addRow попълва тези стойности при
           създаването на реда. Затова „празен“ ред никога не беше празен:
           проверката гледаше само дали ВСИЧКИ полета са празни, а hs_code
           винаги имаше стойност. Проверено в реален браузър — нова фактура
           за Норвегия без нито един попълнен ред сериализираше
           [{"hs_code":"85389099","description":"", … }], редът се записваше
           и се отпечатваше като номерирана позиция „2 | 85389099 | | | |“
           на търговска фактура към чуждестранен клиент и митница.

           Ако операторът СЪЗНАТЕЛНО е въвел нещо другаде в реда, редът си
           остава непразен и подразбиращата се стойност пътува с него —
           затова сравняваме поле по поле, а не целия ред наведнъж. */
        if (value && value !== (rowDefaults[field] || "")) empty = false;
      });
      if (!empty) items.push(item);
    });
    return items;
  }

  (initialItems || []).forEach(addRow);
  if (!initialItems || !initialItems.length) addRow();
  initialFillDone = true;

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
  // Одит (находки К6/С1): parseDecimal (дефинирана по-долу — вдигната тук
  // чрез hoisting на function-декларации в JS) е СЪЩИЯТ стриктен разбор,
  // ползван и за фактурите, и на сървъра (appcore.pallet_total_qty) —
  // отхвърля „боклук“ след числото, nan/inf и отрицателни стойности,
  // вместо parseFloat да ги приема мълчаливо/различно от сървъра.
  //
  // Одит (19.08.2026, находка №9): сумирането минава през мащабиран BigInt
  // (scaleDecimalToBigInt/formatScaledSum), не през parseFloat +
  // Math.round. Старият път закръгляше половинката НАГОРЕ (0.0625 →
  // 0.063), докато сървърът форматираше float и я закръгляше към ЧЕТНО
  // (0.062) — операторът виждаше едно число, а издадената карта и Excel
  // износът твърдяха друго. Сега двете страни ползват идентична
  // ROUND_HALF_UP аритметика върху СУРОВИЯ текст, без междинен float.
  //
  // Одит (29.08.2026, находка №5): колонната сума минава през sumRawDecimals
  // — сумиране с пълна точност и ЕДНО закръгляне накрая, точно както
  // appcore.pallet_total_qty. Досега всяка стойност се закръгляше поотделно
  // на 3 знака преди сумата, затова два реда по „1.2345“ показваха 2.470 на
  // екрана срещу 2.469 на издадената карта.
  var text = sumRawDecimals((items || []).map(function (it) {
    return it && it.qty;
  }), 3, true);
  return text || "—";
}

/** Одит (19.08.2026, находка №8): жива подсказка „сборът на редовете е X“
 *  под всяко от четирите обобщаващи полета на опаковъчния лист.
 *
 *  Тези полета се въвеждат НА РЪКА и се печатат буквално в реда ОБЩО/TOTAL
 *  на бланката, която придружава ЧМР при митническо оформяне — за разлика
 *  от палетната карта („Общ брой“ се преизчислява винаги) и от фактурите
 *  (живи суми). Съзнателно НЕ попълваме полето автоматично: общото
 *  легитимно може да включва тара на палета/опаковка. Показваме сбора и
 *  оцветяваме подсказката при разминаване; сървърът предупреждава още
 *  веднъж при самото издаване. */
function initPackingTotals(form, tableApi) {
  var wrap = form.querySelector("[data-packing-totals]");
  if (!wrap || !tableApi) return;
  var hints = Array.prototype.slice.call(wrap.querySelectorAll("[data-packing-sum]"));
  if (!hints.length) return;

  function update() {
    var items = tableApi.collect();
    hints.forEach(function (hint) {
      var field = hint.dataset.packingSum;
      // Одит (29.08.2026, находка №5): същият ред на операциите като
      // appcore.packing_sum — пълна точност, едно закръгляне накрая. Иначе
      // подсказката „Сбор от редовете“ можеше да се разминава с проверката,
      // която сървърът прави при издаване (packing_total_mismatches), и да
      // предупреждава за разминаване, каквото реално няма (или обратното).
      var sum = sumRawDecimals(items.map(function (it) {
        return it && it[field];
      }), 3, true);
      var input = hint.parentNode.querySelector("input");
      var typed = input ? String(input.value || "").trim() : "";
      if (!sum) { hint.textContent = ""; hint.classList.remove("field-hint--warn"); return; }
      hint.textContent = tf("rows_sum", "Сбор от редовете: {sum}", { sum: sum });
      var typedScaled = scaleDecimalToBigInt(typed, 3);
      var typedText = typedScaled === null ? "" : formatScaledSum([typedScaled], 3, true);
      hint.classList.toggle("field-hint--warn", typed !== "" && typedText !== sum);
    });
  }

  var table = form.querySelector("table.items");
  if (table) {
    table.addEventListener("input", update);
    table.addEventListener("change", update);
    /* Одит (31.08.2026, находка №16): ИЗТРИВАНЕТО на ред с ✕ не излъчва
       нито input, нито change, нито „items-row-added“ — подсказките
       „Сбор от редовете“ оставаха на старите суми и не предупреждаваха за
       разминаване, а сървърът после противоречеше при издаване.
       setTimeout(…, 0) — точно както bindPalletQtyTotal и bindInvoiceTotals
       — за да се прочете таблицата СЛЕД като редът вече е премахнат. */
    table.addEventListener("click", function () { setTimeout(update, 0); });
  }
  wrap.addEventListener("input", update);
  document.addEventListener("items-row-added", update);
  update();
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
function openPalletTypeModal(callback, returnFocusTo) {
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
  // Одит (19.08.2026, находка №33): началният фокус е в полето „Дължина“
  // (тук единственото смислено начало — модалът съществува само за да се
  // въведат две числа), Tab се затваря в кръг в модала, фонът става inert,
  // а при затваряне фокусът се връща на падащото меню „Тип палет“, от
  // което модалът е отворен — иначе следващият Tab тръгва от началото на
  // страницата и операторът се озовава в съвсем друга форма.
  var deactivate = activateModal(modal, lengthInput, returnFocusTo);

  function finish(result) {
    modal.style.display = "none";
    confirmBtn.removeEventListener("click", onConfirm);
    cancelBtn.removeEventListener("click", onCancel);
    closeBtn.removeEventListener("click", onCancel);
    modal.removeEventListener("keydown", onKeydown);
    if (deactivate) { deactivate(); deactivate = null; }
    callback(result);
  }
  function onConfirm() {
    var l = lengthInput.value.trim();
    var w = widthInput.value.trim();
    if (!l || !w) { (l ? widthInput : lengthInput).focus(); return; }
    finish(l + "×" + w);
  }
  function onCancel() { finish(null); }
  // Одит (16.08.2026, находка №8, висока — довършва поправката за
  // изнасяне на модала извън <form>, виж pallet_bulk_review.html): дори
  // извън формата, Enter в текстово поле по подразбиране не прави нищо
  // без изричен обработчик — потребителите естествено натискат Enter след
  // попълване, очаквайки „Прилагане“ (както confirm/camera модалите).
  // Escape затваря (=Отказ), огледално на другите модали в приложението.
  function onKeydown(e) {
    if (e.key === "Enter") { e.preventDefault(); onConfirm(); }
    else if (e.key === "Escape") { e.preventDefault(); onCancel(); }
  }
  confirmBtn.addEventListener("click", onConfirm);
  cancelBtn.addEventListener("click", onCancel);
  closeBtn.addEventListener("click", onCancel);
  modal.addEventListener("keydown", onKeydown);
}

// Общ помощник за <select> с фиксиран списък опции, който все пак трябва
// да покаже КОРЕКТНО стойност, записана преди списъкът да е съществувал
// (стар/друг формат) — вместо select.value=... тихо да не избере нищо,
// добавя липсващата стойност като допълнителна опция и чак тогава я
// избира. Ползва се и за "Тип палет" (модал "Друг"), и за "Вид транспорт"
// на фактурите за Бразилия (заявка за падащо меню — стари фактури,
// издадени преди то да съществува, може да пазят свободен текст, различен
// от двата стандартни варианта).
function injectAndSelectOption(select, value) {
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
  // Одит (29.08.2026, находка №2): програмното задаване осведомява менюто за
  // размери коя е текущата „добра“ стойност — иначе „Отказ“ в диалога за
  // „Друг…“ би върнал застояла (подразбираща се) стойност. За останалите
  // селекти извикването е безобидно (само записва data-prev-value).
  rememberPalletTypeValue(select);
}

/* Одит (29.08.2026, находка №2, средна): „последната добра стойност“ на
   менюто за размери (тази, към която се връщаме при „Отказ“) живее в САМИЯ
   елемент (data-prev-value), а не в затворена променлива.

   Защо: на формата за РЕДАКЦИЯ initPalletTypeSelect се връзва, докато менюто
   още показва подразбиращата се първа опция (120×80) — истинската запазена
   стойност се налага ПРОГРАМНО чак след това, и то по ДВА пътя
   (injectAndSelectOption и prefillForm в initDocumentForm), и двата с директно
   `.value = …`, БЕЗ `change` събитие. Затова затвореният prevValue оставаше
   „120×80“, а „Друг / Other… → Отказ“ подменяше размера на палета с
   подразбиращия се — тихо. Ако операторът не забележеше и натиснеше „Запази
   промените“, размерите в официалния документ се променяха. (Формата за НОВ
   документ и bulk прегледът не бяха засегнати: там стойността идва през
   change, съответно сървърно със `selected`.)

   Понеже стойността стои на елемента, всеки програмен път може да я поднови
   (виж rememberPalletTypeValue по-долу и извикванията ѝ) — включително бъдещ,
   който още не съществува. */
function rememberPalletTypeValue(select) {
  if (select && select.value && select.value !== "__other__") {
    select.dataset.prevValue = select.value;
  }
}

function initPalletTypeSelect(select) {
  if (!select || select.dataset.otherBound) return;
  select.dataset.otherBound = "1";
  rememberPalletTypeValue(select);
  /* Втора защита към подновяването при програмно задаване: снимаме и в
     момента на ДОКОСВАНЕ — човек няма как да смени <select> без да го
     фокусира/натисне, така че това покрива и мишка, и клавиатура. */
  function snapshotPrevValue() { rememberPalletTypeValue(select); }
  select.addEventListener("mousedown", snapshotPrevValue);
  select.addEventListener("keydown", snapshotPrevValue);
  select.addEventListener("focus", snapshotPrevValue);
  select.addEventListener("change", function () {
    if (select.value === "__other__") {
      openPalletTypeModal(function (dims) {
        if (dims) { injectAndSelectOption(select, dims); }
        else { select.value = select.dataset.prevValue || select.options[0].value; }
      // Одит (19.08.2026, находка №33): фокусът се връща изрично на СЪЩОТО
      // падащо меню — то е „бутонът“, отворил модала, и от него операторът
      // продължава нататък по формата.
      }, select);
    } else {
      rememberPalletTypeValue(select);
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
      // Одит (19.08.2026, находка №13): заглавието на картата беше твърдо
      // двуезично („Карта N / Card N“); сега минава през речника — на БГ
      // остава точно същото, на EN/TR е само съответният език.
      if (title) title.textContent = multi
        ? (tf("pallet_card_title", "Карта {n} / Card {n}", { n: i + 1 }) + " — ")
        : "";
      var removeBtn = block.querySelector(".pallet-card-remove");
      if (removeBtn) removeBtn.style.display = multi ? "" : "none";
      var noInput = block.querySelector('[data-field="pallet_no"]');
      if (noInput) {
        if (multi) {
          noInput.readOnly = true;
          noInput.value = palletNoOf(i + 1, blocks.length);
        } else {
          noInput.readOnly = false;
          if (palletNoOfPattern().test(noInput.value)) noInput.value = "";
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
  initPackingTotals(form, itemsTables["packing-items"]);
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
      if (ptSelect && editData.pallet_type) injectAndSelectOption(ptSelect, editData.pallet_type);
      // "Вид транспорт" е падащо меню само за фактурите за Бразилия
      // (select[name="transport_way"]) — за Норвегия е свободен текст и
      // този селектор просто не намира нищо. Стара фактура, издадена
      // преди менюто да съществува, може да пази стойност извън двата
      // стандартни варианта — inject-ва се, за да не изчезне тихо.
      var twSelect = form.querySelector('select[name="transport_way"]');
      if (twSelect && editData.transport_way) injectAndSelectOption(twSelect, editData.transport_way);
      // Одит (находка В8, висок риск): "Вид на опаковката" (ЧМР) и "Вид
      // опаковка" (палетна карта) НЯМАТ value= атрибут на <option>-ите
      // си — браузърът ползва самия ПРЕВЕДЕН текст като стойност.
      // Записан документ на български ("Палети") пази точно този низ; ако
      // потребителят после смени езика на интерфейса (нова заявка, нов
      // render на <option>-ите вече на английски "Pallets"), select.value
      // = "Палети" тихо не намира съвпадение и полето изглежда изчистено
      // — макар записаната стойност в базата да си е СЪЩАТА. Същата
      // inject-and-select техника като pallet_type/transport_way по-горе.
      var packingSelect = form.querySelector('select[name="packing"]');
      if (packingSelect && editData.packing) injectAndSelectOption(packingSelect, editData.packing);
      var packagingTypeSelect = form.querySelector('select[name="packaging_type"]');
      if (packagingTypeSelect && editData.packaging_type)
        injectAndSelectOption(packagingTypeSelect, editData.packaging_type);
      prefillForm(form, editData);
      // Одит (29.08.2026, находка №2): prefillForm задава `el.value` направо,
      // без `change` — затова подновяваме „последната добра стойност“ ИЗРИЧНО
      // след него, вместо менюто да остане с подразбиращата се. Без този ред
      // „Друг / Other… → Отказ“ при редакция подменяше размера на палета тихо.
      rememberPalletTypeValue(ptSelect);
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
      placeholder.textContent = t("select_unload_point",
        "— избери пункт за разтоварване (по клиента получател) —");
      unloadSelect.appendChild(placeholder);
      if (!client) return;
      var mainAddr = fmtAddress(client);
      if (mainAddr) {
        var mainOpt = document.createElement("option");
        mainOpt.value = mainAddr;
        mainOpt.textContent = tf("central_address", "Централен адрес — {addr}", { addr: mainAddr });
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

// ---------------------------------------------------------------- ЧМР: побиране в един лист
// Одит (19.08.2026, находка №11): официалната ЧМР бланка е ЕДНОСТРАНИЧНА,
// а при по-дълго описание на стоката (поле 6–9 е свободен текст) печатът
// ставаше на 2 страници — на втория лист оставаха само кутиите с подписите
// 22/23/24, без рамки и без номер на документа, а при „4 екземпляра“ това
// са 8 листа половин-бланки.
//
// Тук се избира най-едрата „степен на побиране“ (.cmr-fit-1…5 в
// style.css, само размери на текст/отстъпи — подредбата не се пипа), при
// която бланката се събира във височината на листа. Измерва се на екрана,
// но резултатът важи и за хартията, защото ШИРИНАТА на съдържанието е
// една и съща (194мм: .print-page е 210мм с 8мм отстъп на екран, а при
// печат заема точно печатното поле на A4 при 8мм полета от @page) —
// височината е единственото, което се променя. За времето на измерването
// класът .cmr-measuring неутрализира min-height-а на страницата и скрива
// .no-print елементите (подсказката под QR кода), за да не се мери нещо,
// което на хартия няма да го има.
//
// Ако дори последната степен не стигне, документът СЪЗНАТЕЛНО прелива на
// втора страница — нищо не се изрязва (виж находка К5: мълчаливото
// изрязване е по-лошият дефект).
var CMR_FIT_LEVELS = 5;

function fitCmrPages() {
  var pages = document.querySelectorAll(".cmr-page");
  if (!pages.length) return;
  var PX_PER_MM = 96 / 25.4;
  // 268мм е височината, на която style.css фиксира самата бланка при печат
  // (`@media print { .cmr-page { min-height: 268mm } }` — внимателно
  // настроената височина, при която бланката запълва листа A4 при 8мм
  // полета). Съдържание, което не се събира в тези 268мм, ПРЕЛИВА извън
  // кутията (`.cmr-grid` е flex:1 с min-height:0 — свива се под своя
  // min-content и излишъкът излиза навън) и точно това преливане — понякога
  // само няколко десети от милиметъра — ражда втората страница. Затова
  // мерим срещу 268мм, не срещу цялото печатно поле, и оставяме 1мм резерв
  // за закръгленията на печатния движок (същият ефект „косъм под ръба“,
  // доказан при находка В9 за етикетния формат).
  var limitPx = (268 - 1) * PX_PER_MM;
  Array.prototype.forEach.call(pages, function (page) {
    var cmr = page.querySelector(".cmr");
    if (!cmr) return;
    page.classList.add("cmr-measuring");
    var chosen = 0;
    for (var level = 0; level <= CMR_FIT_LEVELS; level++) {
      for (var i = 1; i <= CMR_FIT_LEVELS; i++) {
        page.classList.toggle("cmr-fit-" + i, i === level);
      }
      chosen = level;
      if (cmr.getBoundingClientRect().height <= limitPx) break;
    }
    page.classList.remove("cmr-measuring");
    page.dataset.cmrFit = String(chosen);
  });
}

function initCmrPrintFit() {
  if (!document.querySelector(".cmr-page")) return;
  fitCmrPages();
  // Логото на фирмата (.doc-logo) е обикновено <img> БЕЗ размери в
  // атрибути — до зареждането му заглавната част е по-ниска, отколкото ще
  // бъде на хартия, затова премерваме отново след `load`. Печат преди
  // това е практически невъзможен (страницата се отваря и чак после се
  // натиска „Печат“), но повторното измерване е евтино и премахва цялата
  // зависимост от реда на зареждане.
  window.addEventListener("load", fitCmrPages);
  // НЯМА преизмерване на `beforeprint`: това събитие се задейства, когато
  // страницата вече е в печатен режим, а тогава .print-page е `width: auto`
  // и се разпъва по ШИРИНАТА НА ПРОЗОРЕЦА (обикновено много по-широка от
  // 194мм) — описанието на стоката се пренася на по-малко редове, височината
  // излиза измамно ниска и се избира прекалено едра степен. Доказано:
  // headless печатът (page.pdf) също вдига beforeprint и точно това връщаше
  // ЧМР с 400 знака обратно на 2 страници. Измерването при зареждане, докато
  // страницата е с фиксирана ширина 210мм, е единственото достоверно.
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
    msg.textContent = t("searching", "Търсене…");
    var body = new URLSearchParams();
    body.set("code", code);
    body.set("csrf_token", csrfInput ? csrfInput.value : "");
    fetchJsonSafe(btn.dataset.url, { method: "POST", body: body })
      .then(function (data) {
        if (data.ok) {
          tableApi.addRow(data.row);
          // Одит (находка С12): "note" (напр. „нето тегло не се пази в
          // палетната карта — попълнете го ръчно“) обяснява ЗАЩО полето
          // „Нето, кг“ идва празно, вместо операторът да реши, че е грешка.
          msg.textContent = tf("pallet_row_added", "Добавен ред от палетна карта № {number}.",
            { number: data.number }) + (data.note ? " " + data.note : "");
          input.value = "";
          input.focus();
        } else {
          msg.textContent = data.error || t("generic_error", "Грешка.");
        }
      })
      .catch(function (err) {
        msg.textContent = (err && err.sessionExpired)
          ? SESSION_EXPIRED_MSG : t("request_error", "Грешка при заявката.");
      });
  }

  btn.addEventListener("click", pull);
  input.addEventListener("keydown", function (e) {
    if (e.key === "Enter") { e.preventDefault(); pull(); }
  });
}

// ---------------------------------------------------------------- фактури
// (invoice_br_form.html / invoice_no_form.html / invoice_dubai_form.html).
// Три отделни поведения, всичките задвижени от data-* атрибути на самата
// таблица, за да работят еднакво и за трите фактури, въпреки че колоните
// им се различават:
//
//  1) автоматично попълване на теглото (Бразилия) или описанието
//     (Норвегия) от справочника материали по въведения Material code —
//     кое поле се пълни идва от data-lookup-fill;
//  2) живи суми под таблицата (общо количество/стойност/тегло) — същата
//     сметка като на сървъра (appcore.invoice_totals), за да няма изненада
//     между формата и готовата бланка;
//  3) зареждане на ВСИЧКИ редове от вече издадена палетна карта.

// Одит (находки К6/С1): ЕДИНЕН, стриктен формат за десетично число — САМО
// цифри и НАЙ-МНОГО ЕДИН десетичен разделител (запетая ИЛИ точка), без
// разделител на хилядите (двусмислен без locale) и БЕЗ nan/inf (структурно
// изключени, защото regex-ът позволява само цифри). Точно СЪЩИЯТ regex
// (буква по буква) е приложен и на сървъра — вижте appcore._DECIMAL_RE —
// за да не могат живата сума на екрана и записаният в документа резултат
// да излязат РАЗЛИЧНИ числа за един и същ въведен текст: преди тази
// поправка `parseFloat` тук четеше само водещите цифри и мълчаливо
// пренебрегваше остатъка (напр. „12 кг“ → 12), докато Python изискваше
// ЦЯЛОТО поле да е валиден литерал — резултат: екранът показваше сума, а
// готовата фактура излизаше с ПРАЗНА клетка за същия ред.
var DECIMAL_RE = /^-?\d+([.,]\d+)?$/;

function parseDecimal(value) {
  if (value === undefined || value === null) return null;
  var text = String(value).trim().replace(/\s+/g, "");
  if (!text || !DECIMAL_RE.test(text)) return null;
  var n = parseFloat(text.replace(",", "."));
  if (isNaN(n) || n < 0) return null;  // отрицателно кол-во/цена/тегло няма смисъл тук
  return n;
}

// Запазено старо име (ползвано другаде в тази секция) — same function.
function invoiceNumber(value) { return parseDecimal(value); }

function invoiceFmt(value, decimals) {
  if (value === null) return "";
  var text = value.toFixed(decimals === undefined ? 2 : decimals);
  if (text.indexOf(".") >= 0) text = text.replace(/0+$/, "").replace(/\.$/, "");
  return text || "0";
}

// ---------------------------------------------------------------- точна
// парична аритметика (BigInt, без двоично приближение на float) — одит,
// находка С1: IEEE754 double смята 0.145 като 0.1449999999999999..., затова
// обикновено `(qty*price).toFixed(2)` закръгля НАДОЛУ вместо очакваното
// "училищно" закръгляне нагоре при .5 (напр. 7×0.145 → "1.01" вместо 1.02).
// Тук умножаваме directно суровите текстови низове като цели числа
// (мащабирани с 10^decimals), СЪЩИЯТ резултат, който сървърът смята с
// decimal.Decimal (appcore._parse_decimal_exact/_fmt_money) — за да не се
// разминават живата сума на екрана и готовата фактура.
function _scaledBigInt(text, scale) {
  var dot = text.indexOf(".");
  var intPart = dot >= 0 ? text.slice(0, dot) : text;
  var fracPart = dot >= 0 ? text.slice(dot + 1) : "";
  fracPart = (fracPart + "0000000000").slice(0, scale);
  return BigInt((intPart || "0") + fracPart);
}

/** Умножение на два СУРОВИ текстови входа (директно от полетата на реда,
 *  НЕ през parseFloat) — връща BigInt, мащабирано с 10^decimals,
 *  закръглено ROUND_HALF_UP, или null ако някой от двата текста не е
 *  валидно неотрицателно десетично число. */
function multiplyDecimalScaled(rawA, rawB, decimals) {
  var target = decimals === undefined ? 2 : decimals;
  var textA = String(rawA == null ? "" : rawA).trim().replace(/\s+/g, "");
  var textB = String(rawB == null ? "" : rawB).trim().replace(/\s+/g, "");
  if (!DECIMAL_RE.test(textA) || !DECIMAL_RE.test(textB)) return null;
  if (textA.charAt(0) === "-" || textB.charAt(0) === "-") return null;
  var na = textA.replace(",", "."), nb = textB.replace(",", ".");
  var scaleA = na.indexOf(".") >= 0 ? na.length - na.indexOf(".") - 1 : 0;
  var scaleB = nb.indexOf(".") >= 0 ? nb.length - nb.indexOf(".") - 1 : 0;
  var product = _scaledBigInt(na, scaleA) * _scaledBigInt(nb, scaleB);
  var scale = scaleA + scaleB;
  if (scale > target) {
    var divisor = 10n ** BigInt(scale - target);
    product = (product + divisor / 2n) / divisor;  // ROUND_HALF_UP (неотрицателни числа)
  } else if (scale < target) {
    product = product * (10n ** BigInt(target - scale));
  }
  return product;
}

/** Одит (19.08.2026, находка №9): една ЕДИНИЧНА десетична стойност като
 *  мащабиран BigInt, със същото ROUND_HALF_UP закръгляне като
 *  multiplyDecimalScaled. Нужна е за СУМАРНИТЕ количества/тегла, които
 *  досега минаваха през parseFloat + Math.round/toFixed — три различни
 *  режима на закръгляне между палетния екран, фактурния екран и Python,
 *  тоест едно число на екрана и друго в издадения документ. */
function scaleDecimalToBigInt(raw, decimals) {
  var target = decimals === undefined ? 3 : decimals;
  var text = String(raw == null ? "" : raw).trim().replace(/\s+/g, "");
  if (!DECIMAL_RE.test(text)) return null;
  if (text.charAt(0) === "-") return null;  // отрицателните се пропускат, както в Python
  var n = text.replace(",", ".");
  var scale = n.indexOf(".") >= 0 ? n.length - n.indexOf(".") - 1 : 0;
  var scaled = _scaledBigInt(n, scale);
  if (scale > target) {
    var divisor = 10n ** BigInt(scale - target);
    scaled = (scaled + divisor / 2n) / divisor;  // ROUND_HALF_UP (неотрицателни)
  } else if (scale < target) {
    scaled = scaled * (10n ** BigInt(target - scale));
  }
  return scaled;
}

/** Сумата на няколко вече мащабирани BigInt стойности (multiplyDecimalScaled),
 *  форматирана като десетичен текст с `decimals` знака.
 *
 *  `trimZeros` (одит 19.08.2026, находка №45): маха завършващите нули, за
 *  да съвпада с appcore._fmt_amount_exact — цялото приложение показва
 *  количества/тегла като „6.25“, а живата сума под таблицата с редове
 *  единствена изписваше „6.250“. За ПАРИ се ползва БЕЗ този флаг
 *  (счетоводно „1234.50 €“, не „1234.5 €“). */
function formatScaledSum(scaledValues, decimals, trimZeros) {
  var target = decimals === undefined ? 2 : decimals;
  var sum = 0n;
  var any = false;
  scaledValues.forEach(function (v) {
    if (v !== null && v !== undefined) { sum += v; any = true; }
  });
  if (!any) return "";
  var s = sum.toString().padStart(target + 1, "0");
  var intStr = s.slice(0, s.length - target) || "0";
  var fracStr = target > 0 ? s.slice(s.length - target) : "";
  var out = fracStr ? intStr + "." + fracStr : intStr;
  if (trimZeros && out.indexOf(".") >= 0) {
    out = out.replace(/0+$/, "").replace(/\.$/, "");
  }
  return out || "0";
}

/** Одит (29.08.2026, находка №5): сума на КОЛОНА сурови стойности с точно
 *  същия ред на операциите като сървъра — сумиране с ПЪЛНА точност и
 *  закръгляне САМО НАКРАЯ.
 *
 *  Разликата има значение: за редовите ПРОИЗВЕДЕНИЯ (цена = кол.×ед.цена,
 *  тегло) и двете страни съзнателно закръглят всеки ред и после сумират —
 *  защото на бланката се вижда всеки ЗАКРЪГЛЕН ред и общото трябва да е
 *  сборът на видимите редове. Но за проста колонна сума (общо количество,
 *  опаковъчни нето/бруто/обем) бланката показва въведеното КАКТО Е, тоест
 *  няма закръглен ред, към който да се равняваме — там авторитетът е
 *  сървърът (appcore.pallet_total_qty/packing_sum/invoice_totals), който
 *  трупа decimal.Decimal с пълна точност и квантува веднъж накрая.
 *
 *  Досега JS закръгляше ВСЯКА стойност на 3 знака преди сумата, затова два
 *  реда по „1.2345“ даваха 2.470 на екрана и 2.469 на издадения документ,
 *  в Excel и в PDF — операторът виждаше едно, а официалната бланка твърдеше
 *  друго (проверено с изпълнение). */
function sumRawDecimals(rawValues, decimals, trimZeros) {
  var target = decimals === undefined ? 3 : decimals;
  var parsed = [];
  var maxScale = 0;
  (rawValues || []).forEach(function (raw) {
    var text = String(raw == null ? "" : raw).trim().replace(/\s+/g, "");
    if (!DECIMAL_RE.test(text)) return;
    if (text.charAt(0) === "-") return;  // отрицателните се пропускат, както в Python
    var n = text.replace(",", ".");
    var scale = n.indexOf(".") >= 0 ? n.length - n.indexOf(".") - 1 : 0;
    if (scale > maxScale) maxScale = scale;
    parsed.push(n);
  });
  if (!parsed.length) return "";
  /* Сумата се трупа при НАЙ-ГОЛЯМАТА срещната точност — нищо не се губи. */
  var sum = 0n;
  parsed.forEach(function (n) { sum += _scaledBigInt(n, maxScale); });
  /* И чак сега — едно-единствено закръгляне ROUND_HALF_UP до target знака,
     точно както Decimal.quantize(ROUND_HALF_UP) на сървъра. */
  if (maxScale > target) {
    var divisor = 10n ** BigInt(maxScale - target);
    sum = (sum + divisor / 2n) / divisor;
  } else if (maxScale < target) {
    sum = sum * (10n ** BigInt(target - maxScale));
  }
  return formatScaledSum([sum], target, trimZeros);
}

/** Попълва поле на реда от справочника материали, само ако е ПРАЗНО —
 *  вече въведена ръчно стойност никога не се презаписва автоматично. */
function bindInvoiceMaterialLookup(table) {
  var url = table.dataset.lookupUrl;
  var fillField = table.dataset.lookupFill;
  if (!url || !fillField) return;

  var cache = {};

  function fillRow(tr, code, lookupFailed) {
    var target = tr.querySelector('input[data-field="' + fillField + '"]');
    if (!target || target.value.trim()) return;  // ръчното въведено печели
    // Одит (находка С6, среден риск): преди поправката .catch тук беше
    // НАПЪЛНО празен — при мрежова грешка/изтекла сесия полето просто
    // оставаше празно, неразличимо от "проверихме, материалът наистина
    // няма зададено тегло/описание в справочника" — операторът може да
    // реши второто и да издаде фактура с празно тегло. Тук поне маркираме
    // полето видимо (пунктирана рамка + подсказка при задържане), за да е
    // ясно, че трябва да се провери/попълни РЪЧНО.
    target.classList.remove("lookup-failed");
    target.removeAttribute("title");
    if (lookupFailed) {
      target.classList.add("lookup-failed");
      target.title = t("material_lookup_failed",
        "Проверката в справочника материали не бе успешна " +
        "(мрежова грешка или изтекла сесия) — попълнете стойността ръчно.");
      return;
    }
    var entry = cache[code.toUpperCase()];
    if (entry === undefined) return;
    if (entry && entry[fillField]) {
      target.value = entry[fillField];
      markAutofilled(target);
    }
  }

  table.addEventListener("change", function (e) {
    var input = e.target;
    if (!input.dataset || input.dataset.field !== "material_code") return;
    var code = input.value.trim();
    if (!code) return;
    var tr = input.closest("tr");
    var key = code.toUpperCase();
    if (cache[key] !== undefined) { fillRow(tr, code); return; }
    fetchJsonSafe(url + "?code=" + encodeURIComponent(code))
      .then(function (data) {
        cache[key] = data.ok ? data : null;
        fillRow(tr, code);
      })
      .catch(function () { fillRow(tr, code, true); });
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
    // Одит (16.08.2026, находка №43): "Общо количество: 0" се показваше
    // дори когато НИТО един ред няма валидно количество, защото
    // invoiceFmt(0, ...) връща низа "0" (истинска стойност в JS —
    // `"0" || "—"` дава "0", не "—"; само празният низ "" е falsy).
    // hasQtyValue проследява дали поне ЕДИН ред реално е допринесъл —
    // теглото вече не се нуждае от аналогичен флаг (виж находка №17
    // по-долу — formatScaledSum вече връща "" сама, ако няма нито един
    // валиден принос, точно както при цената).
    var hasQtyValue = false;
    var scaledRowPrices = [];
    var scaledRowWeights = [];
    // Одит (19.08.2026, находка №9): и количеството минава през BigInt —
    // виж sumQtyForDisplay по-горе за пълното обяснение.
    // Одит (29.08.2026, находка №5): количеството е проста КОЛОННА сума —
    // сървърът (invoice_totals) я трупа с пълна точност и закръгля веднъж,
    // затова тук пазим суровите текстове и ги подаваме на sumRawDecimals
    // по-долу, вместо да закръгляме всеки ред поотделно. (Цената и теглото
    // са редови ПРОИЗВЕДЕНИЯ и си остават закръглени на ред — така е и на
    // сървъра, и така се равняват на видимите редове на бланката.)
    var rawRowQty = [];
    items.forEach(function (it) {
      var qty = invoiceNumber(it.qty);
      if (qty !== null) { hasQtyValue = true; }
      rawRowQty.push(it.qty);
      // Одит (16.08.2026, находка №17): огледално на цената по-долу —
      // тегло×количество вече минава през СЪЩАТА ТОЧНА (BigInt, не
      // двоичен float) аритметика вместо предишното `(qty*weight).
      // toFixed(3)` — при стойност точно на границата (x.xxx5) JS-кото
      // toFixed и Python-овото форматиране на float МОЖЕХА да закръглят в
      // различни посоки (виж appcore._GRAMS/invoice_row_weight), затова
      // живата сума тук и готовата бланка можеха да покажат различни
      // числа за ЕДНИ И СЪЩИ въведени стойности.
      scaledRowWeights.push(multiplyDecimalScaled(it.qty, it.net_weight, 3));
      // Сумата на цената се трупа от ТОЧНИ (BigInt, не двоичен float) редови
      // произведения — вижте multiplyDecimalScaled по-горе — СЪЩИЯТ резултат,
      // който сървърът смята (appcore.invoice_row_total/invoice_totals),
      // за да не се разминава живата сума тук с готовата бланка.
      scaledRowPrices.push(multiplyDecimalScaled(it.qty, it.unit_price, 2));
    });
    var totalPriceText = formatScaledSum(scaledRowPrices, 2);
    // trimZeros=true (находка №45): „6.25“, не „6.250“ — както навсякъде
    // другаде в приложението и както appcore._fmt_amount_exact на сървъра.
    var totalWeightText = formatScaledSum(scaledRowWeights, 3, true);
    var totalQtyText = sumRawDecimals(rawRowQty, 3, true);
    // Одит (22.08.2026, находка №11): преди тази поправка редът беше
    // `box.innerHTML = parts.join(" · ")`, а самите `parts` се сглобяваха
    // чрез конкатенация на t(…) с "<b>". Числата са безопасни (идват от
    // собствената ни аритметика), но ПРЕВОДИТЕ влизаха в документа като
    // HTML — това беше ЕДИНСТВЕНОТО място в целия файл, където низ от
    // t() стига до innerHTML. Достатъчно е бъдещ турски/английски превод
    // да съдържа „<“ (напр. „< 1 кг“) и изгледът се чупи мълчаливо, а
    // низовете идват от .po файл, който не минава през никаква HTML
    // проверка. Сглобяваме с createElement/textContent, както навсякъде
    // другаде в app.js — теглото/сумата остават в <b>, но като ЕЛЕМЕНТ,
    // не като текст с ъглови скоби.
    var parts = [
      [t("summary_rows", "Редове"), String(items.length)],
      [t("summary_total_qty", "Общо количество"),
       hasQtyValue ? (totalQtyText || "—") : "—"],
      [t("summary_total_value", "Обща стойност"), (totalPriceText || "—") + " €"],
    ];
    if (hasWeight) {
      parts.push([t("summary_total_net", "Общо нето тегло"),
                  (totalWeightText || "—") + " " + t("unit_kg", "кг")]);
    }
    box.textContent = "";
    parts.forEach(function (part, i) {
      if (i) { box.appendChild(document.createTextNode(" · ")); }
      box.appendChild(document.createTextNode(part[0] + ": "));
      var strong = document.createElement("b");
      strong.textContent = part[1];
      box.appendChild(strong);
    });
  }

  table.addEventListener("input", update);
  table.addEventListener("click", function () { setTimeout(update, 0); });
  update();
  return update;
}

/** Етикет на група по поръчка — празната група са редовете без P.O NO. */
/** Пише статус под импортите с цвят по изхода: „ok“ зелено, „err“
 *  червено, без вид — неутрално сиво (напр. „Търсене…“). Виж
 *  .msg-ok/.msg-err в style.css — дотук успех и грешка изглеждаха
 *  еднакво и грешката лесно се пропускаше. */
function setImportMsg(msg, textContent, kind) {
  msg.classList.remove("msg-ok", "msg-err");
  if (kind === "ok") msg.classList.add("msg-ok");
  if (kind === "err") msg.classList.add("msg-err");
  msg.textContent = textContent;
}

function invoicePoLabel(po) {
  return po ? po : t("po_none", "(редове без поръчка №)");
}

/** Показва избор „коя поръчка да заредя“ — сървърът е върнал choose_po,
 *  защото източникът (палетна карта/Excel файл) съдържа редове от НЯКОЛКО
 *  поръчки, а една фактура се издава за ЕДНА (заявка: „във фактури един
 *  номер на поръчка да бъде на една фактура“). loadFn(po) презарежда със
 *  same източник, но само за избраната поръчка. */
function renderInvoicePoChoice(msg, data, loadFn) {
  msg.textContent = "";
  var note = document.createElement("div");
  note.textContent = tf("po_choose",
    "Източникът съдържа {count} различни поръчки — една фактура се издава за ЕДНА поръчка."
    + " Изберете коя да заредите тук; за останалите издайте отделни фактури:",
    { count: data.pos.length });
  msg.appendChild(note);

  var select = document.createElement("select");
  select.style.maxWidth = "340px";
  data.pos.forEach(function (p) {
    var opt = document.createElement("option");
    opt.value = p.po_no;
    opt.textContent = invoicePoLabel(p.po_no) + " — " +
      tf("po_rows_count", "{count} реда", { count: p.count });
    select.appendChild(opt);
  });
  var pick = document.createElement("button");
  pick.type = "button";
  pick.className = "btn-secondary btn-small";
  pick.style.marginLeft = "8px";
  pick.textContent = t("po_load", "Зареди тази поръчка");
  pick.addEventListener("click", function () { loadFn(select.value); });

  var rowEl = document.createElement("div");
  rowEl.style.marginTop = "6px";
  rowEl.appendChild(select);
  rowEl.appendChild(pick);
  msg.appendChild(rowEl);
}

/** Съобщението след успешно зареждане + напомняне за останалите поръчки,
 *  които трябва да отидат на отделни фактури. */
function invoiceLoadedMessage(baseText, data) {
  var text = baseText;
  if (data.matched < data.count) {
    text += " " + tf("lookup_partial",
      "Тегло/описание от справочника е намерено за {matched} от тях — останалите попълнете ръчно.",
      { matched: data.matched });
  }
  if (data.remaining && data.remaining.length) {
    var parts = data.remaining.map(function (p) {
      return invoicePoLabel(p.po_no) + " (" +
        tf("po_rows_count", "{count} реда", { count: p.count }) + ")";
    });
    text += " " + tf("po_remaining", "ОСТАВАТ ЗА ОТДЕЛНИ ФАКТУРИ: {list}.",
      { list: parts.join(", ") });
  }
  return text;
}

/** Зарежда редовете на издадена палетна карта във фактурата — за разлика
 *  от initPullFromPallet (опаковъчен лист), който добавя един обобщен
 *  ред. При карта с няколко поръчки първо се избира коя (choose_po). Виж
 *  routes_invoices.invoice_pull_pallet за съответствието между колоните. */
function bindInvoicePullPallet(box, tableApi, onChanged) {
  var btn = box.querySelector(".invoice-pull-btn");
  var input = box.querySelector(".invoice-pull-code");
  var msg = box.querySelector(".invoice-pull-msg");
  if (!btn || !input || !tableApi) return;
  var form = btn.closest("form");
  var csrfInput = form ? form.querySelector('[name="csrf_token"]') : null;

  function pull(poNo) {
    var code = input.value.trim();
    if (!code) return;
    setImportMsg(msg, t("searching", "Търсене…"));
    var body = new URLSearchParams();
    body.set("code", code);
    body.set("csrf_token", csrfInput ? csrfInput.value : "");
    if (poNo !== undefined) body.set("po_no", poNo);
    fetchJsonSafe(btn.dataset.url, { method: "POST", body: body })
      .then(function (data) {
        if (!data.ok) { setImportMsg(msg, data.error || t("generic_error", "Грешка."), "err"); return; }
        if (data.choose_po) {
          // НЕ чистим input — повторното зареждане чете същия номер оттам.
          setImportMsg(msg, "");
          renderInvoicePoChoice(msg, data, pull);
          return;
        }
        data.rows.forEach(function (row) { tableApi.addRow(row); });
        setImportMsg(msg, invoiceLoadedMessage(
          tf("loaded_from_pallet", "Заредени {count} реда от палетна карта № {number}",
             { count: data.count, number: data.number }) +
          (data.loaded_po !== undefined
            ? " " + tf("loaded_for_po", "за поръчка {po}", { po: invoicePoLabel(data.loaded_po) })
            : "") + ".",
          data), "ok");
        input.value = "";
        input.focus();
        if (onChanged) onChanged();
      })
      .catch(function (err) {
        setImportMsg(msg, (err && err.sessionExpired)
          ? SESSION_EXPIRED_MSG : t("request_error", "Грешка при заявката."), "err");
      });
  }

  btn.addEventListener("click", function () { pull(); });
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

  function load(poNo) {
    if (!input.files || !input.files.length) {
      setImportMsg(msg, t("pick_xlsx_first", "Първо изберете .xlsx файл."), "err");
      return;
    }
    setImportMsg(msg, t("loading", "Зареждане…"));
    var body = new FormData();
    body.append("excel_file", input.files[0]);
    body.append("csrf_token", csrfInput ? csrfInput.value : "");
    if (poNo !== undefined) body.append("po_no", poNo);
    fetchJsonSafe(btn.dataset.url, { method: "POST", body: body })
      .then(function (data) {
        if (!data.ok) { setImportMsg(msg, data.error || t("generic_error", "Грешка."), "err"); return; }
        if (data.choose_po) {
          // НЕ чистим input — повторното зареждане праща същия файл.
          setImportMsg(msg, "");
          renderInvoicePoChoice(msg, data, load);
          return;
        }
        data.rows.forEach(function (row) { tableApi.addRow(row); });
        var loadedText = invoiceLoadedMessage(
          tf("loaded_from_file", "Заредени {count} реда от „{filename}“",
             { count: data.count, filename: data.filename }) +
          (data.loaded_po !== undefined
            ? " " + tf("loaded_for_po", "за поръчка {po}", { po: invoicePoLabel(data.loaded_po) })
            : "") + ".",
          data);
        // Одит (16.08.2026, находка №18): предупреждения от сървъра (напр.
        // орязани редове, заглавен ред на друга позиция, обединени клетки —
        // виж routes_invoices._parse_invoice_items_xlsx/invoice_import_items)
        // — добавят се към същото съобщение, вместо да се губят тихо.
        if (data.warnings && data.warnings.length) {
          loadedText += " " + data.warnings.join(" ");
        }
        setImportMsg(msg, loadedText, "ok");
        input.value = "";
        if (onChanged) onChanged();
      })
      .catch(function (err) {
        setImportMsg(msg, (err && err.sessionExpired)
          ? SESSION_EXPIRED_MSG : t("request_error", "Грешка при заявката."), "err");
      });
  }

  btn.addEventListener("click", function () { load(); });
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
    // Дребни (одит): СЪЩИЯТ проблем като bindClientSelect по-горе — при
    // празен избор (parseInt("",10)=NaN, entry не се намира) кодът
    // преди поправката просто спираше, оставяйки данните на ПРЕДИШНО
    // избрания клиент от адресната книга видимо попълнени във фактурата.
    var map = entry ? {
      consignee_name: entry.delivery_name,
      consignee_address: entry.delivery_address,
      consignee_phone: entry.delivery_phone,
      billto_name: entry.billing_name,
      billto_address: entry.billing_address,
      billto_phone: entry.billing_phone
    } : {
      consignee_name: "", consignee_address: "", consignee_phone: "",
      billto_name: "", billto_address: "", billto_phone: ""
    };
    Object.keys(map).forEach(function (name) {
      var el = form.querySelector('[name="' + name + '"]');
      if (el) {
        el.value = map[name] || "";
        if (el.value) markAutofilled(el);
      }
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

// ---------------------------------------------------------------- предупреждение за автоматичен рестарт (В6)
// Одит (находка В6, висок риск): автоматичното обновяване преди тази
// поправка рестартираше програмата (os._exit) без НИКАКВО предупреждение
// — потребител, попълващ форма в момента, губеше всичко незапазено.
// Сървърът вече изчаква updater.AUTO_RESTART_WARNING_SECONDS, преди
// реално да рестартира; тук показваме видим (не блокиращ) банер с
// обратно броене, за да има потребителят шанс да довърши/запази.
function initPendingRestartBanner() {
  var banner = document.getElementById("pending-restart-banner");
  var textEl = document.getElementById("pending-restart-text");
  if (!banner || !textEl) return;

  var secondsLeft = null;
  var pollTimer = null;

  function render() {
    if (secondsLeft === null) { banner.style.display = "none"; return; }
    banner.style.display = "flex";
    textEl.textContent = tf("pending_restart",
      "Програмата ще се рестартира автоматично след {seconds} сек. поради обновяване —"
      + " запазете текущата си работа.", { seconds: Math.max(0, secondsLeft) });
  }

  function poll() {
    fetch("/update/pending-restart", {credentials: "same-origin"})
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        secondsLeft = (data && data.pending) ? data.seconds_left : null;
        render();
      })
      .catch(function () { /* мълчаливо — банерът просто не се обновява този път */ });
  }

  // Първоначалната стойност идва СЪРВЪРНО рендирана (без забавяне при
  // презареждане на страница по средата на изчакването) — вижте
  // window.__PENDING_RESTART__ в base.html; полингът поема нататъшното
  // обратно броене и покрива случая на потребител, останал на една и
  // съща страница през целия прозорец на предупреждението.
  if (window.__PENDING_RESTART__) {
    secondsLeft = window.__PENDING_RESTART__.seconds_left;
    render();
  }
  pollTimer = setInterval(poll, 15000);
  if (secondsLeft === null) poll();
  // Одит (16.08.2026, находка №41): преди тази поправка тук стоеше
  // window.addEventListener("beforeunload", ...) само за да спре
  // setInterval при напускане на страницата — БЕЗУСЛОВНО регистриран
  // beforeunload listener обаче прави страницата НЕГОДНА за browser back-
  // forward cache (bfcache) в повечето съвременни браузъри (Chrome/Edge
  // изрично документират това), дори когато listener-ът НЕ вика
  // preventDefault()/не показва диалог за потвърждение — самото му
  // присъствие е достатъчно. Практическата полза от clearInterval тук е
  // нулева: при истинско напускане на страницата ЦЕЛИЯТ JS контекст (вкл.
  // самия таймер) се унищожава от браузъра, независимо дали сме го
  // "спрели" ръчно — премахнат е изцяло, вместо да плащаме цената на
  // изгубения bfcache за несъществуваща полза.
}

document.addEventListener("DOMContentLoaded", function () {
  initToasts();
  initConfirmModal();
  initBusyForms();
  initLiveSearch();
  initDocumentForm();
  initPendingRestartBanner();
  initCmrPrintFit();


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

    function isVisibleModal(modal) {
      return !!(modal && modal.style.display !== "none" && modal.style.display !== "");
    }

    function isModalOpen() {
      // Одит (находка С4, среден риск): преди поправката тук се проверяваше
      // САМО камерният модал — сканиране, докато е отворен диалогът за
      // потвърждение (#confirm-modal, виж initConfirmModal по-горе),
      // изобщо не биваше блокирано, а буферът/Enter-ът минаваше „под“
      // модала и отвеждаше страницата другаде, без потребителят изобщо да
      // разбере защо диалогът внезапно изчезна.
      // Одит (16.08.2026, находка №40): третият модал в приложението
      // (#pallet-type-modal — „Друг“ размер на палет) липсваше тук —
      // сканиран баркод при отворен модал за размери (фокус извън
      // текстовите полета му, напр. клик върху заглавието/тялото) би
      // навигирал страницата и изхвърлил оператора от формата с
      // всичко въведено, вместо да остане блокиран както при другите два.
      return isVisibleModal(document.getElementById("camera-scan-modal")) ||
             isVisibleModal(document.getElementById("confirm-modal")) ||
             isVisibleModal(document.getElementById("pallet-type-modal"));
    }

    // Одит (находка С4, среден риск): физическа клавиша -> знакът, който
    // латинска (US QWERTY) подредба би дала на СЪЩАТА физическа позиция.
    // e.code съобщава ТОЧНО тази физическа позиция (напр. "KeyC") и е
    // НАПЪЛНО НЕЗАВИСИМ от текущо активната подредба на клавиатурата на
    // операционната система — за разлика от e.key (предишния код), чиято
    // стойност Windows превежда според активната подредба. Физическите
    // баркод скенери емулират клавиатура на латинска (US) подредба
    // независимо какво в момента е избрано в Windows, затова при кирилска
    // подредба e.key за физическия клавиш „C“ дава кирилска буква (напр.
    // „ъ“), докато e.code за СЪЩИЯ клавиш винаги остава „KeyC“ — точно
    // затова само глобалният буфер тук е напълно имунизиран към подредбата
    // (вижте bg_keyboard.py за ВТОРА линия защита в самите текстови полета
    // за сканиране, където това не важи — там символите вече идват
    // преведени от браузъра/ОС, преди JS изобщо да ги види).
    var CODE_TO_CHAR = {
      Minus: "-", Equal: "=", BracketLeft: "[", BracketRight: "]",
      Semicolon: ";", Quote: "'", Backslash: "\\", Comma: ",", Period: ".",
      Slash: "/", Backquote: "`", Space: " "
    };
    "0123456789".split("").forEach(function (d, i) { CODE_TO_CHAR["Digit" + d] = d; });
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ".split("").forEach(function (ch) { CODE_TO_CHAR["Key" + ch] = ch; });

    function codeToChar(e) {
      var base = CODE_TO_CHAR[e.code];
      if (base === undefined) return null;
      if (base.length === 1 && base >= "A" && base <= "Z") return e.shiftKey ? base : base.toLowerCase();
      return base;
    }

    document.addEventListener("keydown", function (e) {
      if (isEditableFocus() || isModalOpen()) { buffer = ""; return; }

      // Игнорираме комбинации с модификатор (Ctrl/Alt/Meta) — иначе браузърни
      // клавишни комбинации с еднобуквен клавиш (Ctrl+A, Cmd+R и т.н.) биха
      // замърсили буфера с случайни символи, различни от реално сканиран код.
      if (e.ctrlKey || e.altKey || e.metaKey) { buffer = ""; return; }

      // Одит (находка С4): задържан клавиш (auto-repeat) НИКОГА не идва от
      // реален скенер — той изпраща по едно keydown събитие на символ.
      // Задържане (напр. случайно притиснат Enter от служител) преди
      // поправката можеше да замърси/преждевременно да изпрати буфера.
      if (e.repeat) return;

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
      var ch = codeToChar(e);
      if (ch !== null) {
        buffer += ch;
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
    // Одит (16.08.2026, находка №29, средна): "поколение" на текущия
    // отворен опит за камера — СЪЩИЯТ модел, ползван в remote_tunnel.py
    // (находка №12) за аналогично надбягване между асинхронен старт и
    // междинно спиране. getUserMedia() чака РЕАЛНО РАЗРЕШЕНИЕ от
    // потребителя (диалогът на браузъра) — може да отнеме произволно
    // дълго, докато потребителят реши. Ако модалът бъде затворен
    // (Escape/✕/клик извън него) ПРЕДИ .then() да се изпълни, преди тази
    // поправка callback-ът въпреки това презаписваше camStream с НОВ,
    // РЕАЛНО активен поток, палеше видеото в СКРИТ модал и стартираше
    // разпознаването — камерата оставаше включена (индикаторът на
    // устройството свети) без видим начин потребителят да я спре.
    var camGeneration = 0;
    // Одит (19.08.2026, находка №37, дребна): таймер за подсказката „не
    // откривам код“ — виж startCamHintTimer по-долу.
    var camHintTimer = null;
    // Одит (19.08.2026, находка №33): виж activateModal — функцията за
    // връщане на фокуса, докато камерният модал е отворен.
    var camDeactivate = null;

    // Дребни (одит): камерният модал се затваряше само с ✕ — нямаше
    // Escape/клик по фона (за разлика от #confirm-modal, който вече
    // поддържа и двете). Достъпност: клавиатурен потребител, попаднал в
    // модала, беше „заклещен“ — единственият изход е мишка върху ✕.
    function onCamKey(e) { if (e.key === "Escape") stopCamera(); }

    function stopCamera() {
      camGeneration++;  // виж находка №29 — обезсилва вече изпратен getUserMedia() опит
      camDetecting = false;
      if (camRaf) cancelAnimationFrame(camRaf);
      camRaf = null;
      if (camHintTimer) { clearTimeout(camHintTimer); camHintTimer = null; }
      if (camStream) {
        camStream.getTracks().forEach(function (t) { t.stop(); });
        camStream = null;
      }
      camVideo.srcObject = null;
      camModal.style.display = "none";
      document.removeEventListener("keydown", onCamKey);
      // Одит (19.08.2026, находка №33): връща фокуса на бутона „Сканирай с
      // камера“ и „отпуска“ фона (виж activateModal по-горе).
      if (camDeactivate) { camDeactivate(); camDeactivate = null; }
    }

    function submitScanned(code) {
      stopCamera();
      if (globalForm && globalCode) {
        globalCode.value = code;
        globalForm.submit();
      }
    }

    // Одит (19.08.2026, находка №37, дребна): QR кодът, отпечатан на
    // НАШАТА собствена бланка (_macros.doc_qr), съдържа ПЪЛЕН адрес
    // (`https://хост/p/<token>`), а не номер на документ — подаден както е
    // в полето за търсене, той просто не намира нищо. Тук разпознаваме
    // такъв адрес и връщаме пътя към ПУБЛИЧНИЯ преглед НА ТОЗИ сървър.
    //
    // Нарочно НЕ отваряме сканирания адрес както е записан: (а) хостът в
    // отпечатания QR може да е стар/чужд (виж находка №21 — ефимерните
    // *.trycloudflare.com поддомейни се преизползват), (б) не бива
    // сканиране на произволен чужд QR код да отваря произволен сайт от
    // прозореца на програмата. Взимаме САМО токена и го отваряме на
    // текущия origin — същият документ, но гарантирано от нашия сървър.
    var PUBLIC_DOC_RE = /\/p\/([A-Za-z0-9_-]{16,128})\/?$/;

    function publicDocPath(value) {
      if (!/^https?:\/\//i.test(value)) return null;
      var m = PUBLIC_DOC_RE.exec(value.split("?")[0].split("#")[0]);
      return m ? "/p/" + m[1] : null;
    }

    // Одит (19.08.2026, находка №37): дотук при неразпознат код НЕ се
    // случваше НИЩО — безкрайно, без съобщение и без таймаут; операторът
    // няма как да разбере дали камерата работи, дали кодът не се вижда,
    // или програмата е забила. След ~10 сек. без разпознат код показваме
    // конкретна подсказка какво да опита.
    function startCamHintTimer() {
      if (camHintTimer) clearTimeout(camHintTimer);
      camHintTimer = setTimeout(function () {
        camHintTimer = null;
        if (camDetecting) {
          camMsg.textContent = t("camera_no_code_yet",
            "Още не откривам код — доближете телефона на 10–20 см от кода, "
            + "задръжте неподвижно и се уверете, че е добре осветен.");
        }
      }, 10000);
    }

    function tick(detector) {
      if (!camDetecting) return;
      detector.detect(camVideo).then(function (codes) {
        if (codes && codes.length) {
          var value = (codes[0].rawValue || "").trim();
          var path = publicDocPath(value);
          if (path) { stopCamera(); window.location.href = path; return; }
          if (/^https?:\/\//i.test(value)) {
            // Чужд QR код (реклама, опаковка, друга система) — НЕ го
            // подаваме като код за търсене и НЕ го отваряме; казваме го и
            // продължаваме да сканираме.
            camMsg.textContent = t("camera_foreign_qr",
              "Сканираният QR код не води към документ от тази програма.");
            startCamHintTimer();
            camRaf = requestAnimationFrame(function () { tick(detector); });
            return;
          }
          if (value) { submitScanned(value); return; }
        }
        camRaf = requestAnimationFrame(function () { tick(detector); });
      }).catch(function () {
        camRaf = requestAnimationFrame(function () { tick(detector); });
      });
    }

    camModal.addEventListener("click", function (e) { if (e.target === camModal) stopCamera(); });

    camBtn.addEventListener("click", function () {
      camMsg.textContent = "";
      camModal.style.display = "flex";
      document.addEventListener("keydown", onCamKey);
      // Одит (19.08.2026, находка №33): фокус върху ✕ (единственият
      // управляващ елемент в този модал — самото сканиране е автоматично),
      // Tab затворен в модала, фонът inert, а при затваряне фокусът се
      // връща на бутона „Сканирай с камера“ в страничната лента.
      camDeactivate = activateModal(camModal, camClose);
      if (!window.isSecureContext) {
        camMsg.textContent = t("camera_needs_https",
          "Камерата изисква сигурна връзка (https://). "
          + "Ако сканирате от телефон извън офиса, използвайте отдалечения "
          + "адрес от „⚙ Настройки“ (само за администратори).");
        return;
      }
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        camMsg.textContent = t("camera_unsupported", "Този браузър не поддържа достъп до камера.");
        return;
      }
      if (!("BarcodeDetector" in window)) {
        camMsg.textContent = t("camera_no_detector",
          "Този браузър не поддържа вградено разпознаване "
          + "на баркод (напр. Safari на по-стар iPhone). Използвайте "
          + "физически скенер или полето за въвеждане.");
        return;
      }
      var myCamGen = camGeneration;
      navigator.mediaDevices.getUserMedia({
        video: { facingMode: { ideal: "environment" } }
      }).then(function (stream) {
        // Одит (находка №29): модалът е бил затворен (stopCamera()),
        // докато чакахме разрешение от потребителя — НЕ палим камерата
        // изобщо, веднага спираме новополучения поток.
        if (myCamGen !== camGeneration) {
          stream.getTracks().forEach(function (t) { t.stop(); });
          return;
        }
        camStream = stream;
        camVideo.srcObject = stream;
        // Одит (19.08.2026, находка №37, дребна): дотук списъкът беше само
        // `code_128` — тоест насочен към QR кода, който САМАТА програма
        // печата на бланката (виж _macros.doc_qr), скенерът не разпознаваше
        // нищо, безкрайно и без съобщение. Добавени са `qr_code` (нашият
        // собствен код за публичен преглед) и `ean_13` (стандартен
        // търговски баркод по опаковките).
        //
        // Форматите се пресяват спрямо това, което конкретният браузър
        // реално поддържа: конструкторът на BarcodeDetector хвърля, ако
        // сред `formats` има неподдържан — а тогава сканирането отново не
        // би тръгнало изобщо. Ако проверката не е налична/не успее,
        // конструираме БЕЗ `formats` (по подразбиране = всички поддържани).
        var wanted = ["qr_code", "code_128", "ean_13"];
        var startDetecting = function (detector) {
          if (myCamGen !== camGeneration) return;  // виж находка №29
          camDetecting = true;
          startCamHintTimer();
          camRaf = requestAnimationFrame(function () { tick(detector); });
        };
        if (window.BarcodeDetector.getSupportedFormats) {
          window.BarcodeDetector.getSupportedFormats().then(function (supported) {
            var use = wanted.filter(function (f) { return (supported || []).indexOf(f) >= 0; });
            startDetecting(use.length
              ? new window.BarcodeDetector({ formats: use })
              : new window.BarcodeDetector());
          }).catch(function () {
            startDetecting(new window.BarcodeDetector());
          });
        } else {
          startDetecting(new window.BarcodeDetector({ formats: wanted }));
        }
      }).catch(function (err) {
        if (myCamGen !== camGeneration) return;  // виж находка №29 по-горе
        camMsg.textContent = tf("camera_denied",
          "Достъпът до камерата е отказан или неуспешен: {error}", { error: err.message });
      });
    });
    if (camClose) camClose.addEventListener("click", stopCamera);
  }
});
