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

  // Compact theme picker (public pages): icon button opens a dropdown menu.
  // Theme selection itself is still handled by wireThemeToggle above, since
  // the menu's buttons carry the same [data-theme-btn] attribute -- this
  // only owns open/close: click the icon, pick a theme, click outside, or Esc.
  function wireThemePicker() {
    document.querySelectorAll(".theme-picker").forEach(function (picker) {
      var toggle = picker.querySelector("[data-theme-picker-toggle]");
      if (!toggle) return;
      function close() {
        picker.classList.remove("open");
        toggle.setAttribute("aria-expanded", "false");
      }
      function open() {
        picker.classList.add("open");
        toggle.setAttribute("aria-expanded", "true");
      }
      toggle.addEventListener("click", function (e) {
        e.stopPropagation();
        if (picker.classList.contains("open")) close(); else open();
      });
      picker.querySelectorAll("[data-theme-btn]").forEach(function (btn) {
        btn.addEventListener("click", close);
      });
      document.addEventListener("click", function (e) {
        if (picker.classList.contains("open") && !picker.contains(e.target)) close();
      });
      document.addEventListener("keydown", function (e) {
        if (e.key === "Escape") close();
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
    wireThemePicker();
    wireMobileNav();
    wireSpotlight();
    wireCountUp();
    wireScrollReveal();
  });
})();
