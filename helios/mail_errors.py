"""
What an error of the mail server means for an email (see helios.mail):

  PERMANENT  this email will not go through (unknown mailbox, rejected): failed
  TEMPORARY  this email, later (greylisting, mailbox full, throttling): deferred
  SYSTEM     the mail server cannot be reached: deferred, and sending paused
  CONFIG     the mail server refuses our login or sender address: deferred for
             longer, sending paused and the site administrators told

SMTP replies are judged by their RFC 3463 enhanced status code ("5.1.1",
"4.2.2") where the server gives one, else by the three-digit reply code: 5xx is
permanent, 4xx temporary. HTTP mail services (Mailgun, Amazon SES): 429, 5xx and
throttling are temporary, 401/403 (API key) a configuration error, other 4xx
permanent. Anything else is taken as temporary: tried again rather than lost.
"""
import re
import smtplib
import ssl
from typing import NamedTuple

PERMANENT = 'permanent'
TEMPORARY = 'temporary'
SYSTEM = 'system'
CONFIG = 'config'

# at the start of the reply text, e.g. b'5.1.1 <erika@example.org>: Recipient address rejected'
ENHANCED_CODE = re.compile(r'\s*([245])\.(\d{1,3})\.(\d{1,3})(?![\d.])')

# error codes of Amazon SES (botocore ClientError) that mean "slow down"
THROTTLING_CODES = ('Throttling', 'ThrottlingException', 'TooManyRequestsException')


class MailError(NamedTuple):
  category: str   # PERMANENT, TEMPORARY, SYSTEM or CONFIG
  code: str       # for OutgoingEmail.last_error_code: '550 5.1.1', '451', 'SMTPServerDisconnected'
  text: str


def _text(message):
  if isinstance(message, bytes):
    return message.decode('utf-8', 'replace')
  return str(message or '')


def enhanced_code(message):
  """the RFC 3463 enhanced status code at the start of an SMTP reply text ('5.1.1'), or None"""
  match = ENHANCED_CODE.match(_text(message))
  return '.'.join(match.groups()) if match else None


def classify_reply(code, message):
  """PERMANENT, TEMPORARY or SYSTEM for an SMTP reply (CODE 550, MESSAGE b'5.1.1 ...')"""
  enhanced = enhanced_code(message)
  if enhanced:
    temporary = not enhanced.startswith('5')
  else:
    temporary = not (500 <= (code or 0) <= 599)
  if not temporary:
    return PERMANENT
  # 421: the server closes the connection (overloaded, shutting down), for all emails
  return SYSTEM if code == 421 else TEMPORARY


def _reply_code(code, message):
  enhanced = enhanced_code(message)
  return ('%s %s' % (code, enhanced)) if enhanced else str(code)


def _describe(exception):
  return ('%s: %s' % (type(exception).__name__, exception))[:2000]


def classify_http(status, error_code=''):
  """for HTTP mail services (Mailgun, Amazon SES)"""
  if error_code in THROTTLING_CODES or status == 429 or (status or 0) >= 500:
    return TEMPORARY
  if status in (401, 403):
    return CONFIG
  if 400 <= (status or 0) <= 499:
    return PERMANENT
  return TEMPORARY


def classify(exception):
  """the MailError for an exception raised while handing an email to the mail server"""
  text = _describe(exception)

  # the mail server refuses us, not the recipient: no email will go through
  if isinstance(exception, (smtplib.SMTPAuthenticationError, smtplib.SMTPSenderRefused)):
    return MailError(CONFIG, _reply_code(exception.smtp_code, exception.smtp_error), text)
  if isinstance(exception, smtplib.SMTPNotSupportedError):   # e.g. no STARTTLS, no AUTH
    return MailError(CONFIG, type(exception).__name__, text)

  if isinstance(exception, smtplib.SMTPRecipientsRefused):
    # one recipient per email here
    if exception.recipients:
      code, message = next(iter(exception.recipients.values()))
      return MailError(classify_reply(code, message), _reply_code(code, message), text)
    return MailError(PERMANENT, type(exception).__name__, text)

  # cannot talk to the mail server at all
  if isinstance(exception, (smtplib.SMTPConnectError, smtplib.SMTPHeloError)):
    return MailError(SYSTEM, _reply_code(exception.smtp_code, exception.smtp_error), text)
  if isinstance(exception, smtplib.SMTPServerDisconnected):
    return MailError(SYSTEM, type(exception).__name__, text)

  # any other reply, e.g. SMTPDataError (the message was refused after DATA)
  if isinstance(exception, smtplib.SMTPResponseException):
    return MailError(classify_reply(exception.smtp_code, exception.smtp_error),
                     _reply_code(exception.smtp_code, exception.smtp_error), text)

  if isinstance(exception, ssl.SSLCertVerificationError):   # wrong EMAIL_HOST or certificate
    return MailError(CONFIG, type(exception).__name__, text)
  # timeouts, refused connections, unknown host names, TLS errors
  if isinstance(exception, OSError):
    return MailError(SYSTEM, type(exception).__name__, text)

  # HTTP mail services: anymail errors have status_code, botocore's ClientError a response
  status = getattr(exception, 'status_code', None)
  error_code = ''
  response = getattr(exception, 'response', None)
  if isinstance(response, dict):
    error_code = (response.get('Error') or {}).get('Code') or ''
    status = status or (response.get('ResponseMetadata') or {}).get('HTTPStatusCode')
  if isinstance(status, int) or error_code:
    code = ' '.join(str(part) for part in (status, error_code) if part)
    return MailError(classify_http(status, error_code), code[:32], text)
  if type(exception).__name__ == 'AnymailRecipientsRefused':
    return MailError(PERMANENT, type(exception).__name__, text)

  return MailError(TEMPORARY, type(exception).__name__[:32], text)
