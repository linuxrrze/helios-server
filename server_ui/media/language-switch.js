/**
 * Keeps the input on a page when the visitor switches the language.
 *
 * The language switcher (server_ui/templates/_language_switcher.html) posts to
 * Django's set_language view, which redirects back to the same page; the page
 * is loaded again in the new language. Before that, this script copies what
 * the visitor has entered into sessionStorage and puts it back into the fields
 * of the reloaded page.
 *
 * Rules:
 * - Only sessionStorage, one entry, read and deleted on the very next page
 *   load (whatever page that is) and used only if it is for the same URL and
 *   at most MAX_AGE old.
 * - Never kept: password, file and hidden fields, read-only and disabled
 *   fields, fields whose name looks like a secret, and everything inside an
 *   element with the attribute data-no-preserve (secret keys, voter
 *   credentials, ...). If the visitor has changed such a field (or the page
 *   called HeliosFormState.markUnsaved()), the switcher asks before the input
 *   is lost.
 * - Saved are the fields the visitor changed; on a page that answers a POST
 *   (a form shown again with errors) all fields, because a plain reload would
 *   show the empty form.
 * - Pages with fields built by JavaScript mark that part with
 *   data-preserve-custom and register a hook:
 *     HeliosFormState.register(name, {save: function () { return data or null; },
 *                                     restore: function (data) { ... }});
 *   data must be JSON. A hook registered after the restore gets its data when
 *   it registers.
 *
 * Without JavaScript or without sessionStorage the switcher works as before
 * (the input is lost; with JavaScript the visitor is asked first).
 */
