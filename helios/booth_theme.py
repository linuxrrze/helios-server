"""
The stylesheet with the colors of an election's voting booth.

The booth (heliosbooth/vote.html) is a static page, the same for every election,
and usually served by the web server. Its look comes from CSS custom properties
(--booth-*) in heliosbooth/css/booth-ui.css, with today's colors as defaults.
vote.html loads /helios/elections/<uuid>/booth-theme.css, generated here from
the election's organization:

1. its light colors, as custom properties;
2. the booth's dark colors (heliosbooth/css/booth-dark.css) if the organization's
   dark mode is "auto";
3. its dark colors, as custom properties in the same media query;
4. its web font, as an @font-face under a fixed family name.

Only values validated at start go in (settings.parse_booth_colors: colors as
#rrggbb with sufficient contrast; check_font_url: a font file on this site), and
only into custom properties and one @font-face: never CSS from the configuration.
So an organization can't hide or change the election fingerprint, the audit
option or the steps of casting a ballot, and the fingerprint and trackers keep
their monospace font. Without any colors or font, the stylesheet is exactly
/booth-dark-mode.css.
"""

import os

from django.conf import settings

# configured color -> custom properties it sets
LIGHT_PROPERTIES = {
  'primary': ('--booth-primary', '--booth-outline'),
  'primary_contrast': ('--booth-primary-contrast',),
  'accent': ('--booth-accent',),
  'accent_contrast': ('--booth-accent-contrast', '--booth-accent-muted', '--booth-accent-link'),
  'link': ('--booth-link',),
  'background': ('--booth-background',),
}
FONT_FAMILY = 'booth-organization-font'
# the default font stack of booth-ui.css, after the organization's font
FONT_FALLBACK = 'system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif'
FONT_FORMATS = {'woff2': 'woff2', 'woff': 'woff', 'ttf': 'truetype', 'otf': 'opentype'}


def dark_mode_css(dark_mode):
  """the booth's dark colors for dark mode 'auto', else a comment (also served as /booth-dark-mode.css)"""
  if dark_mode == 'auto':
    with open(os.path.join(settings.ROOT_PATH, 'heliosbooth', 'css', 'booth-dark.css'), encoding='utf-8') as f:
      return f.read()
  return "/* DARK_MODE is off: the voting booth stays light */\n"


def _mix(color, other, weight):
  """'#rrggbb': COLOR with WEIGHT (0..1) of OTHER mixed in"""
  channels = [round(int(color[i:i + 2], 16) * (1 - weight) + int(other[i:i + 2], 16) * weight) for i in (1, 3, 5)]
  return '#' + ''.join('%02x' % c for c in channels)


def _properties(colors, dark):
  """custom properties for validated colors ('#rrggbb'), with the shades derived from them"""
  properties = []
  for name in settings.BOOTH_COLOR_NAMES:
    if name in colors:
      properties += [(prop, colors[name]) for prop in LIGHT_PROPERTIES[name]]
  if 'accent' in colors and 'accent_contrast' not in colors:
    # the footer text (with the election fingerprint) and the exit link in the text color
    # checked against the accent, not in the built-in muted and link colors
    contrast = settings.BOOTH_BUILT_IN_COLORS['dark' if dark else 'light']['accent_contrast']
    properties += [('--booth-accent-muted', contrast), ('--booth-accent-link', contrast)]
  if 'primary' in colors:
    primary = colors['primary']
    surface = settings.BOOTH_BUILT_IN_COLORS['dark' if dark else 'light']['surface']
    properties.append(('--booth-primary-hover', _mix(primary, '#000000', 0.25)))
    properties.append(('--booth-selected', _mix(primary, surface, 0.75 if dark else 0.88)))
    if not dark:
      properties.append(('--booth-button-hover', _mix(primary, surface, 0.93)))
  return properties


def _root(properties, indent=''):
  lines = ['%s:root {' % indent]
  lines += ['%s  %s: %s;' % (indent, prop, value) for prop, value in properties]
  lines.append('%s}' % indent)
  return '\n'.join(lines) + '\n'


def css_for(organization):
  """the booth stylesheet of an election of ORGANIZATION (helios.organizations.Organization)"""
  colors = organization.booth_colors or {}
  dark_mode = organization.dark_mode
  parts = []
  light = _properties({k: v for k, v in colors.items() if k != 'dark'}, dark=False)
  if light:
    parts.append('/* colors of the organization (generated from validated values) */\n' + _root(light))
  parts.append(dark_mode_css(dark_mode))
  dark = _properties(colors.get('dark', {}), dark=True)
  if dark and dark_mode == 'auto':
    parts.append('@media (prefers-color-scheme: dark) {\n' + _root(dark, '  ') + '}\n')
  font_url = organization.font_url
  if font_url:
    extension = font_url.rsplit('.', 1)[1].lower()
    parts.append('@font-face {\n  font-family: "%s";\n  src: url("%s") format("%s");\n  font-display: swap;\n}\n'
                 % (FONT_FAMILY, font_url, FONT_FORMATS[extension])
                 + _root([('--booth-font', '"%s", %s' % (FONT_FAMILY, FONT_FALLBACK))]))
  return ''.join(parts)
