// GAZEBO GIC — small UI helpers
document.addEventListener('DOMContentLoaded', function () {

  // Confirm prompts on data-confirm forms/buttons
  document.querySelectorAll('[data-confirm]').forEach(function (el) {
    el.addEventListener('submit', function (e) {
      if (!confirm(el.getAttribute('data-confirm'))) {
        e.preventDefault();
      }
    });
    el.addEventListener('click', function (e) {
      if (el.tagName === 'A' && !confirm(el.getAttribute('data-confirm'))) {
        e.preventDefault();
      }
    });
  });

  // Auto-format thousand separators in number inputs as user types (display only)
  document.querySelectorAll('input.money-input').forEach(function (inp) {
    inp.addEventListener('input', function () {
      var v = inp.value.replace(/[^0-9]/g, '');
      if (v.length) {
        inp.value = parseInt(v, 10).toLocaleString();
      } else {
        inp.value = '';
      }
    });
  });

  // Close sidebar when clicking outside on mobile
  var sidebar = document.getElementById('sidebar');
  if (sidebar) {
    document.addEventListener('click', function (e) {
      if (window.innerWidth > 768) return;
      if (sidebar.classList.contains('open')
          && !sidebar.contains(e.target)
          && !e.target.closest('.sidebar-toggle')) {
        sidebar.classList.remove('open');
      }
    });
  }
});
