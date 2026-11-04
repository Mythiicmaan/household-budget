/* Household Budget: small progressive enhancements. Everything works without JS. */
(function () {
  "use strict";

  function money(n) {
    var neg = n < 0;
    var s = Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    return (neg ? "-$" : "$") + s;
  }
  function num(v) {
    var x = parseFloat(String(v || "").replace(/[$,\s]/g, ""));
    return isNaN(x) ? 0 : x;
  }

  function init(root) {
    root = root || document;

    // Quick add: open the sheet and focus the amount field inside the tap handler,
    // so iOS shows the number pad right away.
    root.querySelectorAll("[data-open-add]").forEach(function (btn) {
      btn.addEventListener("click", function (e) {
        var dlg = document.getElementById("quick-add");
        if (!dlg || !dlg.showModal) return; // falls back to the /add link
        e.preventDefault();
        dlg.showModal();
        var amt = dlg.querySelector("input[name=amount]");
        if (amt) amt.focus();
      });
    });
    root.querySelectorAll("[data-close-add]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var dlg = document.getElementById("quick-add");
        if (dlg) dlg.close();
      });
    });

    // Description autocomplete picks that description's usual category.
    var mapEl = document.getElementById("desc-map");
    var descMap = {};
    try { descMap = mapEl ? JSON.parse(mapEl.textContent) : {}; } catch (_) { descMap = {}; }
    root.querySelectorAll("input[data-desc]").forEach(function (inp) {
      inp.addEventListener("change", function () {
        var form = inp.form;
        var cat = descMap[inp.value.trim().toLowerCase()];
        if (!cat || !form) return;
        var chosen = form.querySelector("input[name=category_id]:checked");
        if (chosen && chosen.value !== "") return;
        var chip = form.querySelector("input[name=category_id][value='" + cat + "']");
        if (chip) chip.checked = true;
      });
    });

    // Tap a selected category chip again to clear it.
    root.querySelectorAll(".chips input[type=radio]").forEach(function (r) {
      r.addEventListener("click", function () {
        if (r.dataset.was === "1") {
          r.checked = false;
          r.dataset.was = "0";
          var none = r.form && r.form.querySelector("input[name=" + r.name + "][value='']");
          if (none) none.checked = true;
        } else {
          (r.form || document).querySelectorAll("input[name=" + r.name + "]").forEach(function (o) { o.dataset.was = "0"; });
          r.dataset.was = "1";
        }
      });
      if (r.checked) r.dataset.was = "1";
    });

    // Card picker only makes sense for money out.
    root.querySelectorAll("form[data-entry]").forEach(function (form) {
      function sync() {
        var dir = form.querySelector("input[name=direction]:checked");
        var cardField = form.querySelector("[data-card-field]");
        if (cardField) cardField.hidden = dir && dir.value === "in";
      }
      form.querySelectorAll("input[name=direction]").forEach(function (r) { r.addEventListener("change", sync); });
      sync();
    });

    // Prevent double submits (the server also ignores repeated tokens).
    root.querySelectorAll("form[data-once]").forEach(function (form) {
      form.addEventListener("submit", function () {
        form.querySelectorAll("button[type=submit]").forEach(function (b) {
          setTimeout(function () { b.disabled = true; }, 0);
        });
      });
    });

    // Live remainder on paycheck cards.
    root.querySelectorAll("form[data-check-form]").forEach(function (form) {
      function recalc() {
        var total = num(form.querySelector("input[name=amount]").value);
        form.querySelectorAll("[data-line]").forEach(function (row) {
          var del = row.querySelector("input[type=checkbox]");
          if (del && del.checked) return;
          total -= Math.abs(num(row.querySelector("input[data-amt]").value));
        });
        var extra = form.querySelector("input[name=new-amount]");
        if (extra) total -= Math.abs(num(extra.value));
        var out = form.querySelector("[data-remainder]");
        if (out) {
          out.textContent = money(total);
          out.classList.toggle("neg", total < 0);
        }
      }
      form.addEventListener("input", recalc);
      form.addEventListener("change", recalc);
    });

    // New month: live true starting balance.
    root.querySelectorAll("form[data-new-month]").forEach(function (form) {
      function recalc() {
        var bank = form.querySelector("input[name=bank_balance]");
        var out = form.querySelector("[data-true-start]");
        if (!bank || !out) return;
        if (!bank.value.trim()) { out.textContent = "—"; return; }
        var total = num(bank.value);
        form.querySelectorAll("input[name=cleared]").forEach(function (c) {
          if (!c.checked) total += num(c.dataset.amount);
        });
        out.textContent = money(total);
      }
      form.addEventListener("input", recalc);
      form.addEventListener("change", recalc);
      recalc();
    });

    // Toasts fade away on their own.
    root.querySelectorAll(".toast").forEach(function (t) {
      setTimeout(function () { t.classList.add("hide"); }, 7000);
    });

    // History chart.
    var data = root.querySelector ? root.querySelector("#chart-data") : null;
    var canvas = document.getElementById("history-chart");
    if (data && canvas && window.Chart) {
      var d = JSON.parse(data.textContent);
      new window.Chart(canvas, {
        data: {
          labels: d.labels,
          datasets: [
            { type: "bar", label: "Money in", data: d.in, backgroundColor: "rgba(34,197,94,.7)" },
            { type: "bar", label: "Money out", data: d.out, backgroundColor: "rgba(239,68,68,.7)" },
            { type: "line", label: "Net", data: d.net, borderColor: "#2563eb", backgroundColor: "#2563eb", tension: 0.25 }
          ]
        },
        options: {
          maintainAspectRatio: false,
          interaction: { mode: "index", intersect: false },
          scales: { y: { ticks: { callback: function (v) { return "$" + v.toLocaleString(); } } } }
        }
      });
    }
  }

  if (window.htmx) {
    window.htmx.onLoad(function (el) { init(el === document.body ? document : el); });
  } else {
    document.addEventListener("DOMContentLoaded", function () { init(document); });
  }
})();
