(function () {
  'use strict';
  var book = document.getElementById('book');
  var pages = Array.prototype.slice.call(book.querySelectorAll('.page'));
  var prev = document.getElementById('prev');
  var next = document.getElementById('next');
  var count = document.getElementById('count');
  var current = 0;

  pages.forEach(function (page, i) { page.style.zIndex = pages.length - i; });

  function show(index) {
    current = Math.max(0, Math.min(pages.length - 1, index));
    pages.forEach(function (page, i) {
      page.classList.toggle('turned', i < current);
      page.setAttribute('aria-hidden', i === current ? 'false' : 'true');
    });
    prev.disabled = current === 0;
    next.disabled = current === pages.length - 1;
    count.textContent = (current + 1).toLocaleString('ar-IQ') + ' / ' + pages.length.toLocaleString('ar-IQ');
  }

  next.addEventListener('click', function () { show(current + 1); });
  prev.addEventListener('click', function () { show(current - 1); });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'ArrowLeft') show(current + 1);
    if (e.key === 'ArrowRight') show(current - 1);
  });

  // السحب يميناً يقلب إلى الأمام كما يُقلب الورق العربي باليد.
  var startX = null;
  book.addEventListener('pointerdown', function (e) { startX = e.clientX; });
  window.addEventListener('pointerup', function (e) {
    if (startX === null) return;
    var dx = e.clientX - startX;
    startX = null;
    if (Math.abs(dx) > 40) show(current + (dx > 0 ? 1 : -1));
    else if (book.contains(e.target)) show(current + 1);
  });

  show(0);
})();
