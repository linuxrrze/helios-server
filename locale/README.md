# Translations

The interface is written in English (the msgids) and translated to German here:

- `de/LC_MESSAGES/django.po`: Django templates (pages and emails) and Python code
- `de/LC_MESSAGES/djangojs.po`: the voting booth (`heliosbooth/`), the verifiers
  (`heliosverifier/`, `heliosbooth/single-ballot-verify.html`) and the question editor
  (`helios/media/static_templates/question.html`); served by `/jsi18n/`

The compiled `.mo` files are committed so that deployments need no gettext tools.
Recompile and commit them whenever a `.po` file changes.

## Updating after changing texts

Needs GNU gettext (`apt install gettext`).

```sh
# pages, emails, Python
uv run python manage.py makemessages -l de -d django \
  -i '.venv/*' -i 'heliosbooth/*' -i 'heliosverifier/*' -i '*/media/*' -i '*/tests*.py'

# booth, verifiers, question editor (jTemplates and inline JS call gettext("..."))
uv run python manage.py makemessages -l de -d djangojs -e js,html \
  -i '.venv/*' -i 'helios/templates/*' -i 'helios_auth/*' -i 'server_ui/*' -i 'templates/*' \
  -i '*compressed*' -i '*.min.js' -i '*/js/*' -i 'helios/media/helios/*'

# translate the new/fuzzy entries in the .po files, then
uv run python manage.py compilemessages -l de
```

## Conventions

- Templates: `{% translate %}` / `{% blocktranslate trimmed %}`, one msgid per sentence;
  no URLs or user data in msgids (pass them as variables).
- jTemplates (booth, question editor): `{gettext("...")}`; the expression must not contain
  `{` or `}` (the booth has `BOOTH.fmt()` for placeholders).
- German: formal "Sie", gender-neutral wording or gender star, German quotes „…“ (a `"` in a
  translation would break HTML attributes and JS strings).
- Glossary: election = Wahl, referendum = Abstimmung, ballot = Stimmzettel, voter =
  wahlberechtigte Person, election administrator = Wahlleitung, trustee = Treuhänder*in,
  voting booth = Wahlkabine, ballot tracker = Stimmzettel-Tracker, tally = Auszählung.
