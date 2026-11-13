"""
Election times in the installation's local time zone.

Helios stores and compares all election times (voting start and end, cast
times, ...) as naive datetimes in UTC, and that does not change. People,
however, enter and read them in local time: the forms take a date and time
in ELECTION_TIME_ZONE and convert it to UTC, and the pages show stored times
in that zone, always with the zone's name.
"""

import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.utils import formats

UTC = datetime.timezone.utc


class NonexistentLocalTime(ValueError):
  """a wall-clock time that is skipped when the clocks go forward"""


def election_time_zone():
  """The zone in which dates and times are entered and shown (a tzinfo)."""
  name = getattr(settings, 'ELECTION_TIME_ZONE', None) or 'UTC'
  try:
    return ZoneInfo(name)
  except (ZoneInfoNotFoundError, ValueError):
    return ZoneInfo('UTC')


def zone_name(tz=None):
  tz = tz or election_time_zone()
  return getattr(tz, 'key', None) or str(tz)


def local_to_utc(value, tz=None):
  """
  Naive local time in tz -> naive UTC.

  A time that occurs twice when the clocks go back is taken as the first
  occurrence (still on summer time). A time that does not exist because the
  clocks go forward raises NonexistentLocalTime.
  """
  tz = tz or election_time_zone()
  value = value.replace(tzinfo=None, fold=0)
  utc = value.replace(tzinfo=tz).astimezone(UTC)
  if utc.astimezone(tz).replace(tzinfo=None) != value:
    raise NonexistentLocalTime(value)
  return utc.replace(tzinfo=None)


def utc_to_local(value, tz=None):
  """Naive UTC -> aware datetime in tz."""
  tz = tz or election_time_zone()
  if value.tzinfo is None:
    value = value.replace(tzinfo=UTC)
  return value.astimezone(tz)


def utc_offset_label(local_dt):
  """'UTC+01:00' for an aware datetime"""
  offset = local_dt.utcoffset() or datetime.timedelta(0)
  minutes = int(offset.total_seconds() // 60)
  sign = '+' if minutes >= 0 else '-'
  minutes = abs(minutes)
  return 'UTC%s%02d:%02d' % (sign, minutes // 60, minutes % 60)


def zone_abbreviation(local_dt):
  """'CET', 'EST', ... where the zone has a name for it, else 'UTC+03:00'"""
  name = local_dt.tzname() or ''
  if name.isalpha():
    return name
  return utc_offset_label(local_dt)


def current_offset_label(tz=None):
  """the zone's offset from UTC right now, e.g. 'UTC+01:00'"""
  return utc_offset_label(datetime.datetime.now(UTC).astimezone(tz or election_time_zone()))


def format_local(value, tz=None):
  """
  A stored (naive UTC) datetime as local text in the active language,
  e.g. '20 November 2026, 10:00 CET'.
  """
  local = utc_to_local(value, tz)
  naive = local.replace(tzinfo=None)
  return '%s, %s %s' % (formats.date_format(naive, 'DATE_FORMAT'), naive.strftime('%H:%M'),
                         zone_abbreviation(local))
