"""SCIM error responses (RFC 7644, section 3.12)."""

import json

from django.http import HttpResponse

CONTENT_TYPE = 'application/scim+json'
ERROR_SCHEMA = 'urn:ietf:params:scim:api:messages:2.0:Error'


class ScimError(Exception):
  def __init__(self, status, scim_type=None, detail=''):
    super().__init__(detail)
    self.status, self.scim_type, self.detail = status, scim_type, detail

  def response(self):
    return error_response(self.status, self.scim_type, self.detail)


def scim_response(data, status=200, headers=None):
  response = HttpResponse(json.dumps(data), status=status, content_type=CONTENT_TYPE)
  for key, value in (headers or {}).items():
    response[key] = value
  return response


def error_response(status, scim_type=None, detail=''):
  body = {'schemas': [ERROR_SCHEMA], 'status': str(status)}
  if scim_type:
    body['scimType'] = scim_type
  if detail:
    body['detail'] = detail
  return scim_response(body, status=status)
