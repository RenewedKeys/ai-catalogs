// Catalog editor: slider <-> number box sync, tabs, and polling while a job is open.
// The number box is the submitted value; the slider is only a convenience. No inline script (CSP).
(function () {
  "use strict";
  document.querySelectorAll('input[type=range][data-for]').forEach(function (range) {
    var box = document.getElementById(range.getAttribute("data-for"));
    if (!box) return;
    range.removeAttribute("tabindex");
    range.addEventListener("input", function () { box.value = range.value; });
    box.addEventListener("input", function () {
      var n = parseFloat(box.value);
      if (!isNaN(n)) range.value = n;
    });
  });

  document.querySelectorAll("button[data-copy]").forEach(function (button) {
    button.addEventListener("click", function () {
      var box = document.getElementById(button.getAttribute("data-copy"));
      if (!box) return;
      box.select();
      var done = function () { button.textContent = "Copied"; };
      if (navigator.clipboard) navigator.clipboard.writeText(box.value).then(done, function () {});
      else if (document.execCommand("copy")) done();
    });
  });

  var tabs = document.querySelectorAll(".tabs a");
  tabs.forEach(function (tab) {
    tab.addEventListener("click", function (ev) {
      ev.preventDefault();
      tabs.forEach(function (t) {
        var target = document.querySelector(t.getAttribute("href"));
        var on = t === tab;
        t.classList.toggle("active", on);
        if (target) target.classList.toggle("hidden-tab", !on);
      });
    });
  });

  var runs = document.getElementById("runs");
  if (runs && runs.getAttribute("data-open-job")) {
    var url = runs.getAttribute("data-status-url");
    var timer = setInterval(function () {
      fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (st) {
          if (st && !st.open_job) { clearInterval(timer); window.location.href = window.location.pathname + "#runs"; }
        })
        .catch(function () {});
    }, 5000);
  }
})();
