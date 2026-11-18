// AI Catalogs editor. No inline script anywhere (CSP).
// Pickers are progressive enhancement: each one hides a plain field and writes the same text
// format back into it (one value per line, or JSON, which the server reads as YAML), so the
// form still works and round-trips without this script.
(function () {
  "use strict";
  var PREFIX = "/configure";

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }
  function lines(text) {
    return (text || "").split(/\r?\n/).map(function (s) { return s.trim(); }).filter(Boolean);
  }
  function fire(field) { field.dispatchEvent(new Event("input", { bubbles: true })); }

  // Movies or shows: the catalog form's toggle (PLAN.md section 29).
  var catalogForm = document.getElementById("form-tab");
  function mediaKind() {
    return catalogForm && catalogForm.getAttribute("data-kind") === "series" ? "series" : "movie";
  }
  function titleSearchKind() { return mediaKind() === "series" ? "tv" : "movie"; }

  // ---------------------------------------------------------------- suggestion dropdown
  function attachSuggest(input, kind, onPick) {
    var box = el("div", "suggest-list");
    box.hidden = true;
    input.parentNode.insertBefore(box, input.nextSibling);
    var items = [], active = -1, timer = null, seq = 0;
    function render() {
      box.innerHTML = "";
      items.forEach(function (it, i) {
        var opt = el("button", "suggest-item" + (i === active ? " active" : ""), it.label);
        opt.type = "button";
        opt.addEventListener("mousedown", function (ev) { ev.preventDefault(); pick(i); });
        box.appendChild(opt);
      });
      box.hidden = items.length === 0;
    }
    function pick(i) {
      if (items[i]) onPick(items[i]);
      items = []; active = -1; render();
    }
    input.addEventListener("input", function () {
      clearTimeout(timer);
      var q = input.value.trim();
      if (q.length < 2) { items = []; render(); return; }
      timer = setTimeout(function () {
        var mine = ++seq;
        var k = typeof kind === "function" ? kind() : kind;
        fetch(PREFIX + "/suggest?kind=" + encodeURIComponent(k) + "&q=" + encodeURIComponent(q), {
          credentials: "same-origin", headers: { Accept: "application/json" }
        }).then(function (r) { return r.ok ? r.json() : []; })
          .then(function (data) { if (mine === seq) { items = data || []; active = -1; render(); } })
          .catch(function () {});
      }, 250);
    });
    input.addEventListener("keydown", function (ev) {
      if (box.hidden) return;
      if (ev.key === "ArrowDown") { ev.preventDefault(); active = Math.min(items.length - 1, active + 1); render(); }
      else if (ev.key === "ArrowUp") { ev.preventDefault(); active = Math.max(0, active - 1); render(); }
      else if (ev.key === "Enter" && active >= 0) { ev.preventDefault(); ev.stopPropagation(); pick(active); }
      else if (ev.key === "Escape") { items = []; render(); }
    });
    input.addEventListener("blur", function () { setTimeout(function () { items = []; render(); }, 150); });
  }

  // ---------------------------------------------------------------- chips
  // values: array of strings. opts: {options: [[value,label]], kind: "movie"|..., free: bool}
  function chipEditor(values, opts, onChange, placeholder) {
    var wrap = el("div", "chips");
    var list = el("div", "chip-list");
    wrap.appendChild(list);
    var labels = {};
    (opts.options || []).forEach(function (o) { labels[String(o[0])] = o[1]; });
    function draw() {
      list.innerHTML = "";
      values.forEach(function (v, i) {
        var c = el("span", "tag");
        c.appendChild(el("span", "", labels[v] || v));
        var x = el("button", "tag-x", "×");
        x.type = "button";
        x.setAttribute("aria-label", "Remove " + (labels[v] || v));
        x.addEventListener("click", function () { values.splice(i, 1); draw(); onChange(values); });
        c.appendChild(x);
        list.appendChild(c);
      });
      if (sel) refreshSelect();
    }
    function add(v) {
      v = String(v).trim();
      if (!v || values.indexOf(v) >= 0) return;
      values.push(v); draw(); onChange(values);
    }
    var sel = null, input = null;
    function refreshSelect() {
      sel.innerHTML = "";
      var first = el("option", "", placeholder || "Add…");
      first.value = "";
      sel.appendChild(first);
      opts.options.forEach(function (o) {
        if (values.indexOf(String(o[0])) >= 0) return;
        var op = el("option", "", o[1]);
        op.value = String(o[0]);
        sel.appendChild(op);
      });
    }
    if (opts.options) {
      sel = el("select", "chip-add");
      sel.addEventListener("change", function () { if (sel.value) add(sel.value); sel.value = ""; });
      wrap.appendChild(sel);
    } else {
      var holder = el("div", "suggest-wrap");
      input = el("input", "chip-input");
      input.type = "text";
      input.placeholder = placeholder || "Type and press Enter";
      input.autocomplete = "off";
      holder.appendChild(input);
      wrap.appendChild(holder);
      if (opts.kind) attachSuggest(input, opts.kind, function (it) { add(it.value); input.value = ""; input.focus(); });
      input.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter" || (ev.key === "," && opts.free)) {
          ev.preventDefault();
          if (input.value.trim()) { add(input.value); input.value = ""; }
        }
      });
    }
    draw();
    return wrap;
  }

  function jsonOf(field) {
    var text = field.value.trim();
    if (!text) return null;
    try { return JSON.parse(text); } catch (e) { /* stored YAML: use the stored value */ }
    try { return JSON.parse(field.getAttribute("data-json") || "null"); } catch (e) { return null; }
  }
  function writeJson(field, obj) {
    field.value = obj && Object.keys(obj).length ? JSON.stringify(obj) : "";
    fire(field);
  }
  function optionsOf(field) {
    try { return JSON.parse(field.getAttribute("data-options") || "null"); } catch (e) { return null; }
  }
  function mount(field, node) {
    field.hidden = true;
    field.parentNode.insertBefore(node, field);
  }

  var CHIP_HINTS = { movie: "Type a movie title…", free: "Type a theme and press Enter", languages: "Add a language…", genres: "Add a genre…" };

  function buildChips(field, source) {
    // "lines" fields hold one value per line; "yaml" fields (e.g. Never include) hold a list.
    var asList = field.getAttribute("data-format") === "yaml";
    var values;
    if (asList) {
      var parsed = jsonOf(field);
      values = (Array.isArray(parsed) ? parsed : []).map(function (v) {
        if (v && typeof v === "object") return v.year ? v.title + " (" + v.year + ")" : String(v.title || "");
        return String(v);
      }).filter(Boolean);
    } else {
      values = lines(field.value);
    }
    var opts = { options: optionsOf(field), kind: source === "movie" ? titleSearchKind : null, free: source === "free" };
    var node = chipEditor(values, opts, function (v) {
      field.value = asList ? (v.length ? JSON.stringify(v) : "") : v.join("\n");
      fire(field);
    }, source === "movie" ? (mediaKind() === "series" ? "Type a show title…" : CHIP_HINTS.movie) : CHIP_HINTS[source]);
    var input = node.querySelector(".chip-input");
    if (input && source === "movie") {
      input.setAttribute("data-ph-movie", CHIP_HINTS.movie);
      input.setAttribute("data-ph-series", "Type a show title…");
    }
    field._widget = node;
    mount(field, node);
  }

  document.querySelectorAll("[data-widget]").forEach(function (field) {
    var widget = field.getAttribute("data-widget"), source = field.getAttribute("data-source");
    if (widget === "chips") {
      buildChips(field, source);
    } else if (widget === "suggest") {
      var holder = el("div", "suggest-wrap");
      field.parentNode.insertBefore(holder, field);
      holder.appendChild(field);
      attachSuggest(field, source, function (it) { field.value = it.value; fire(field); });
    } else if (widget === "denylist") {
      var obj = jsonOf(field) || {};
      var box = el("div", "stack-sm");
      [["collections", "collection", "Franchises", "Type a franchise, e.g. a film series…"],
       ["companies", "company", "Studios", "Type a studio…"],
       ["keywords", "keyword", "Keywords", "Type a TMDB keyword…"]].forEach(function (spec) {
        var row = el("div", "sub-row");
        if (spec[0] === "collections") row.setAttribute("data-movie-only", "");  // TV has no collections
        row.appendChild(el("span", "sub-label", spec[2]));
        var vals = (obj[spec[0]] || []).map(String);
        row.appendChild(chipEditor(vals, { kind: spec[1] }, function (v) {
          obj[spec[0]] = v.slice();
          var any = Object.keys(obj).some(function (k) { return Array.isArray(obj[k]) ? obj[k].length : obj[k]; });
          writeJson(field, any ? obj : null);
        }, spec[3]));
        box.appendChild(row);
      });
      mount(field, box);
    } else if (widget === "weekdays") {
      var wk = jsonOf(field) || {};
      var grid = el("div", "weekday-grid");
      ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"].forEach(function (day) {
        var lab = el("label", "weekday");
        lab.appendChild(el("span", "", day.charAt(0).toUpperCase() + day.slice(1)));
        var inp = el("input");
        inp.type = "text";
        inp.value = wk[day] || wk[day.charAt(0).toUpperCase() + day.slice(1)] || "";
        inp.placeholder = day === "friday" ? "e.g. something light and funny" : "";
        inp.addEventListener("input", function () {
          delete wk[day.charAt(0).toUpperCase() + day.slice(1)];
          if (inp.value.trim()) wk[day] = inp.value.trim(); else delete wk[day];
          writeJson(field, wk);
        });
        lab.appendChild(inp);
        grid.appendChild(lab);
      });
      mount(field, grid);
    } else if (widget === "pairs") {
      var pairs = jsonOf(field) || {};
      var rows = el("div", "pairs");
      var addBtn = el("button", "small secondary", "+ Add");
      addBtn.type = "button";
      var keyOptions = source === "decades" ? optionsOf(field) : null;
      function save() {
        var out = {};
        rows.querySelectorAll(".pair").forEach(function (p) {
          var k = p.querySelector(".pair-key").value.trim(), n = p.querySelector(".pair-n").value.trim();
          if (k && n !== "") out[k] = /^\d+$/.test(n) ? parseInt(n, 10) : n;
        });
        writeJson(field, out);
      }
      function addRow(k, n) {
        var p = el("div", "pair");
        var key;
        if (keyOptions) {
          key = el("select", "pair-key");
          keyOptions.forEach(function (o) { var op = el("option", "", o[1]); op.value = String(o[0]); key.appendChild(op); });
          key.value = String(k || keyOptions[0][0]);
        } else {
          key = el("input", "pair-key");
          key.type = "text";
          key.placeholder = "Theme";
          key.value = k || "";
          var cats = document.getElementById("f-categories");
          if (cats) {
            var dl = document.getElementById("theme-options") || el("datalist");
            dl.id = "theme-options";
            dl.innerHTML = "";
            lines(cats.value).forEach(function (c) { var op = el("option"); op.value = c; dl.appendChild(op); });
            if (!dl.parentNode) document.body.appendChild(dl);
            key.setAttribute("list", "theme-options");
          }
        }
        var num = el("input", "pair-n");
        num.type = "text";
        num.inputMode = "numeric";
        num.placeholder = "Movies";
        num.value = n === undefined ? "" : String(n);
        var x = el("button", "tag-x", "×");
        x.type = "button";
        x.setAttribute("aria-label", "Remove");
        x.addEventListener("click", function () { p.remove(); save(); });
        [key, num].forEach(function (i) { i.addEventListener("input", save); i.addEventListener("change", save); });
        p.appendChild(key); p.appendChild(el("span", "muted small", "at least")); p.appendChild(num); p.appendChild(x);
        rows.appendChild(p);
      }
      Object.keys(pairs).forEach(function (k) { addRow(k, pairs[k]); });
      addBtn.addEventListener("click", function () { addRow(); });
      var w = el("div", "stack-sm");
      w.appendChild(rows); w.appendChild(addBtn);
      mount(field, w);
    }
  });

  // ---------------------------------------------------------------- sliders
  document.querySelectorAll("input[type=range][data-for]").forEach(function (range) {
    var box = document.getElementById(range.getAttribute("data-for"));
    if (!box) return;
    range.removeAttribute("tabindex");
    range.addEventListener("input", function () { box.value = range.value; });
    box.addEventListener("input", function () {
      var n = parseFloat(box.value);
      if (!isNaN(n)) range.value = n;
    });
  });

  // ---------------------------------------------------------------- side menu panels
  // Without this script every panel shows, stacked, and the menu links jump to them.
  var layout = document.getElementById("layout");
  if (layout) {
    var links = Array.prototype.slice.call(layout.querySelectorAll(".sidenav a[data-panel]"));
    var panels = Array.prototype.slice.call(layout.querySelectorAll(".panel"));
    // A Preview or a failed save renders at .../save: remember the panel per catalog, not per URL.
    var storeKey = "panel:" + window.location.pathname.replace(/\/save\/?$/, "");
    var ALIASES = { "#activity-tab": "p-runs", "#runs": "p-runs", "#form-tab": "p-catalog" };
    function panelFor(hash) {
      if (!hash) return null;
      if (ALIASES[hash]) return ALIASES[hash];
      var id = hash.slice(1);
      return panels.some(function (pn) { return pn.id === id; }) ? id : null;
    }
    function openPanel(id, push) {
      if (!panelFor("#" + id)) id = panels[0].id;
      panels.forEach(function (pn) { pn.classList.toggle("active", pn.id === id); });
      var activity = false;
      links.forEach(function (a) {
        var on = a.getAttribute("data-panel") === id;
        a.classList.toggle("active", on);
        if (on) { a.setAttribute("aria-current", "page"); activity = a.hasAttribute("data-activity"); }
        else a.removeAttribute("aria-current");
      });
      layout.setAttribute("data-mode", activity ? "activity" : "settings");
      try { sessionStorage.setItem(storeKey, id); } catch (e) { /* private mode */ }
      if (push && history.replaceState) history.replaceState(null, "", window.location.pathname + "#" + id);
    }
    layout.classList.add("js-panels");
    links.forEach(function (a) {
      a.addEventListener("click", function (ev) {
        ev.preventDefault();
        openPanel(a.getAttribute("data-panel"), true);
        var nav = layout.querySelector(".sidenav");
        if (nav && nav.getBoundingClientRect().top < 70) window.scrollTo({ top: layout.offsetTop - 80 });
        if (a.scrollIntoView && window.matchMedia("(max-width: 860px)").matches) a.scrollIntoView({ inline: "center", block: "nearest" });
      });
    });
    var stored = null;
    try { stored = sessionStorage.getItem(storeKey); } catch (e) { /* private mode */ }
    openPanel(panelFor(location.hash) || (stored && panelFor("#" + stored)) || panels[0].id, false);
    if (location.hash) {  // open the panel, don't jump past the header and notice
      window.scrollTo(0, 0);
      window.addEventListener("load", function () { setTimeout(function () { window.scrollTo(0, 0); }, 0); });
    }
    window.addEventListener("hashchange", function () {  // back / forward, or a typed #section
      var id = panelFor(location.hash);
      if (id) openPanel(id, false);
    });
    // A required field in a hidden panel would block saving without a word: show its panel.
    var settingsForm = document.getElementById("form-tab");
    if (settingsForm) {
      settingsForm.addEventListener("invalid", function (ev) {
        var pn = ev.target.closest && ev.target.closest(".panel");
        if (pn && !pn.classList.contains("active")) openPanel(pn.id, true);
      }, true);
    }
  }

  // ---------------------------------------------------------------- schedule
  var modes = document.querySelectorAll("input[name=sched_mode]");
  function syncSchedule() {
    var mode = "manual";
    modes.forEach(function (m) { if (m.checked) mode = m.value; });
    document.querySelectorAll("[data-sched]").forEach(function (e) {
      e.hidden = e.getAttribute("data-sched").split(" ").indexOf(mode) < 0;
    });
  }
  modes.forEach(function (m) { m.addEventListener("change", syncSchedule); });
  if (modes.length) syncSchedule();
  document.querySelectorAll("input[data-autotz]").forEach(function (box) {
    if (box.value) return;
    try { box.value = Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch (e) { /* UTC */ }
  });

  // ---------------------------------------------------------------- time zone picker
  // A themed dropdown of common zones (with a search over every zone the browser knows) that
  // writes the IANA name into the plain text box, which stays the source of truth.
  var TZ_GROUPS = [
    ["United States & Canada", [
      ["America/New_York", "Eastern Time", "New York"], ["America/Chicago", "Central Time", "Chicago"],
      ["America/Denver", "Mountain Time", "Denver"], ["America/Phoenix", "Mountain Time, no DST", "Phoenix"],
      ["America/Los_Angeles", "Pacific Time", "Los Angeles"], ["America/Anchorage", "Alaska", "Anchorage"],
      ["Pacific/Honolulu", "Hawaii", "Honolulu"], ["America/Halifax", "Atlantic Time", "Halifax"],
      ["America/St_Johns", "Newfoundland", "St. John's"]]],
    ["Latin America", [
      ["America/Mexico_City", "Mexico", "Mexico City"], ["America/Bogota", "Colombia", "Bogotá"],
      ["America/Sao_Paulo", "Brazil", "São Paulo"], ["America/Argentina/Buenos_Aires", "Argentina", "Buenos Aires"]]],
    ["Europe", [
      ["Europe/London", "UK & Ireland", "London"], ["Europe/Lisbon", "Portugal", "Lisbon"],
      ["Europe/Paris", "Central Europe", "Paris"], ["Europe/Berlin", "Central Europe", "Berlin"],
      ["Europe/Madrid", "Central Europe", "Madrid"], ["Europe/Rome", "Central Europe", "Rome"],
      ["Europe/Amsterdam", "Central Europe", "Amsterdam"], ["Europe/Stockholm", "Central Europe", "Stockholm"],
      ["Europe/Athens", "Eastern Europe", "Athens"], ["Europe/Helsinki", "Eastern Europe", "Helsinki"],
      ["Europe/Istanbul", "Turkey", "Istanbul"], ["Europe/Moscow", "Moscow", "Moscow"]]],
    ["Africa & Middle East", [
      ["Africa/Lagos", "West Africa", "Lagos"], ["Africa/Cairo", "Egypt", "Cairo"],
      ["Africa/Johannesburg", "South Africa", "Johannesburg"], ["Asia/Dubai", "Gulf", "Dubai"]]],
    ["Asia", [
      ["Asia/Kolkata", "India", "Kolkata"], ["Asia/Bangkok", "Indochina", "Bangkok"],
      ["Asia/Singapore", "Singapore", "Singapore"], ["Asia/Manila", "Philippines", "Manila"],
      ["Asia/Shanghai", "China", "Shanghai"], ["Asia/Hong_Kong", "Hong Kong", "Hong Kong"],
      ["Asia/Seoul", "Korea", "Seoul"], ["Asia/Tokyo", "Japan", "Tokyo"]]],
    ["Australia & Pacific", [
      ["Australia/Perth", "Western Australia", "Perth"], ["Australia/Adelaide", "Central Australia", "Adelaide"],
      ["Australia/Brisbane", "Queensland", "Brisbane"], ["Australia/Sydney", "Eastern Australia", "Sydney"],
      ["Pacific/Auckland", "New Zealand", "Auckland"]]],
    ["Other", [["UTC", "Coordinated Universal Time", "UTC"]]]
  ];
  var TZ_KNOWN = {};
  TZ_GROUPS.forEach(function (g) { g[1].forEach(function (z) { TZ_KNOWN[z[0]] = z; }); });
  function tzOffset(zone) {
    try {
      var part = new Intl.DateTimeFormat("en-US", { timeZone: zone, timeZoneName: "shortOffset" })
        .formatToParts(new Date()).filter(function (p) { return p.type === "timeZoneName"; })[0];
      var off = part ? part.value.replace(/^GMT/, "") : "";
      return "UTC" + (off ? off.replace("-", "\u2212") : "");
    } catch (e) { return ""; }
  }
  function tzCity(zone) { return zone.split("/").pop().replace(/_/g, " "); }
  function tzName(zone) {
    var k = TZ_KNOWN[zone];
    if (k) return zone === "UTC" ? "UTC" : k[1] + " \u00b7 " + k[2];
    return tzCity(zone) + " \u00b7 " + zone.split("/")[0];
  }
  var tzDevice = "";
  try { tzDevice = Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch (e) { /* none */ }
  var tzAll = [];
  try { tzAll = Intl.supportedValuesOf ? Intl.supportedValuesOf("timeZone") : []; } catch (e) { tzAll = []; }

  document.querySelectorAll("input[data-tz-picker]").forEach(function (box) {
    var wrap = el("div", "tz");
    var btn = el("button", "tz-button");
    btn.type = "button";
    btn.setAttribute("aria-haspopup", "listbox");
    btn.setAttribute("aria-expanded", "false");
    var btnName = el("span", "tz-name"), btnOff = el("span", "tz-off");
    var nameWrap = el("span");
    nameWrap.appendChild(btnName); nameWrap.appendChild(document.createTextNode(" ")); nameWrap.appendChild(btnOff);
    btn.appendChild(nameWrap); btn.appendChild(el("span", "chev"));
    var pop = el("div", "tz-pop");
    pop.hidden = true;
    var search = el("input", "tz-search");
    search.type = "text";
    search.placeholder = "Search a city or region…";
    search.autocomplete = "off";
    search.setAttribute("aria-label", "Search time zones");
    var list = el("div", "tz-list");
    list.setAttribute("role", "listbox");
    pop.appendChild(search); pop.appendChild(list);
    wrap.appendChild(btn); wrap.appendChild(pop);
    box.hidden = true;
    box.parentNode.insertBefore(wrap, box);
    var lbl = document.querySelector('label[for="' + box.id + '"]');
    if (lbl) { btn.id = box.id + "-btn"; lbl.setAttribute("for", btn.id); }

    function current() { return box.value.trim() || "UTC"; }
    function showValue() { btnName.textContent = tzName(current()); btnOff.textContent = tzOffset(current()); }
    var items = [], active = -1;
    function addItem(zone, extra) {
      var b = el("button", "tz-item");
      b.type = "button";
      b.setAttribute("role", "option");
      b.setAttribute("aria-selected", zone === current() ? "true" : "false");
      var left = el("span", "", extra || tzName(zone));
      b.appendChild(left);
      b.appendChild(el("span", "tz-off", tzOffset(zone)));
      b.addEventListener("click", function () { choose(zone); });
      items.push(b);
      list.appendChild(b);
    }
    function render() {
      list.innerHTML = ""; items = []; active = -1;
      var q = search.value.trim().toLowerCase();
      if (!q) {
        if (tzDevice) {
          list.appendChild(el("div", "tz-group", "This device"));
          addItem(tzDevice);
        }
        TZ_GROUPS.forEach(function (g) {
          list.appendChild(el("div", "tz-group", g[0]));
          g[1].forEach(function (z) { addItem(z[0]); });
        });
        if (!TZ_KNOWN[current()] && current() !== tzDevice) {
          list.insertBefore(el("div", "tz-group", "Selected"), list.firstChild);
          var tmp = items.length; addItem(current());
          list.insertBefore(items[tmp], list.children[1]);
        }
      } else {
        var seen = {}, hits = [];
        function test(zone) {
          if (seen[zone]) return;
          var k = TZ_KNOWN[zone];
          var hay = (zone + " " + tzCity(zone) + (k ? " " + k[1] + " " + k[2] : "")).toLowerCase();
          if (hay.indexOf(q) >= 0) { seen[zone] = 1; hits.push(zone); }
        }
        Object.keys(TZ_KNOWN).forEach(test);
        tzAll.forEach(test);
        if (/^[a-z_]+\/[a-z_\/]+$/i.test(search.value.trim())) test(search.value.trim());
        hits.slice(0, 60).forEach(function (z) { addItem(z); });
        if (!hits.length) list.appendChild(el("div", "tz-empty", "No time zone matches “" + search.value.trim() + "”."));
      }
    }
    function setActive(i) {
      if (!items.length) return;
      active = Math.max(0, Math.min(items.length - 1, i));
      items.forEach(function (b, j) { b.classList.toggle("active", j === active); });
      items[active].scrollIntoView({ block: "nearest" });
    }
    function open() {
      search.value = ""; render(); pop.hidden = false; btn.setAttribute("aria-expanded", "true");
      search.focus();
      var sel = list.querySelector('[aria-selected="true"]');
      if (sel) sel.scrollIntoView({ block: "center" });
    }
    function close(focusBtn) {
      if (pop.hidden) return;
      pop.hidden = true; btn.setAttribute("aria-expanded", "false");
      if (focusBtn) btn.focus();
    }
    function choose(zone) {
      if (box.value !== zone) { box.value = zone; fire(box); box.dispatchEvent(new Event("change", { bubbles: true })); }
      showValue(); close(true);
    }
    btn.addEventListener("click", function () { if (pop.hidden) open(); else close(true); });
    search.addEventListener("input", render);
    pop.addEventListener("keydown", function (ev) {
      if (ev.key === "ArrowDown") { ev.preventDefault(); setActive(active + 1); }
      else if (ev.key === "ArrowUp") { ev.preventDefault(); setActive(active - 1); }
      else if (ev.key === "Enter") { ev.preventDefault(); if (items[active >= 0 ? active : 0]) items[active >= 0 ? active : 0].click(); }
      else if (ev.key === "Escape") { ev.preventDefault(); ev.stopPropagation(); close(true); }
    });
    document.addEventListener("mousedown", function (ev) { if (!wrap.contains(ev.target)) close(false); });
    wrap.addEventListener("focusout", function () {
      setTimeout(function () { if (!wrap.contains(document.activeElement)) close(false); }, 0);
    });
    showValue();
  });

  // ---------------------------------------------------------------- one-time notices
  // "?msg=..." shows a notice once: drop it from the address so a reload or the automatic refresh
  // after a run doesn't bring it back, and fade the notice out after a few seconds.
  if (/[?&]msg=/.test(window.location.search) && history.replaceState) {
    history.replaceState(null, "", window.location.pathname + window.location.hash);
  }
  document.querySelectorAll(".notice.flash").forEach(function (n) {
    setTimeout(function () { n.classList.add("gone"); }, 6000);
    setTimeout(function () { n.remove(); }, 6600);
  });

  // ---------------------------------------------------------------- run and version lists
  var filterBtns = document.querySelectorAll(".filter-btn");
  filterBtns.forEach(function (btn) {
    btn.addEventListener("click", function () {
      var kind = btn.getAttribute("data-filter");
      filterBtns.forEach(function (b) { b.classList.toggle("active", b === btn); });
      document.querySelectorAll("#run-rows .run-row").forEach(function (row) {
        row.classList.toggle("filtered-out", kind !== "all" && row.getAttribute("data-kind") !== kind);
      });
      var older = document.querySelector('[data-show-older="run-rows"]');
      if (kind !== "all") {  // a filter shows every matching run, older ones included
        document.querySelectorAll("#run-rows .older").forEach(function (r) { r.classList.add("shown"); });
        if (older) older.hidden = true;
      }
    });
  });
  document.querySelectorAll("[data-show-older]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      document.querySelectorAll("#" + btn.getAttribute("data-show-older") + " .older").forEach(function (r) {
        r.classList.add("shown");
      });
      btn.hidden = true;
    });
  });

  // ---------------------------------------------------------------- copy, confirm, cancel
  document.querySelectorAll("button[data-copy]").forEach(function (button) {
    button.addEventListener("click", function () {
      var box = document.getElementById(button.getAttribute("data-copy"));
      if (!box) return;
      box.select();
      var done = function () {
        button.textContent = "Copied ✓";
        setTimeout(function () { button.textContent = "Copy"; }, 2000);
      };
      if (navigator.clipboard) navigator.clipboard.writeText(box.value).then(done, function () {});
      else if (document.execCommand("copy")) done();
    });
  });
  // A themed confirm box (the browser's own is a white system dialog). Resolves true on OK.
  var modal = null;
  function ask(text, okText, danger) {
    if (!window.HTMLDialogElement) return Promise.resolve(window.confirm(text));
    if (!modal) {
      modal = el("dialog", "modal");
      var body = el("div", "modal-body");
      body.appendChild(el("span", "modal-icon", "!"));
      var words = el("div");
      words.appendChild(el("p", "modal-title"));
      words.appendChild(el("p", "modal-text"));
      body.appendChild(words);
      var actions = el("div", "modal-actions");
      var no = el("button", "secondary", "Cancel");
      no.type = "button"; no.value = "no";
      var yes = el("button", "modal-ok");
      yes.type = "button"; yes.value = "yes";
      [no, yes].forEach(function (b) { b.addEventListener("click", function () { modal.close(b.value); }); });
      actions.appendChild(no); actions.appendChild(yes);
      modal.appendChild(body); modal.appendChild(actions);
      modal.addEventListener("click", function (ev) { if (ev.target === modal) modal.close("no"); });  // backdrop
      document.body.appendChild(modal);
    }
    // "Delete “X”? It disappears…" → title "Delete “X”?", text "It disappears…"
    var cut = text.indexOf("? ");
    modal.querySelector(".modal-title").textContent = cut > 0 ? text.slice(0, cut + 1) : text;
    var rest = modal.querySelector(".modal-text");
    rest.textContent = cut > 0 ? text.slice(cut + 2) : "";
    rest.hidden = cut <= 0;
    var ok = modal.querySelector(".modal-ok");
    ok.textContent = okText || "OK";
    ok.className = "modal-ok" + (danger ? " danger" : "");
    modal.classList.toggle("is-danger", !!danger);
    modal.returnValue = "no";
    return new Promise(function (resolve) {
      modal.addEventListener("close", function done() {
        modal.removeEventListener("close", done);
        resolve(modal.returnValue === "yes");
      });
      modal.showModal();
      modal.querySelector("button.secondary").focus();
    });
  }
  document.querySelectorAll("form[data-confirm]").forEach(function (f) {
    f.addEventListener("submit", function (ev) {
      if (f._confirmed) { f._confirmed = false; return; }
      ev.preventDefault();
      var submitter = ev.submitter;
      ask(f.getAttribute("data-confirm"), f.getAttribute("data-confirm-ok"), true).then(function (yes) {
        if (!yes) return;
        f._confirmed = true;
        if (f.requestSubmit) f.requestSubmit(submitter && submitter.form === f ? submitter : undefined);
        else f.submit();
      });
    });
  });
  var editForm = document.getElementById("form-tab"), dirty = false;
  if (editForm) {
    editForm.addEventListener("input", function () { dirty = true; });
    editForm.addEventListener("change", function () { dirty = true; });
    editForm.addEventListener("submit", function () { dirty = false; });
  }
  document.querySelectorAll("[data-confirm-leave]").forEach(function (a) {
    a.addEventListener("click", function (ev) {
      if (!dirty) return;
      ev.preventDefault();
      ask("Discard your changes? What you changed here is not saved.", "Discard", true).then(function (yes) {
        if (yes) { dirty = false; window.location.href = a.href; }
      });
    });
  });
  var preview = document.getElementById("preview");
  if (preview) preview.scrollIntoView({ block: "start" });

  // ---------------------------------------------------------------- drag to reorder (mouse and touch)
  var grid = document.getElementById("catalog-grid");
  if (grid) {
    var dragging = null;
    function saveOrder() {
      var order = Array.prototype.map.call(grid.querySelectorAll(".cat"), function (c) { return c.getAttribute("data-id"); });
      var body = new URLSearchParams();
      body.set("csrf", grid.getAttribute("data-csrf"));
      body.set("order", order.join(","));
      fetch(grid.getAttribute("data-order-url"), { method: "POST", credentials: "same-origin", body: body })
        .catch(function () {});
    }
    function onMove(ev) {
      if (!dragging) return;
      ev.preventDefault();
      // One row per catalog: the row under the pointer decides the slot, by its vertical middle.
      var rows = Array.prototype.filter.call(grid.querySelectorAll(".cat"), function (c) { return c !== dragging; });
      var best = null, bestD = Infinity;
      rows.forEach(function (c) {
        var r = c.getBoundingClientRect();
        var d = Math.abs(ev.clientY - (r.top + r.height / 2));
        if (d < bestD) { bestD = d; best = c; }
      });
      if (!best) return;
      var r = best.getBoundingClientRect();
      var after = ev.clientY > r.top + r.height / 2;
      grid.insertBefore(dragging, after ? best.nextSibling : best);
    }
    function onUp() {
      if (!dragging) return;
      dragging.classList.remove("dragging");
      dragging = null;
      document.removeEventListener("pointermove", onMove);
      document.removeEventListener("pointerup", onUp);
      document.removeEventListener("pointercancel", onUp);
      saveOrder();
    }
    grid.querySelectorAll(".grip").forEach(function (grip) {
      grip.addEventListener("pointerdown", function (ev) {
        ev.preventDefault();
        dragging = grip.closest(".cat");
        dragging.classList.add("dragging");
        document.addEventListener("pointermove", onMove);
        document.addEventListener("pointerup", onUp);
        document.addEventListener("pointercancel", onUp);
      });
      grip.addEventListener("keydown", function (ev) {  // keyboard: arrow keys move the card
        var card = grip.closest(".cat");
        var moved = false;
        if ((ev.key === "ArrowUp" || ev.key === "ArrowLeft") && card.previousElementSibling) {
          grid.insertBefore(card, card.previousElementSibling); moved = true;
        } else if ((ev.key === "ArrowDown" || ev.key === "ArrowRight") && card.nextElementSibling) {
          grid.insertBefore(card.nextElementSibling, card); moved = true;
        }
        if (moved) { ev.preventDefault(); saveOrder(); grip.focus(); }
      });
    });
  }

  // ---------------------------------------------------------------- Movies / Shows toggle
  if (catalogForm) {
    catalogForm.querySelectorAll("input[name=type]").forEach(function (radio) {
      radio.addEventListener("change", function () {
        if (!radio.checked) return;
        var kind = radio.value === "series" ? "series" : "movie";
        catalogForm.setAttribute("data-kind", kind);
        document.querySelectorAll("[data-movie][data-series]").forEach(function (e) {
          e.textContent = e.getAttribute("data-" + kind);
        });
        document.querySelectorAll("[data-ph-movie][data-ph-series]").forEach(function (e) {
          e.placeholder = e.getAttribute("data-ph-" + kind);
        });
        // Genre lists differ: keep the genres both lists have, offer the new type's list.
        document.querySelectorAll("[data-options-series]").forEach(function (field) {
          var options = field.getAttribute("data-options-" + kind);
          var ids = (JSON.parse(options) || []).map(function (o) { return String(o[0]); });
          field.setAttribute("data-options", options);
          field.value = lines(field.value).filter(function (v) { return ids.indexOf(v) >= 0; }).join("\n");
          if (field._widget) {
            field._widget.remove();
            buildChips(field, field.getAttribute("data-source"));
          }
          fire(field);
        });
        var note = catalogForm.querySelector("[data-switch-note]");
        var saved = catalogForm.getAttribute("data-saved-kind");
        if (note) note.hidden = !saved || saved === kind;
      });
    });
  }

  // ---------------------------------------------------------------- reload when a run ends
  var runs = document.getElementById("runs");
  if (runs && runs.getAttribute("data-open-job")) {
    var url = runs.getAttribute("data-status-url");
    var misses = 0;
    function reloadHere() {
      clearInterval(timer);
      history.replaceState(null, "", window.location.pathname + "#p-runs");
      window.location.reload();
    }
    var timer = setInterval(function () {
      fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } })
        .then(function (r) {
          var json = (r.headers.get("content-type") || "").indexOf("application/json") === 0;
          if (r.ok && json && !r.redirected) return r.json();
          throw new Error("not the status");
        })
        .then(function (st) {
          misses = 0;
          if (st && !st.open_job) reloadHere();
        })
        .catch(function () {
          // Usually the sign-in expired (the proxy answers with its login page). Reloading lets it
          // sign in again and come back to this page instead of to the status URL.
          if (++misses >= 2) reloadHere();
        });
    }, 5000);
  }
})();
