/**
 * Preview of an election's address (helios/templates/helios/widgets/election_address.html):
 * shows the whole link while the administrator types the chosen part. The form works
 * without it; the fixed part of the link is shown in front of the box anyway.
 */
(function() {
  'use strict';

  function setUp(preview) {
    var input = document.getElementById(preview.getAttribute('data-for'));
    var url = preview.querySelector('.address-url');
    if (!input || !url || preview.getAttribute('data-ready')) {
      return;
    }
    preview.setAttribute('data-ready', '1');
    var base = preview.getAttribute('data-base');
    var prefix = preview.getAttribute('data-prefix');

    function update() {
      var value = input.value.trim();
      if (!value) {
        url.textContent = preview.getAttribute('data-automatic-url') || preview.getAttribute('data-automatic-text');
      } else if (value === preview.getAttribute('data-keep')) {
        // unchanged: the address stays as it is
        url.textContent = preview.getAttribute('data-keep-url');
      } else {
        url.textContent = base + (prefix ? prefix + '-' : '') + value;
      }
    }

    input.addEventListener('input', update);
    update();
    preview.hidden = false;
  }

  function init() {
    var previews = document.querySelectorAll('.address-preview[data-for]');
    for (var i = 0; i < previews.length; i++) {
      setUp(previews[i]);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
