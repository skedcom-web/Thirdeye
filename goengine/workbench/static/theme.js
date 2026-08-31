/* Third Eye front-end behavior: theme persistence, count-up metrics,
   cursor spotlight, mobile nav toggle. No framework, no build step. */
(function () {
  "use strict";

  var STORAGE_KEY = "thirdeye-theme";
  var THEMES = ["light", "dark", "glass"];

  function applyTheme(name) {
    if (THEMES.indexOf(name) === -1) name = "light";
    document.documentElement.setAttribute("data-theme", name);
    // Force a synchronous style recalculation. Belt-and-braces: an
    // attribute-selector cascade change should repaint on its own, but this
    // guarantees it in any embedding context that defers style recalc for a
    // backgrounded/non-composited view.
    void document.documentElement.offsetHeight;
    try { localStorage.setItem(STORAGE_KEY, name); } catch (e) { /* storage unavailable */ }
    document.querySelectorAll("[data-theme-btn]").forEach(function (btn) {
      btn.setAttribute("aria-pressed", String(btn.getAttribute("data-theme-btn") === name));
    });
  }

  function initTheme() {
    var saved = null;
    try { saved = localStorage.getItem(STORAGE_KEY); } catch (e) { /* ignore */ }
    applyTheme(saved || document.documentElement.getAttribute("data-theme") || "light");
  }

  function wireThemeToggle() {
    document.querySelectorAll("[data-theme-btn]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        applyTheme(btn.getAttribute("data-theme-btn"));
      });
    });
  }

  function wireMobileNav() {
    // Generic over both header shells (marketing .site-header and the
    // admin app-header) -- each page only ever has one of them.
    document.querySelectorAll("[data-nav-toggle]").forEach(function (toggle) {
      var header = toggle.closest(".site-header, .app-header");
      if (!header) return;
      toggle.addEventListener("click", function () {
        header.classList.toggle("nav-open");
        toggle.setAttribute("aria-expanded", header.classList.contains("nav-open") ? "true" : "false");
      });
    });
  }

  // Progressive enhancement over a plain checkbox list: the checkboxes
  // (name="departments", same values as always) stay exactly as they are in
  // the DOM the whole time -- this only adds a dropdown shell around them,
  // so the submitted form payload never changes and works identically with
  // JS disabled (the panel just renders inline instead of collapsing).
  // Written generically over [data-dept-multiselect] rather than one bucket
  // list specifically, so it keeps working unchanged if the option count
  // later grows from today's 4 buckets to the full department directory.
  function wireDeptMultiselect() {
    document.querySelectorAll("[data-dept-multiselect]").forEach(function (root) {
      var toggle = root.querySelector("[data-dept-toggle]");
      var panel = root.querySelector("[data-dept-panel]");
      var search = root.querySelector("[data-dept-search]");
      var summary = root.querySelector("[data-dept-summary]");
      var selectAllBtn = root.querySelector("[data-dept-select-all]");
      var clearBtn = root.querySelector("[data-dept-clear]");
      var options = Array.prototype.slice.call(root.querySelectorAll("[data-dept-option]"));
      if (!toggle || !panel) return;

      function checkboxes() {
        return options.map(function (opt) { return opt.querySelector("[data-dept-checkbox]"); });
      }

      function updateSummary() {
        var checked = checkboxes().filter(function (cb) { return cb.checked; });
        if (checked.length === 0) {
          summary.textContent = "All Departments";
        } else if (checked.length === options.length) {
          summary.textContent = "All Departments";
        } else {
          summary.textContent = checked.length + " selected";
        }
      }

      function open() {
        panel.hidden = false;
        toggle.setAttribute("aria-expanded", "true");
        if (search) search.focus();
      }
      function close() {
        panel.hidden = true;
        toggle.setAttribute("aria-expanded", "false");
      }

      toggle.addEventListener("click", function () {
        if (panel.hidden) open(); else close();
      });
      document.addEventListener("click", function (e) {
        if (!root.contains(e.target)) close();
      });
      document.addEventListener("keydown", function (e) {
        if (e.key === "Escape" && !panel.hidden) {
          close();
          toggle.focus();
        }
      });

      if (search) {
        search.addEventListener("input", function () {
          var term = search.value.trim().toLowerCase();
          options.forEach(function (opt) {
            var text = opt.getAttribute("data-dept-text") || "";
            opt.style.display = text.indexOf(term) === -1 ? "none" : "";
          });
        });
      }

      if (selectAllBtn) {
        selectAllBtn.addEventListener("click", function () {
          checkboxes().forEach(function (cb) { cb.checked = true; });
          updateSummary();
        });
      }
      if (clearBtn) {
        clearBtn.addEventListener("click", function () {
          checkboxes().forEach(function (cb) { cb.checked = false; });
          updateSummary();
        });
      }

      checkboxes().forEach(function (cb) {
        cb.addEventListener("change", updateSummary);
      });

      updateSummary();
    });
  }

  function wireAutoRefresh() {
    // Was a plain <meta http-equiv="refresh" content="2"> on job/diagnostic-
    // run pages -- a real usability bug: it reloaded the whole page (nav
    // included) every 2s regardless of what the admin was doing, so opening
    // a nav dropdown while a job ran meant it vanished out from under you
    // within moments. This still auto-refreshes a live-running job, just
    // never while a dropdown is actually open (checked again shortly after
    // instead of giving up), and on a gentler interval.
    var marker = document.querySelector("[data-auto-refresh]");
    if (!marker) return;
    var delay = parseInt(marker.getAttribute("data-auto-refresh"), 10) || 4000;
    function attempt() {
      var openDropdown = document.querySelector('.nav-group-toggle[aria-expanded="true"]');
      if (openDropdown) {
        setTimeout(attempt, 1500);
        return;
      }
      location.reload();
    }
    setTimeout(attempt, delay);
  }

  function wireSpotlight() {
    var hero = document.querySelector(".hero");
    if (!hero || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    hero.addEventListener("pointermove", function (e) {
      var rect = hero.getBoundingClientRect();
      var x = ((e.clientX - rect.left) / rect.width) * 100;
      var y = ((e.clientY - rect.top) / rect.height) * 100;
      hero.style.setProperty("--spot-x", x + "%");
      hero.style.setProperty("--spot-y", y + "%");
    });
  }

  function countUp(el) {
    var target = parseFloat(el.getAttribute("data-count-to") || "0");
    var duration = 1100;
    var start = null;
    var isInt = Number.isInteger(target);
    function step(ts) {
      if (start === null) start = ts;
      var progress = Math.min((ts - start) / duration, 1);
      var eased = 1 - Math.pow(1 - progress, 3);
      var value = target * eased;
      el.textContent = isInt ? Math.round(value).toLocaleString() : value.toFixed(1);
      if (progress < 1) requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
  }

  function wireCountUp() {
    var els = document.querySelectorAll("[data-count-to]");
    if (!els.length) return;
    if (!("IntersectionObserver" in window) || window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      els.forEach(function (el) { el.textContent = el.getAttribute("data-count-to"); });
      return;
    }
    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          countUp(entry.target);
          observer.unobserve(entry.target);
        }
      });
    }, { threshold: 0.4 });
    els.forEach(function (el) { observer.observe(el); });
  }

  // Fade/slide-in as `.reveal` elements enter the viewport. Unobserves
  // after firing once -- this is a first-impression flourish, not something
  // that should re-trigger every time a user scrolls back past a section.
  function wireScrollReveal() {
    var els = document.querySelectorAll(".reveal");
    if (!els.length) return;
    if (!("IntersectionObserver" in window) || window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      els.forEach(function (el) { el.classList.add("revealed"); });
      return;
    }
    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add("revealed");
          observer.unobserve(entry.target);
        }
      });
    }, { threshold: 0.15, rootMargin: "0px 0px -60px 0px" });
    els.forEach(function (el) { observer.observe(el); });
  }

  // Apply theme immediately (this file is loaded with `defer`, so DOM
  // exists, but we still run before paint-affecting layout settles).
  initTheme();

  document.addEventListener("DOMContentLoaded", function () {
    wireThemeToggle();
    wireMobileNav();
    wireDeptMultiselect();
    wireAutoRefresh();
    wireSpotlight();
    wireCountUp();
    wireScrollReveal();
  });
})();
