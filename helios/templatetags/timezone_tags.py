"""
Template tags for showing election times (stored in UTC) in local time
"""

import datetime

from django import template
from django.utils import formats
from django.utils.html import format_html

from helios import timezones

register = template.Library()


@register.filter(name='utc_time')
def utc_time(value):
  """
  A stored (naive UTC) datetime in the election time zone, with the zone's
  abbreviation, e.g. "20 November 2026, 10:00 CET". The UTC time is in the
  title and the datetime attribute; timezone-display.js adds the visitor's
  own local time if their browser is in another zone.

  Usage in templates:
    {{ election.voting_starts_at|utc_time }}
  """
  if value is None:
    return ''

  # datetime.datetime is a subclass of datetime.date, so check datetime first
  if isinstance(value, datetime.datetime):
    utc = value.replace(tzinfo=None) if value.tzinfo is None else value.astimezone(timezones.UTC).replace(tzinfo=None)
    iso = utc.strftime('%Y-%m-%dT%H:%MZ')
    return format_html('<time class="tz-timestamp" datetime="{}" data-utc-time="{}" data-time-zone="{}" title="{}">{}</time>',
                       iso, iso, timezones.zone_name(), utc.strftime('%Y-%m-%d %H:%M UTC'), timezones.format_local(utc))
  if isinstance(value, datetime.date):
    return format_html('<time datetime="{}">{}</time>', value.isoformat(), formats.date_format(value, 'DATE_FORMAT'))
  return format_html('{}', str(value))
