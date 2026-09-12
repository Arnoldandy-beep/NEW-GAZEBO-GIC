/* GIC-MS design system — theme switcher (light/dark), shared by all mockups. */
(function () {
  var KEY = 'gic-theme';
  var root = document.documentElement;

  function apply(theme) {
    root.setAttribute('data-theme', theme);
    try { localStorage.setItem(KEY, theme); } catch (e) { /* private mode */ }
    document.querySelectorAll('[data-theme-toggle]').forEach(function (btn) {
      var icon = btn.querySelector('i');
      if (icon) icon.className = theme === 'dark' ? 'ph ph-sun' : 'ph ph-moon-stars';
      btn.setAttribute('aria-label', theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode');
      var label = btn.querySelector('[data-theme-label]');
      if (label) label.textContent = theme === 'dark' ? 'Light mode' : 'Dark mode';
    });
  }

  var stored;
  try { stored = localStorage.getItem(KEY); } catch (e) { stored = null; }
  apply(stored || root.getAttribute('data-theme') || 'light');

  document.addEventListener('click', function (ev) {
    var btn = ev.target.closest('[data-theme-toggle]');
    if (!btn) return;
    apply(root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark');
  });
})();
