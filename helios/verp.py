"""
VERP (variable envelope return path): each email goes out with its own
envelope sender, so that a bounce tells which email it is about.

  bounce+<id>-<mac>@<HELIOS_BOUNCE_DOMAIN>     e.g. bounce+4711-k3m9xq2r7w5tz4ab@bounces.example.org

<id> is the id of the OutgoingEmail, <mac> the first 80 bits of
HMAC-SHA256(HELIOS_BOUNCE_SECRET, id) in base32. Without the MAC anyone could
send forged bounces for made-up ids and get voters' addresses suppressed. Only
lower-case letters and digits (and '+', '-'), as some mail servers change the
case of the local part; the token is compared case-insensitively.

The header From stays DEFAULT_FROM_EMAIL (SPF/DKIM/DMARC of the sender domain);
only the SMTP envelope sender (MAIL FROM, later the Return-Path) changes. On
by setting HELIOS_BOUNCE_DOMAIN and HELIOS_BOUNCE_SECRET, and only with the
SMTP backend (helios.mail_backend.SMTPBackend): bounces are read from the
mailbox of the bounce domain by `manage.py process_bounces` (helios.bounces).
"""
import base64
import hashlib
import hmac
import re

from django.conf import settings
from django.utils.module_loading import import_string

LOCAL_PART = 'bounce'
MAC_LENGTH = 16   # base32 characters: 80 bits

# a token on its own ("4711-k3m9xq2r7w5tz4ab", also the DSN's ENVID) and in an address
TOKEN = re.compile(r'(?<![0-9a-z])(\d{1,18})-([a-z2-7]{%d})(?![0-9a-z])' % MAC_LENGTH)
ADDRESS = re.compile(r'%s\+(\d{1,18})-([a-z2-7]{%d})@([a-z0-9.-]+)' % (LOCAL_PART, MAC_LENGTH))


def _backend_class():
  path = settings.EMAIL_BACKEND
  if path == 'helios.mail_backend.LoggingEmailBackend':
    path = settings.HELIOS_EMAIL_BACKEND
  try:
    return import_string(path)
  except ImportError:
    return None


def enabled():
  """True if emails get a VERP envelope sender: bounce domain and secret set, sent over SMTP"""
  if not (settings.HELIOS_BOUNCE_DOMAIN and settings.HELIOS_BOUNCE_SECRET):
    return False
  from .mail_backend import SMTPBackend
  backend = _backend_class()
  return isinstance(backend, type) and issubclass(backend, SMTPBackend)


def _mac(email_id):
  digest = hmac.new(settings.HELIOS_BOUNCE_SECRET.encode('utf-8'), b'helios-bounce:%d' % int(email_id),
                    hashlib.sha256).digest()
  return base64.b32encode(digest).decode('ascii').lower()[:MAC_LENGTH]


def token(email_id):
  """the token of the OutgoingEmail EMAIL_ID ('4711-k3m9xq2r7w5tz4ab'), kept as its bounce_token"""
  return '%d-%s' % (int(email_id), _mac(email_id))


def address(bounce_token):
  """the envelope sender for BOUNCE_TOKEN"""
  return '%s+%s@%s' % (LOCAL_PART, bounce_token, settings.HELIOS_BOUNCE_DOMAIN.lower())


def verify(email_id, mac):
  """True if MAC belongs to EMAIL_ID (constant-time comparison)"""
  if not settings.HELIOS_BOUNCE_SECRET:
    return False
  return hmac.compare_digest(_mac(email_id), mac.lower())


def email_id_from_token(text):
  """the email id of a valid token in TEXT ('4711-k3m9...', any case), or None"""
  match = TOKEN.search((text or '').lower())
  if match and verify(match.group(1), match.group(2)):
    return int(match.group(1))
  return None


def email_ids_in(text):
  """the email ids of all valid VERP addresses of our bounce domain in TEXT, in order"""
  domain = (settings.HELIOS_BOUNCE_DOMAIN or '').lower().rstrip('.')
  found = []
  for match in ADDRESS.finditer((text or '').lower()):
    email_id, mac, address_domain = match.groups()
    if domain and address_domain.rstrip('.') != domain:
      continue
    if verify(email_id, mac) and int(email_id) not in found:
      found.append(int(email_id))
  return found
