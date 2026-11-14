"""
SCIM filter expressions (RFC 7644, section 3.4.2.2) and attribute paths
(section 3.5.2), parsed into a small tree and evaluated on the JSON
representation of a resource (or on one element of a multi-valued attribute).

  filter := term ("or" term)*
  term   := factor ("and" factor)*
  factor := "not"? ( "(" filter ")" | path "pr" | path op value | attr "[" filter "]" )
  path   := [schema URN ":"] attr ["." subattr]

Nodes: ('or', a, b), ('and', a, b), ('not', a), ('pr', path), ('cmp', path, op, value),
('vp', attr, filter); a path is a tuple of lowercased attribute names.
"""

import json
import re

from .errors import ScimError

OPERATORS = ('eq', 'ne', 'co', 'sw', 'ew', 'gt', 'ge', 'lt', 'le')

# attributes compared case-sensitively (caseExact in RFC 7643); all others ignore case
CASE_EXACT = {('id',), ('externalid',), ('members', 'value')}

_TOKEN = re.compile(r'''
    \s*(?:
      (?P<lpar>\() | (?P<rpar>\)) | (?P<lbr>\[) | (?P<rbr>\]) |
      (?P<string>"(?:[^"\\]|\\.)*") |
      (?P<word>[A-Za-z0-9_$.:\-+]+)
    )''', re.VERBOSE)


def _tokenize(text):
  tokens, pos = [], 0
  text = text.strip()
  while pos < len(text):
    m = _TOKEN.match(text, pos)
    if not m or m.end() == pos:
      raise ScimError(400, 'invalidFilter', 'cannot parse the filter near %r' % text[pos:pos + 20])
    pos = m.end()
    kind = m.lastgroup
    tokens.append((kind, m.group(kind)))
  return tokens


def strip_schema(attr_path):
  """'urn:ietf:params:scim:schemas:core:2.0:User:name.givenName' -> ('urn:...:User', 'name.givenName')"""
  if attr_path.lower().startswith('urn:') and ':' in attr_path:
    schema, _sep, rest = attr_path.rpartition(':')
    return schema, rest
  return '', attr_path


def split_path(attr_path):
  _schema, rest = strip_schema(attr_path)
  parts = tuple(p.lower() for p in rest.split('.') if p)
  if not parts or len(parts) > 2:
    raise ScimError(400, 'invalidFilter', 'invalid attribute path %r' % attr_path)
  return parts


class _Parser:
  def __init__(self, text):
    self.tokens = _tokenize(text)
    self.pos = 0

  def peek(self, offset=0):
    i = self.pos + offset
    return self.tokens[i] if i < len(self.tokens) else (None, None)

  def take(self, kind=None):
    tok = self.peek()
    if tok[0] is None or (kind and tok[0] != kind):
      raise ScimError(400, 'invalidFilter', 'unexpected end of filter' if tok[0] is None else 'unexpected %r' % tok[1])
    self.pos += 1
    return tok

  def keyword(self, word):
    kind, value = self.peek()
    return kind == 'word' and value.lower() == word

  def parse(self):
    node = self.parse_or()
    if self.peek()[0] is not None:
      raise ScimError(400, 'invalidFilter', 'unexpected %r' % self.peek()[1])
    return node

  def parse_or(self):
    node = self.parse_and()
    while self.keyword('or'):
      self.take()
      node = ('or', node, self.parse_and())
    return node

  def parse_and(self):
    node = self.parse_factor()
    while self.keyword('and'):
      self.take()
      node = ('and', node, self.parse_factor())
    return node

  def parse_factor(self):
    if self.keyword('not') and self.peek(1)[0] == 'lpar':
      self.take()
      return ('not', self.parse_factor())
    if self.peek()[0] == 'lpar':
      self.take()
      node = self.parse_or()
      self.take('rpar')
      return node
    _kind, attr = self.take('word')
    if self.peek()[0] == 'lbr':
      self.take()
      inner = self.parse_or()
      self.take('rbr')
      return ('vp', split_path(attr)[0], inner)
    path = split_path(attr)
    _kind, op = self.take('word')
    op = op.lower()
    if op == 'pr':
      return ('pr', path)
    if op not in OPERATORS:
      raise ScimError(400, 'invalidFilter', 'unknown operator %r' % op)
    return ('cmp', path, op, self._value())

  def _value(self):
    kind, raw = self.take()
    if kind == 'string':
      try:
        return json.loads(raw)
      except ValueError:
        raise ScimError(400, 'invalidFilter', 'invalid string %s' % raw)
    if kind == 'word':
      low = raw.lower()
      if low in ('true', 'false'):
        return low == 'true'
      if low == 'null':
        return None
      try:
        return float(raw) if '.' in raw else int(raw)
      except ValueError:
        pass
    raise ScimError(400, 'invalidFilter', 'invalid value %r' % raw)