var HeliosFormState = (function () {
  'use strict';

  var STORAGE_KEY = 'helios-language-switch';
  var MAX_AGE = 2 * 60 * 1000;
  var MAX_SIZE = 512 * 1024;
  var SKIPPED_TYPES = /^(password|file|hidden|submit|button|reset|image)$/i;
  var SECRET_NAMES = /passw|secret|csrf|token|private_?key|^sk$|_sk$/i;
  var SECRET_AUTOCOMPLETE = /(current-password|new-password|one-time-code)/i;

  var hooks = {};
  var pendingHooks = {};
  var unsaved = false;    // changed something that is not kept
  var changed = false;    // changed anything

  function storage() {
    try {
      return window.sessionStorage || null;
    } catch (e) {
      return null;
    }
  }

  function here() {
    return window.location.pathname + window.location.search;
  }

  function closest(el, test) {
    for (; el && el.nodeType === 1; el = el.parentNode) {
      if (test(el)) return el;
    }
    return null;
  }

  function inSwitcher(el) {
    return !!closest(el, function (e) { return e.classList && e.classList.contains('language-switcher'); });
  }

  function excludedRegion(el) {
    return !!closest(el, function (e) { return e.hasAttribute('data-no-preserve'); });
  }

  function customRegion(el) {
    return !!closest(el, function (e) { return e.hasAttribute('data-preserve-custom'); });
  }

  function isField(el) {
    var tag = el && el.tagName ? el.tagName.toLowerCase() : '';
    return tag === 'input' || tag === 'select' || tag === 'textarea';
  }

  /** whether this field may be stored at all */
  function keepable(el) {
    if (!isField(el) || inSwitcher(el)) return false;
    if (el.tagName.toLowerCase() === 'input' && SKIPPED_TYPES.test(el.type || '')) return false;
    if (el.disabled || el.readOnly) return false;
    if (SECRET_NAMES.test(el.name || '') || SECRET_NAMES.test(el.id || '')) return false;
    if (SECRET_AUTOCOMPLETE.test(el.getAttribute('autocomplete') || '')) return false;
    if (excludedRegion(el) || customRegion(el)) return false;
    return true;
  }

  /** the fields handled here, each with a key that is the same after the reload */
  function fields() {
    var result = [];
    var seen = {};
    var forms = Array.prototype.slice.call(document.forms);
    var all = document.querySelectorAll('input, select, textarea');
    for (var i = 0; i < all.length; i++) {
      var el = all[i];
      if (!keepable(el)) continue;
      var form = el.form;
      var formKey = form ? (form.id ? '#' + form.id : 'form' + forms.indexOf(form)) : '-';
      var name = el.name || (el.id ? '#' + el.id : el.tagName.toLowerCase());
      if (el.type === 'checkbox' || el.type === 'radio') name += '=' + el.value;
      var base = formKey + '|' + name;
      var n = seen[base] = (seen[base] || 0) + 1;
      result.push({key: base + '|' + n, el: el});
    }
    return result;
  }

  function valueOf(el) {
    if (el.type === 'checkbox' || el.type === 'radio') return el.checked;
    if (el.tagName.toLowerCase() === 'select' && el.multiple) {
      var values = [];
      for (var i = 0; i < el.options.length; i++) {
        if (el.options[i].selected) values.push(el.options[i].value);
      }
      return values;
    }
    return el.value;
  }

  function isDefault(el) {
    if (el.type === 'checkbox' || el.type === 'radio') return el.checked === el.defaultChecked;
    if (el.tagName.toLowerCase() === 'select') {
      if (!el.multiple) {
        // without an explicit default the browser selects the first option
        var initial = 0;
        for (var j = 0; j < el.options.length; j++) {
          if (el.options[j].defaultSelected) initial = j;
        }
        return el.selectedIndex === initial;
      }
      for (var i = 0; i < el.options.length; i++) {
        if (el.options[i].selected !== el.options[i].defaultSelected) return false;
      }
      return true;
    }
    return el.value === el.defaultValue;
  }

  function setValue(el, value) {
    if (el.type === 'checkbox' || el.type === 'radio') {
      if (el.checked === !!value) return false;
      el.checked = !!value;
      return true;
    }
    if (el.tagName.toLowerCase() === 'select') {
      var wanted = Array.isArray(value) ? value : [value];
      var changedAny = false;
      for (var i = 0; i < el.options.length; i++) {
        var on = wanted.indexOf(el.options[i].value) !== -1;
        if (!el.multiple && !on) continue;
        if (el.options[i].selected !== on) {
          el.options[i].selected = on;
          changedAny = true;
        }
        if (!el.multiple && on) break;
      }
      return changedAny;
    }
    if (typeof value !== 'string' || el.value === value) return false;
    el.value = value;
    return true;
  }

  function fire(el, type) {
    var evt;
    try {
      evt = new Event(type, {bubbles: true});
    } catch (e) {
      evt = document.createEvent('Event');
      evt.initEvent(type, true, false);
    }
    el.dispatchEvent(evt);
  }

  /** the state of the page, or null if there is nothing to keep */
  function snapshot(allFields) {
    var values = {};
    var count = 0;
    fields().forEach(function (f) {
      if (!allFields && isDefault(f.el)) return;
      values[f.key] = valueOf(f.el);
      count++;
    });
    var custom = {};
    var customCount = 0;
    for (var name in hooks) {
      try {
        var data = hooks[name].save();
        if (data !== null && data !== undefined) {
          custom[name] = data;
          customCount++;
        }
      } catch (e) { /* a broken hook must not block the switch */ }
    }
    if (!count && !customCount) return null;
    return {url: here(), time: Date.now(), fields: values, hooks: custom,
            scrollX: window.pageXOffset || 0, scrollY: window.pageYOffset || 0};
  }

  function save(state) {
    var store = storage();
    if (!store) return false;
    try {
      var text = JSON.stringify(state);
      if (text.length > MAX_SIZE) return false;
      store.setItem(STORAGE_KEY, text);
      return true;
    } catch (e) {
      return false;
    }
  }

  function discard() {
    var store = storage();
    try {
      if (store) store.removeItem(STORAGE_KEY);
    } catch (e) { /* nothing to do */ }
  }

  /** read and delete the saved state; null unless it is for this page and fresh */
  function take() {
    var store = storage();
    if (!store) return null;
    var text = null;
    try {
      text = store.getItem(STORAGE_KEY);
      store.removeItem(STORAGE_KEY);
    } catch (e) {
      return null;
    }
    if (!text) return null;
    try {
      var state = JSON.parse(text);
      var age = Date.now() - state.time;
      if (state.url !== here() || !(age >= 0 && age <= MAX_AGE)) return null;
      return state;
    } catch (e) {
      return null;
    }
  }

  function restoreHook(name, data) {
    try {
      hooks[name].restore(data);
    } catch (e) { /* keep the rest of the page working */ }
  }

  function restore(state) {
    for (var name in state.hooks) {
      if (hooks[name]) restoreHook(name, state.hooks[name]);
      else pendingHooks[name] = state.hooks[name];
    }
    var values = state.fields || {};
    fields().forEach(function (f) {
      if (!Object.prototype.hasOwnProperty.call(values, f.key)) return;
      try {
        if (setValue(f.el, values[f.key])) {
          // dependent display (time zone line below date fields, ...)
          fire(f.el, 'input');
          fire(f.el, 'change');
        }
      } catch (e) { /* next field */ }
    });
    if (state.scrollY || state.scrollX) {
      try {
        window.scrollTo(state.scrollX, state.scrollY);
      } catch (e) { /* not important */ }
    }
  }

  function track(evt) {
    var el = evt.target;
    if (!isField(el) || inSwitcher(el)) return;
    changed = true;
    if (!keepable(el) && !customRegion(el)) unsaved = true;
  }

  function onSwitch(evt) {
    var form = evt.currentTarget || evt.target;
    var state = null;
    try {
      state = snapshot(form.getAttribute('data-page-method') === 'POST');
    } catch (e) {
      state = null;
    }
    var kept = state ? save(state) : true;
    var message = null;
    if (!kept && (changed || unsaved)) message = form.getAttribute('data-confirm-lost');
    else if (unsaved) message = form.getAttribute('data-confirm-partial');
    if (message && !window.confirm(message)) {
      discard();
      evt.preventDefault();
    }
  }

  function init() {
    document.addEventListener('input', track, true);
    document.addEventListener('change', track, true);
    // a submitted page form saves its input; the next page starts clean
    document.addEventListener('submit', function (evt) {
      if (!inSwitcher(evt.target)) {
        unsaved = false;
        changed = false;
      }
    }, true);
    var switchers = document.querySelectorAll('form.language-switcher');
    for (var i = 0; i < switchers.length; i++) {
      switchers[i].addEventListener('submit', onSwitch);
    }

    var state = take();
    if (!state) return;
    // after the page's own scripts have built the page (jQuery ready etc.)
    var run = function () { setTimeout(function () { restore(state); }, 0); };
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', run);
    else run();
  }

  init();

  return {
    /** register a hook for fields built by JavaScript (see above) */
    register: function (name, hook) {
      hooks[name] = hook;
      if (Object.prototype.hasOwnProperty.call(pendingHooks, name)) {
        var data = pendingHooks[name];
        delete pendingHooks[name];
        restoreHook(name, data);
      }
    },
    /** the page has state that switching the language would lose (generated keys, ...) */
    markUnsaved: function () {
      unsaved = true;
    }
  };
})();
