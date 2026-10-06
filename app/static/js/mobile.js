/* Command Nexus — Mobile nav toggle */
(function() {
  // Inject hamburger button into header-left
  const headerLeft = document.querySelector('.header-left');
  if (!headerLeft) return;

  const nav = headerLeft.querySelector('.nav-links, .nav-links-top');
  if (!nav) return;

  const btn = document.createElement('button');
  btn.className = 'mobile-toggle';
  btn.innerHTML = '&#x2630;';
  btn.setAttribute('aria-label', 'Menu');
  btn.onclick = function() {
    nav.classList.toggle('open');
    btn.innerHTML = nav.classList.contains('open') ? '&#x2715;' : '&#x2630;';
  };

  // Insert before the nav
  headerLeft.insertBefore(btn, nav);

  // Close menu when a link is clicked
  nav.querySelectorAll('.nav-link').forEach(function(link) {
    link.addEventListener('click', function() {
      nav.classList.remove('open');
      btn.innerHTML = '&#x2630;';
    });
  });

  // Filter sidebar collapse toggle on mobile
  var sidebar = document.querySelector('.filter-sidebar');
  if (sidebar && window.innerWidth <= 768) {
    sidebar.classList.add('collapsed');
    var toggle = document.createElement('div');
    toggle.className = 'filter-toggle-row';
    toggle.style.cssText = 'display:flex;justify-content:space-between;align-items:center;cursor:pointer;padding:4px 0';
    toggle.innerHTML = '<span style="font-size:12px;font-weight:600">Filters</span><span style="font-size:16px;color:var(--text-muted)">&#x25BC;</span>';
    toggle.onclick = function() {
      sidebar.classList.toggle('collapsed');
      toggle.querySelector('span:last-child').innerHTML = sidebar.classList.contains('collapsed') ? '&#x25BC;' : '&#x25B2;';
    };
    sidebar.insertBefore(toggle, sidebar.firstChild);
  }
})();
