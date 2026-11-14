"""
SCIM PATCH (RFC 7644, section 3.5.2), applied to the JSON representation of a
resource; the result is then stored like a PUT.

Tolerates the dialects of common clients:
- operation names in any case ("Replace", "Add": Entra ID);
- booleans as strings ("False": Entra ID; converted when the result is stored);
- no path and a value object whose keys are paths, e.g.
  {"op": "replace", "value": {"active": false, "name.givenName": "Jane"}} (Okta, Entra ID);
- schema-prefixed paths ("urn:ietf:params:scim:schemas:core:2.0:User:name.givenName");
- removing members with a value list instead of a filter
  ({"op": "Remove", "path": "members", "value": [{"value": "<id>"}]}: Entra ID);
- value paths with filters ('members[value eq "<id>"]', 'emails[type eq "work"].value');
  replacing a sub-attribute of an element that doesn't exist yet creates it.
Attributes Helios doesn't store (e.g. phone numbers, the enterprise extension)
are ignored.
"""

import copy

from . import filter as scim_filter
from .errors import ScimError
from .schemas import core_schema, known_attributes, PATCH_SCHEMA


def apply_patch(resource_type, resource, body):
  """RESOURCE (a dict with canonical attribute names) changed by the PATCH request BODY."""
  if not isinstance(body, dict):
    raise ScimError(400, 'invalidSyntax', 'the request body must be a JSON object')
  schemas = body.get('schemas')
  if schemas is not None and PATCH_SCHEMA not in (schemas or []):
    raise ScimError(400, 'invalidSyntax', 'schemas must contain %s' % PATCH_SCHEMA)
  operations = body.get('Operations', body.get('operations'))
  if not isinstance(operations, list) or not operations:
    raise ScimError(400, 'invalidSyntax', 'Operations must be a non-empty list')

  result = copy.deepcopy(resource)
  patcher = _Patcher(resource_type, result)
  for operation in operations:
    if not isinstance(operation, dict):
      raise ScimError(400, 'invalidSyntax', 'each operation must be an object')
    op = str(operation.get('op', '')).lower()
    if op not in ('add', 'remove', 'replace'):
      raise ScimError(400, 'invalidSyntax', 'unknown operation %r' % operation.get('op'))
    path, value = operation.get('path'), operation.get('value')
    if path in (None, ''):
      if op == 'remove':
        raise ScimError(400, 'noTarget', 'remove needs a path')
      if not isinstance(value, dict):
        raise ScimError(400, 'invalidValue', 'an operation without path needs an object as value')
      for key, sub_value in value.items():
        patcher.apply(op, key, sub_value)
    else:
      if not isinstance(path, str):
        raise ScimError(400, 'invalidPath', 'path must be a string')
      if op != 'remove' and 'value' not in operation:
        raise ScimError(400, 'invalidValue', '%s needs a value' % op)
      patcher.apply(op, path, value)
  return result


class _Patcher:
  def __init__(self, resource_type, resource):
    self.resource = resource
    self.attributes = known_attributes(resource_type)
    self.schema = core_schema(resource_type).lower()

  def apply(self, op, path, value):
    schema, attr, flt, sub = scim_filter.parse_path(path)
    if schema and schema.lower() != self.schema:
      # e.g. the enterprise extension: not stored
      return
    definition = self.attributes.get(attr)
    if definition is None:
      return
    name = definition['name']
    sub_name = self._sub_name(definition, sub)
    if sub is not None and sub_name is None:
      return
    if definition['multiValued']:
      self._multi(op, name, flt, sub_name, value)
    elif flt is not None:
      raise ScimError(400, 'invalidPath', '%s is not multi-valued' % name)
    elif sub_name:
      self._sub(op, name, sub_name, value)
    else:
      self._single(op, name, definition, value)

  def _sub_name(self, definition, sub):
    if sub is None:
      return None
    for s in definition.get('subAttributes', []):
      if s['name'].lower() == sub:
        return s['name']
    return None

  def _single(self, op, name, definition, value):
    if op == 'remove':
      self.resource.pop(name, None)
    elif definition['type'] == 'complex' and isinstance(value, dict):
      current = self.resource.get(name) if isinstance(self.resource.get(name), dict) else {}
      merged = dict(current)
      for key, v in value.items():
        sub_name = self._sub_name(definition, key.lower())
        if sub_name:
          merged[sub_name] = v
      self.resource[name] = merged
    else:
      self.resource[name] = value

  def _sub(self, op, name, sub_name, value):
    current = self.resource.get(name)
    if not isinstance(current, dict):
      current = {}
    if op == 'remove':
      current.pop(sub_name, None)
    else:
      current[sub_name] = value
    self.resource[name] = current

  @staticmethod
  def _element(value):
    return value if isinstance(value, dict) else {'value': value}

  @staticmethod
  def _same(a, b):
    return 'value' in a and 'value' in b and str(a['value']) == str(b['value'])

  def _multi(self, op, name, flt, sub_name, value):
    elements = self.resource.get(name)
    if not isinstance(elements, list):
      elements = []
    self.resource[name] = elements

    if flt is None and sub_name is None:
      if op == 'remove':
        if value in (None, [], {}):
          self.resource.pop(name, None)
        else:
          # Entra ID: remove the listed elements (no filter in the path)
          gone = [self._element(v) for v in (value if isinstance(value, list) else [value])]
          self.resource[name] = [e for e in elements if not any(self._same(e, g) for g in gone)]
        return
      new = [self._element(v) for v in (value if isinstance(value, list) else [value]) if v is not None]
      if op == 'replace':
        self.resource[name] = new
      else:
        for element in new:
          existing = [e for e in elements if self._same(e, element)]
          if existing:
            existing[0].update(element)
          else:
            elements.append(element)
      return

    if flt is None:
      # e.g. "emails.value": the primary element, or the first one
      targets = [e for e in elements if isinstance(e, dict) and e.get('primary') in (True, 'true', 'True')] \
        or [e for e in elements if isinstance(e, dict)][:1]
      if op == 'remove':
        for e in elements:
          if isinstance(e, dict):
            e.pop(sub_name, None)
      elif targets:
        targets[0][sub_name] = value
      else:
        elements.append({sub_name: value})
      return

    matching = [e for e in elements if isinstance(e, dict) and scim_filter.matches(flt, e, (name.lower(),))]
    if op == 'remove':
      if sub_name:
        for e in matching:
          e.pop(sub_name, None)
      else:
        self.resource[name] = [e for e in elements if not any(e is m for m in matching)]
      return
    if matching:
      for e in matching:
        if sub_name:
          e[sub_name] = value
        elif isinstance(value, dict):
          e.update(value)
      return
    # nothing matches: create the element the filter describes, e.g.
    # emails[type eq "work"].value -> {"type": "work", "value": ...}
    new = dict(_filter_defaults(flt))
    if sub_name:
      new[sub_name] = value
    elif isinstance(value, dict):
      new.update(value)
    else:
      return
    elements.append(new)


def _filter_defaults(flt):
  equal = scim_filter.simple_equality(flt) or {}
  return {path[-1]: value for path, value in equal.items() if len(path) == 1}
