import datetime

from django.core.exceptions import ValidationError
from django.forms import fields
from django.utils.translation import gettext_lazy as _

from helios import timezones

from .widgets import SplitSelectDateTimeWidget, DateTimeLocalWidget


class SplitDateTimeField(fields.MultiValueField):
    widget = SplitSelectDateTimeWidget

    def __init__(self, *args, **kwargs):
        """
        Have to pass a list of field types to the constructor, else we
        won't get any data to our compress method.
        """
        all_fields = (fields.DateField(), fields.TimeField())
        super(SplitDateTimeField, self).__init__(all_fields, *args, **kwargs)

    def compress(self, data_list):
        """
        Takes the values from the MultiWidget and passes them as a
        list to this function. This function needs to compress the
        list into a single object to save.
        """
        if data_list:
            if not (data_list[0] and data_list[1]):
                return None
            return datetime.datetime.combine(*data_list)
        return None


class DateTimeLocalField(fields.DateTimeField):
    """
    A field for HTML5 datetime-local input widget.
    Handles datetime input in the format: YYYY-MM-DDTHH:MM
    """
    widget = DateTimeLocalWidget
    input_formats = ['%Y-%m-%dT%H:%M', '%Y-%m-%dT%H:%M:%S']

    def __init__(self, *args, **kwargs):
        if 'input_formats' not in kwargs or kwargs.get('input_formats') is None:
            kwargs['input_formats'] = self.input_formats
        super(DateTimeLocalField, self).__init__(*args, **kwargs)



class LocalDateTimeField(DateTimeLocalField):
  """
  A date and time entered in the election time zone (helios.timezones), cleaned
  to a naive UTC datetime as Helios stores it. Stored values (datetimes) are
  shown in the election time zone; submitted text is shown as entered.
  """

  default_error_messages = {
    'nonexistent': _('This time does not exist in the time zone %(zone)s, because the clocks are put forward then. Please choose another time.'),
  }

  def prepare_value(self, value):
    if isinstance(value, datetime.datetime):
      return timezones.utc_to_local(value).replace(tzinfo=None)
    return value

  def to_python(self, value):
    # a datetime is a stored value, already in UTC
    if isinstance(value, datetime.datetime):
      return value
    local = super().to_python(value)
    if local is None:
      return None
    try:
      return timezones.local_to_utc(local)
    except timezones.NonexistentLocalTime:
      raise ValidationError(self.error_messages['nonexistent'], code='nonexistent',
                            params={'zone': timezones.zone_name()})
