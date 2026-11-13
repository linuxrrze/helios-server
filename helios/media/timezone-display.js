/**
 * Helios Timezone Display Utility
 * Uses built-in browser Intl API for timezone conversion
 */

var HeliosTimezone = (function() {
  'use strict';

  /**
   * Locale for date formatting: the page language (<html lang>, set from the
   * active Django language) if the browser supports it, otherwise the
   * browser default (undefined).
   * @returns {string|undefined}
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
      local: 'Local',
      utcTitle: 'Universal Coordinated Time',
      localTitle: 'Your local timezone',
      thisTimeIs: 'This time is:'
    },
    de: {
      local: 'Lokal',
      utcTitle: 'Koordinierte Weltzeit',
      localTitle: 'Ihre lokale Zeitzone',
      thisTimeIs: 'Dieser Zeitpunkt ist:'
    }
  };
  var labels = LABELS[(locale || '').split('-')[0].toLowerCase()] || LABELS.en;

  // Module configuration
  var config = {
    utcFormatter: new Intl.DateTimeFormat(locale, {
      timeZone: 'UTC',
      year: 'numeric',
      month: 'short',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false
    }),
    localFormatter: new Intl.DateTimeFormat(locale, {
      year: 'numeric',
      month: 'short',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false
    })
  };

  /**
   * Get local timezone name/abbreviation using Intl API
   * @returns {string} Timezone abbreviation or 'Local' (translated)
   */
  function getLocalTimezoneName() {
    try {
      var formatter = new Intl.DateTimeFormat(locale, {
        timeZoneName: 'short'
      });
      var parts = formatter.formatToParts(new Date());
      var tzPart = parts.find(function(part) { return part.type === 'timeZoneName'; });
      return tzPart ? tzPart.value : labels.local;
    } catch (e) {
      return labels.local;
    }
  }

  /**
   * Parse UTC datetime string into Date object
   * @param {string} utcDateStr - UTC datetime string
   * @returns {Date|null} Date object or null if invalid
   */
  function parseUTCDate(utcDateStr) {
    if (!utcDateStr || typeof utcDateStr !== 'string') {
      return null;
    }

    // Try parsing with ' UTC' suffix first
    var date = new Date(utcDateStr + ' UTC');

    if (isNaN(date.getTime())) {
      // Try without ' UTC' suffix
      date = new Date(utcDateStr);
      if (isNaN(date.getTime())) {
        return null;
      }
    }

    return date;
  }

  /**
   * Check if user is in UTC timezone
   * @param {Date} date - Date object to check
   * @returns {boolean} True if in UTC timezone
   */
  function isUTCTimezone(date) {
    return date.getTimezoneOffset() === 0;
  }

  /**
   * Create timezone display HTML
   * @param {Date} date - Date object to format
   * @returns {string} HTML string for display
   */
  function createTimezoneHTML(date) {
    var utcFormatted = config.utcFormatter.format(date);
    var localFormatted = config.localFormatter.format(date);
    var localTz = getLocalTimezoneName();

    if (isUTCTimezone(date)) {
      return '<strong>' + utcFormatted + ' UTC</strong>';
    }

    return '<span class="tz-utc" title="' + labels.utcTitle + '">' +
           utcFormatted + ' UTC</span>' +
           '<span class="tz-separator"> / </span>' +
           '<span class="tz-local" title="' + labels.localTitle + '">' +
           localFormatted + ' ' + localTz + '</span>';
  }

  /**
   * Convert a UTC timestamp element to show both UTC and local time
   * @param {HTMLElement} element - Element to convert
   */
  function convertTimestamp(element) {
    if (!element || element.classList.contains('tz-converted')) {
      return;
    }

    var utcDateStr = element.getAttribute('data-utc-time') || element.textContent.trim();
    var date = parseUTCDate(utcDateStr);

    if (!date) {
      console.warn('[HeliosTimezone] Could not parse datetime:', utcDateStr);
      return;
    }

    // Create and insert the display
    var container = document.createElement('span');
    container.className = 'tz-display';
    container.innerHTML = createTimezoneHTML(date);

    // Replace element content
    element.innerHTML = '';
    element.appendChild(container);
    element.classList.add('tz-converted');
  }

  /**
   * Create helper text HTML for datetime input
   * @param {Date} date - Date object to format
   * @returns {string} HTML string for helper text
   */
  function createHelperHTML(date) {
    var utcFormatted = config.utcFormatter.format(date);
    var localFormatted = config.localFormatter.format(date);
    var localTz = getLocalTimezoneName();

    if (isUTCTimezone(date)) {
      return '<small>' + labels.thisTimeIs + ' <strong>' + utcFormatted + ' UTC</strong></small>';
    }

    return '<small>' + labels.thisTimeIs + ' <strong>' + utcFormatted + ' UTC</strong> / ' +
           '<strong>' + localFormatted + ' ' + localTz + '</strong></small>';
  }

  /**
   * Update datetime-local input helper text
   * @param {HTMLInputElement} input - Input element to update
   */
  function updateDateTimeInputHelper(input) {
    if (!input || !input.value) {
      return;
    }

    // Parse the datetime-local value (format: YYYY-MM-DDTHH:MM)
    var date = new Date(input.value + ':00Z'); // Add seconds and Z for UTC

    if (isNaN(date.getTime())) {
      return;
    }

    // Get or create helper element
    var helper = input.nextElementSibling;
    if (!helper || !helper.classList.contains('tz-input-helper')) {
      helper = document.createElement('div');
      helper.className = 'tz-input-helper';
      input.parentNode.insertBefore(helper, input.nextSibling);
    }

    helper.innerHTML = createHelperHTML(date);
  }

  /**
   * Initialize timezone display for all timestamp elements
   */
  function initTimestampElements() {
    var elements = document.querySelectorAll('[data-utc-time]');
    Array.prototype.forEach.call(elements, convertTimestamp);
  }

  /**
   * Initialize datetime input helpers
   */
  function initDateTimeInputs() {
    var datetimeInputs = document.querySelectorAll('input[type="datetime-local"]');

    Array.prototype.forEach.call(datetimeInputs, function(input) {
      // Update helper on input change
      input.addEventListener('change', function() {
        updateDateTimeInputHelper(this);
      });

      // Update helper on page load if there's a value
      if (input.value) {
        updateDateTimeInputHelper(input);
      }
    });
  }

  /**
   * Initialize all timezone display functionality
   */
  function init() {
    initTimestampElements();
    initDateTimeInputs();
  }

  // Auto-initialize on DOM ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  // Public API
  return {
    init: init,
    convertTimestamp: convertTimestamp,
    updateDateTimeInputHelper: updateDateTimeInputHelper,
    parseUTCDate: parseUTCDate,
    getLocalTimezoneName: getLocalTimezoneName,
    createTimezoneHTML: createTimezoneHTML,
    createHelperHTML: createHelperHTML
  };
})();
