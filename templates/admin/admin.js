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
        fetch(PREFIX + "/suggest?kind=" + encodeURIComponent(kind) + "&q=" + encodeURIComponent(q), {
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

  document.querySelectorAll("[data-widget]").forEach(function (field) {
    var widget = field.getAttribute("data-widget"), source = field.getAttribute("data-source");
    if (widget === "chips") {
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
      var opts = { options: optionsOf(field), kind: source === "movie" ? "movie" : null, free: source === "free" };
      mount(field, chipEditor(values, opts, function (v) {
        field.value = asList ? (v.length ? JSON.stringify(v) : "") : v.join("\n");
        fire(field);
      }, CHIP_HINTS[source]));
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

  // ---------------------------------------------------------------- tabs
  var tabs = Array.prototype.slice.call(document.querySelectorAll(".tabs a"));
  function show(hash) {
    tabs.forEach(function (t) {
      var on = t.getAttribute("href") === hash;
      t.classList.toggle("active", on);
      var target = document.querySelector(t.getAttribute("href"));
      if (target) target.classList.toggle("hidden-tab", !on);
    });
  }
  tabs.forEach(function (tab) {
    tab.addEventListener("click", function (ev) {
      ev.preventDefault();
      show(tab.getAttribute("href"));
      if (history.replaceState) history.replaceState(null, "", tab.getAttribute("href"));
    });
  });
  if (location.hash && tabs.some(function (t) { return t.getAttribute("href") === location.hash; })) {
    show(location.hash);
    window.scrollTo(0, 0);  // open the tab, don't jump past the header and notice
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
  document.querySelectorAll("form[data-confirm]").forEach(function (f) {
    f.addEventListener("submit", function (ev) {
      if (!window.confirm(f.getAttribute("data-confirm"))) ev.preventDefault();
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
      if (dirty && !window.confirm("Discard your changes?")) ev.preventDefault();
      else dirty = false;
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
      // The card whose centre is closest to the pointer decides the slot.
      var cards = Array.prototype.filter.call(grid.querySelectorAll(".cat"), function (c) { return c !== dragging; });
      var best = null, bestD = Infinity;
      cards.forEach(function (c) {
        var r = c.getBoundingClientRect();
        var d = Math.hypot(ev.clientX - (r.left + r.width / 2), ev.clientY - (r.top + r.height / 2));
        if (d < bestD) { bestD = d; best = c; }
      });
      if (!best) return;
      var r = best.getBoundingClientRect();
      var sameRow = ev.clientY > r.top && ev.clientY < r.bottom;
      var after = sameRow ? ev.clientX > r.left + r.width / 2 : ev.clientY > r.top + r.height / 2;
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

  // ---------------------------------------------------------------- reload when a run ends
  var runs = document.getElementById("runs");
  if (runs && runs.getAttribute("data-open-job")) {
    var url = runs.getAttribute("data-status-url");
    var timer = setInterval(function () {
      fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (st) {
          if (st && !st.open_job) { clearInterval(timer); window.location.hash = "#activity-tab"; window.location.reload(); }
        })
        .catch(function () {});
    }, 5000);
  }
})();
