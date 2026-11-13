"""
Ballot trackers that people can read and check.

{{ vote_hash|tracker_groups }}
    the full tracker in groups of four characters. The groups are separate
    elements without spaces between them, so copying the text (or the Copy
    button, which reads textContent) still gives the exact tracker.

{% tracker_qr vote_hash %}
    an inline SVG QR code (no JavaScript, no external service) with the link
    to this ballot on the server, /helios/vh/<full tracker>.
"""

from django import template
from django.conf import settings
from django.urls import reverse
from django.utils.html import format_html, format_html_join
from django.utils.translation import gettext as _

from helios import url_names
from helios.vendor.qrcodegen import QrCode

register = template.Library()

GROUP = 4


@register.filter
def tracker_groups(value):
  if not value:
    return ''
  value = value.decode() if isinstance(value, bytes) else str(value)
  groups = [value[i:i + GROUP] for i in range(0, len(value), GROUP)]
  return format_html_join('', '<span class="tracker-group">{}</span>', ((g,) for g in groups))


def tracker_url(vote_hash):
  return settings.URL_HOST + reverse(url_names.CAST_VOTE_FULLHASH_SHORTCUT, args=[vote_hash])


def qr_svg(text, label, border=4):
  """an SVG QR code of text: black modules on a white quiet zone, also in dark mode"""
  qr = QrCode.encode_text(text, QrCode.Ecc.MEDIUM)
  size = qr.get_size() + 2 * border
  path = ''.join('M%d,%dh1v1h-1z' % (x + border, y + border)
                 for y in range(qr.get_size()) for x in range(qr.get_size()) if qr.get_module(x, y))
  return format_html(
    '<svg class="qr-code" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {0} {0}" role="img" aria-label="{1}" '
    'shape-rendering="crispEdges"><rect width="{0}" height="{0}" fill="#fff"/><path d="{2}" fill="#000"/></svg>',
    size, label, path)


@register.simple_tag
def tracker_qr(vote_hash):
  if not vote_hash:
    return ''
  return qr_svg(tracker_url(vote_hash), _('QR code with the link to your ballot tracker'))


@register.simple_tag
def tracker_link(vote_hash):
  return tracker_url(vote_hash) if vote_hash else ''
