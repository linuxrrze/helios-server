/**
 * Helios time zone display.
 *
 * The server shows election times in the election time zone (ELECTION_TIME_ZONE)
 * and converts the date fields from that zone; this script only adds
 * information, the pages work without it:
 *
 * - <time class="tz-timestamp" data-utc-time="2026-11-20T09:00Z" data-time-zone="Europe/Berlin">
 *   gets the visitor's own local time appended if their browser is in a zone
 *   with a different offset at that moment;
 * - <input type="datetime-local" data-time-zone="Europe/Berlin"> gets a line
 *   below it with the entered time in UTC and, if different, in the visitor's
 *   zone. Inputs without data-time-zone are taken as UTC (older templates).
 *
 * Uses only the browser's Intl API.
 */

var HeliosTimezone = (function() {
  'use strict';

  /**
   * Locale for date formatting: the page language (<html lang>) if the
   * browser supports it, otherwise the browser default (undefined).
   */
  function getPageLocale() {
    var lang = document.documentElement && document.documentElement.lang;
    if (!lang) {
      return undefined;
    }
    try {
      return Intl.DateTimeFormat.supportedLocalesOf([lang]).length ? lang : undefined;
    } catch (e) {
      return undefined;
    }
  }

  var locale = getPageLocale();

  // UI labels; English unless the page language has an entry here
  // (this file is used without the Django JavaScript catalog)
  var LABELS = {
    en: {
      yourTime: 'your time',
      inUTC: 'In UTC:',
      inYourZone: 'In your time zone ({zone}):'
    },
    de: {
      yourTime: 'Ihre Zeit',
      inUTC: 'In UTC:',
      inYourZone: 'In Ihrer Zeitzone ({zone}):'
    }
  };
  var labels = LABELS[(locale || '').split('-')[0].toLowerCase()] || LABELS.en;

  var FORMAT = { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' };

  function browserZone() {
    try {
      return Intl.DateTimeFormat().resolvedOptions().timeZone || null;
    } catch (e) {
      return null;
    }
  }

  function format(date, zone, withZoneName) {
    var options = {};
    for (var k in FORMAT) options[k] = FORMAT[k];
    if (zone) options.timeZone = zone;
    if (withZoneName) options.timeZoneName = 'short';
    try {
      return new Intl.DateTimeFormat(locale, options).format(date);
    } catch (e) {
      return date.toISOString().slice(0, 16).replace('T', ' ') + ' UTC';
    }
  }

  /** offset of zone from UTC at time t (ms since epoch), in ms */
  function offsetAt(t, zone) {
    var parts = new Intl.DateTimeFormat('en-US', {
      timeZone: zone, hourCycle: 'h23', year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit'
    }).formatToParts(new Date(t));
    var v = {};
    parts.forEach(function(p) { v[p.type] = parseInt(p.value, 10); });
    var asUTC = Date.UTC(v.year, v.month - 1, v.day, v.hour % 24, v.minute, v.second);
    return asUTC - Math.floor(t / 1000) * 1000;
  }

  /** a wall-clock time "YYYY-MM-DDTHH:MM" in zone -> Date, or null */
  function wallTimeToDate(value, zone) {
    var m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(value || '');
    if (!m) return null;
    var guess = Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]);
    if (!zone || zone === 'UTC') return new Date(guess);
    try {
      // the offsets a day before and after; a time that occurs twice (clocks
      // go back) is taken as the earlier one, as the server does; a time that
      // is skipped (clocks go forward) has no valid offset and gives null
      var day = 86400000, best = null;
      [offsetAt(guess - day, zone), offsetAt(guess + day, zone)].forEach(function(offset) {
        var t = guess - offset;
        if (offsetAt(t, zone) === offset && (best === null || t < best)) best = t;
      });
      return best === null ? null : new Date(best);
    } catch (e) {
      return null;
    }
  }

  /** whether the visitor's zone shows a different wall time than zone at date */
  function differsFromBrowser(date, zone) {
    var own = browserZone();
    if (!own || own === zone) return false;
    try {
      return offsetAt(date.getTime(), own) !== offsetAt(date.getTime(), zone || 'UTC');
    } catch (e) {
      return false;
    }
  }

  function parseUTC(value) {
    if (!value) return null;
    var date = new Date(/Z$/.test(value) ? value : value.replace(' ', 'T') + 'Z');
    return isNaN(date.getTime()) ? null : date;
  }

  /** append the visitor's local time to a server-rendered timestamp */
  function convertTimestamp(element) {
    if (!element || element.classList.contains('tz-converted')) return;
    var date = parseUTC(element.getAttribute('data-utc-time'));
    var zone = element.getAttribute('data-time-zone') || 'UTC';
    element.classList.add('tz-converted');
    if (!date || !differsFromBrowser(date, zone)) return;
    var span = document.createElement('span');
    span.className = 'tz-viewer';
    span.textContent = ' (' + labels.yourTime + ': ' + format(date, null, true) + ')';
    element.parentNode.insertBefore(span, element.nextSibling);
  }

  /** the line below a date field */
  function updateDateTimeInputHelper(input) {
    if (!input) return;
    var helper = document.getElementById(input.id + '_tzhelper');
    var zone = input.getAttribute('data-time-zone') || 'UTC';
    var date = wallTimeToDate(input.value, zone);
    if (!date) {
      if (helper) helper.textContent = '';
      return;
    }
    if (!helper) {
      helper = document.createElement('div');
      helper.className = 'tz-input-helper';
      helper.id = (input.id || input.name) + '_tzhelper';
      helper.setAttribute('aria-live', 'polite');
      input.parentNode.insertBefore(helper, input.nextSibling);
      var described = input.getAttribute('aria-describedby');
      input.setAttribute('aria-describedby', (described ? described + ' ' : '') + helper.id);
    }
    var text = labels.inUTC + ' ' + format(date, 'UTC', false);
    if (zone !== 'UTC' && differsFromBrowser(date, zone)) {
      text += ' · ' + labels.inYourZone.replace('{zone}', browserZone()) + ' ' + format(date, null, false);
    }
    helper.textContent = text;
  }

  function init() {
    Array.prototype.forEach.call(document.querySelectorAll('.tz-timestamp[data-utc-time]'), convertTimestamp);
    Array.prototype.forEach.call(document.querySelectorAll('input[type="datetime-local"]'), function(input) {
      input.addEventListener('change', function() { updateDateTimeInputHelper(this); });
      input.addEventListener('input', function() { updateDateTimeInputHelper(this); });
      if (input.value) updateDateTimeInputHelper(input);
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  return {
    init: init,
    convertTimestamp: convertTimestamp,
    updateDateTimeInputHelper: updateDateTimeInputHelper,
    wallTimeToDate: wallTimeToDate,
    offsetAt: offsetAt
  };
})();
