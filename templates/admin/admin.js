// AI Catalogs editor. No inline script anywhere (CSP).
(function () {
  "use strict";

  // Sliders mirror their number box; the box is what gets submitted (empty = no limit).
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

  // Tabs. The URL hash picks one, so a redirect can land on Activity.
  var tabs = Array.prototype.slice.call(document.querySelectorAll(".tabs a"));
  function show(hash) {
    var found = false;
    tabs.forEach(function (t) {
      var on = t.getAttribute("href") === hash;
      found = found || on;
      t.classList.toggle("active", on);
      var target = document.querySelector(t.getAttribute("href"));
      if (target) target.classList.toggle("hidden-tab", !on);
    });
    return found;
  }
  tabs.forEach(function (tab) {
    tab.addEventListener("click", function (ev) {
      ev.preventDefault();
      show(tab.getAttribute("href"));
      if (history.replaceState) history.replaceState(null, "", tab.getAttribute("href"));
    });
  });
  if (location.hash && tabs.length) show(location.hash);

  // Refresh schedule: show only the inputs the chosen option needs.
  var modes = document.querySelectorAll("input[name=sched_mode]");
  function syncSchedule() {
    var mode = "manual";
    modes.forEach(function (m) { if (m.checked) mode = m.value; });
    document.querySelectorAll("[data-sched]").forEach(function (el) {
      el.hidden = el.getAttribute("data-sched").split(" ").indexOf(mode) < 0;
    });
  }
  modes.forEach(function (m) { m.addEventListener("change", syncSchedule); });
  if (modes.length) syncSchedule();

  // An empty time zone defaults to this browser's.
  document.querySelectorAll("input[data-autotz]").forEach(function (box) {
    if (box.value) return;
    try { box.value = Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch (e) { /* keep empty: UTC */ }
  });

  // Copy buttons.
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

  // While a run is in progress, reload the page when it ends.
  var runs = document.getElementById("runs");
  if (runs && runs.getAttribute("data-open-job")) {
    var url = runs.getAttribute("data-status-url");
    var timer = setInterval(function () {
      fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (st) {
          if (st && !st.open_job) { clearInterval(timer); window.location.replace(window.location.pathname + "#activity-tab"); window.location.reload(); }
        })
        .catch(function () {});
    }, 5000);
  }
})();