def parse_filter(text):
  if not text or not text.strip():
    raise ScimError(400, 'invalidFilter', 'empty filter')
  return _Parser(text).parse()


def parse_path(text):
  """
  A PATCH path: (schema, attr, filter or None, subattr or None), names
  lowercased, e.g. 'emails[type eq "work"].value' -> ('', 'emails', <filter>, 'value').
  """
  text = (text or '').strip()
  head, bracket, rest = text.partition('[')
  schema, head = strip_schema(head)
  if not bracket:
    parts = [p for p in head.split('.') if p]
    if not parts or len(parts) > 2:
      raise ScimError(400, 'invalidPath', 'invalid path %r' % text)
    return schema, parts[0].lower(), None, (parts[1].lower() if len(parts) > 1 else None)
  inner, closing, after = rest.rpartition(']')
  if not closing or not head or '.' in head:
    raise ScimError(400, 'invalidPath', 'invalid path %r' % text)
  try:
    flt = parse_filter(inner)
  except ScimError as e:
    raise ScimError(400, 'invalidPath', e.detail)
  sub = None
  if after:
    if not after.startswith('.') or len(after) < 2:
      raise ScimError(400, 'invalidPath', 'invalid path %r' % text)
    sub = after[1:].lower()
  return schema, head.lower(), flt, sub


##
## evaluation
##

def _get(obj, name):
  """Case-insensitive attribute lookup in a dict."""
  if not isinstance(obj, dict):
    return None
  for key, value in obj.items():
    if key.lower() == name:
      return value
  return None


def _values(obj, path):
  """All values at PATH (multi-valued attributes are flattened)."""
  current = [obj]
  for name in path:
    nxt = []
    for item in current:
      value = _get(item, name)
      if isinstance(value, list):
        nxt.extend(value)
      elif value is not None:
        nxt.append(value)
    current = nxt
  # a multi-valued attribute compared without sub-attribute means its "value"
  return [v.get('value') if isinstance(v, dict) and 'value' in v else v for v in current]


def _compare(actual, op, expected, case_exact):
  if isinstance(actual, bool) or isinstance(expected, bool):
    if op in ('eq', 'ne'):
      result = isinstance(actual, bool) and actual == expected
      return result if op == 'eq' else not result
    return False
  if expected is None:
    return (actual is None) == (op == 'eq')
  if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
    a, b = actual, expected
  else:
    a, b = str(actual), str(expected)
    if not case_exact:
      a, b = a.lower(), b.lower()
  if op == 'eq':
    return a == b
  if op == 'ne':
    return a != b
  if op == 'co':
    return str(b) in str(a)
  if op == 'sw':
    return str(a).startswith(str(b))
  if op == 'ew':
    return str(a).endswith(str(b))
  try:
    return {'gt': a > b, 'ge': a >= b, 'lt': a < b, 'le': a <= b}[op]
  except TypeError:
    return False


def matches(node, obj, prefix=()):
  kind = node[0]
  if kind == 'and':
    return matches(node[1], obj, prefix) and matches(node[2], obj, prefix)
  if kind == 'or':
    return matches(node[1], obj, prefix) or matches(node[2], obj, prefix)
  if kind == 'not':
    return not matches(node[1], obj, prefix)
  if kind == 'pr':
    return any(v not in (None, '', [], {}) for v in _values(obj, node[1]))
  if kind == 'vp':
    elements = _get(obj, node[1])
    if not isinstance(elements, list):
      elements = [elements] if isinstance(elements, dict) else []
    return any(matches(node[2], el, prefix + (node[1],)) for el in elements)
  _kind, path, op, expected = node
  case_exact = (prefix + path) in CASE_EXACT
  values = _values(obj, path)
  if op == 'ne':
    return all(_compare(v, 'ne', expected, case_exact) for v in values) if values else expected is not None
  return any(_compare(v, op, expected, case_exact) for v in values)


def simple_equality(node):
  """{'path': value} for the conditions a filter requires (combined with
  'and'), to narrow a database query before matching in Python; None if the
  filter can't be narrowed that way."""
  if node[0] == 'cmp' and node[2] == 'eq' and isinstance(node[3], str):
    return {node[1]: node[3]}
  if node[0] == 'and':
    left, right = simple_equality(node[1]), simple_equality(node[2])
    if left is None:
      return right
    if right is None:
      return left
    return {**left, **right}
  return None
