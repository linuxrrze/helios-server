# User experience review

This review covers every page and dialog of the three roles (election
administrator, voter, trustee) and the general pages. The aim was guidance
that is clearer and more current, without changing the cryptographic flow,
server-side validation, URLs or form field names.

**Method.** Each flow was walked through in a local instance with a headless
browser (English and German), from creating an election to releasing the
result: start page, election form, questions, voter upload, trustees,
freeze, email, booth, casting, tally, decryption and release. Templates,
stylesheets and the booth's JavaScript were read alongside. Reference points:
the GOV.UK Design System patterns ("Question pages", "Task list",
"Confirmation pages", "Error summary", "Check answers"), WCAG 2.2 level AA
(in particular 1.3.1, 2.4.3, 2.4.6/2.4.7, 2.4.11 Focus not obscured,
2.5.8 Target size, 3.3.1/3.3.3 errors, 3.3.8 Accessible authentication) and
the usual e-voting usability advice: one primary action per step, say what
is irreversible before it happens, and tell voters clearly whether their
ballot has been cast.

Priorities: **P1** blocks or misleads users, **P2** costs time or causes
mistakes, **P3** polish.

## Overall findings

| # | Finding | Where | Prio | Status |
|---|---------|-------|------|--------|
| G1 | The layout introduced with configurable branding does not load Foundation or `main.css`, but nearly all application pages still rely on them: unstyled buttons, forms and tables; the inline POST forms (archive, delete voter, remove trustee) and the announcement banner lost their styles. | `server_ui/templates/base.html`, `server_ui/media/layout.css`, `server_ui/media/main.css` | P1 | done |
| G2 | The default logo (1205 × 474 px) is shown at full size on every page and pushes the content below the fold. | `base.html` | P1 | done |
| G3 | Anonymous visitors get the narrow "login" splash layout on every page, including election pages, the cast page and results: ballot trackers and result tables are squeezed into a 450 px box. | `base.html` | P1 | done |
| G4 | No consistent button hierarchy: the old Foundation `.button` (white on #2ba6cb, contrast 2.9:1) next to `.standard-btn`, unstyled `<button>`s, and a selector typo (`utton`) that styled every button on hover only. Secondary actions (copy, archive) looked like primary ones. | `layout.css` | P2 | done |
| G5 | No visible keyboard focus, no skip link, several pages without `h1`, headings in arbitrary order (h3 as page title). | all templates | P2 | done |
| G6 | 404/500 pages extend an old bare XHTML base: no styles, no way back. | `templates/404.html`, `500.html` | P2 | done |
| G7 | `alert()`/`confirm()` popups for information and validation (tally started, min answers, trustee warning). | various | P3 | mostly done |

## Administrator

| # | Finding | Where | Prio | Status |
|---|---------|-------|------|--------|
| A1 | The election page gives no overview of where the election stands. "Next Step" is a line of text; missing setup items are not links; rarely used actions (copy, archive) are the most prominent buttons. | `helios/templates/election_view.html` | P1 | done: status badge, "Running this election" panel with the next step and one primary button, seven-step progress list with what each step means, missing items linked, rare actions under "More actions" |
| A2 | Freeze page: "do NOT freeze yet" is a second `<button>` inside the freeze form that navigates away by JavaScript. Without JavaScript (or if the script fails) it **submits the form and freezes the election**. Same for "never mind" on the compute-tally page. | `election_freeze.html`, `election_compute_tally.html` | P1 | done: plain links |
| A3 | Irreversible steps (freeze, compute tally, release) don't say clearly what can't be undone and what happens next; the tally page shows `alert("ok, tally has begun")`. | same, `release_result.html` | P2 | done: shared confirmation pattern (warning box, consequences, one primary button, cancel link) |
| A4 | The note "You must freeze the ballot before you can contact voters" on the freeze page checks a template variable that never exists, so it never shows. | `election_freeze.html` | P3 | done |
| A5 | Question editor: its whole stylesheet is inside the jTemplate, where jTemplates reads CSS braces as expressions, so none of it applies. "+ Add link" is a `<span onclick>` (no keyboard access), selects and answer fields have no labels, the add form and an open edit form share element ids (the link toggle can open the wrong form's field), "absolute"/"relative" are unexplained. | `helios/templates/election_questions.html`, `helios/media/static_templates/question.html` | P1 | done |
| A6 | Forms as `as_table`: labels in a narrow column, help text mixed with controls, no marking of required/optional fields, errors only at the field, vague texts ("the pretty name", "Private?", "type"), dates "in UTC" without saying what an empty value means. | `election_new.html`, `election_edit.html`, `election_extend.html`, `voters_email.html`, `helios/forms.py` | P2 | done: shared field include with labels above, hints, "(optional)", error summary linking to fields; clearer texts |
| A7 | Voter list: actions scattered, "[x]" delete links without accessible names, no empty state, eligibility radios without labels; upload page: file field without label, button "go". | `voters_list.html`, `voters_upload.html`, `voters_upload_confirm.html` | P2 | done |
| A8 | Trustees: adding one is guarded by a long `confirm()` popup; no visible key status; "[x]" without names. | `list_trustees.html`, `new_trustee.html` | P2 | done |
| A9 | Results are raw counts only. | `election_view.html` | P3 | done: share of ballots with a bar |
| A10 | Start page: flat list of headings and buttons, no status per election, invalid list markup. | `server_ui/templates/index.html`, `elections_administered.html` | P3 | done: cards with status badges and empty states |
| A11 | Dates are entered in UTC. The existing helper shows the local equivalent once a value is entered, but the field itself is still UTC. | `helios/forms.py`, `helios/widgets.py` | P2 | done: entry and display in `ELECTION_TIME_ZONE` (default `TIME_ZONE`), stored in UTC as before |
| A12 | The email preview on "Contact voters" always shows the block "We have recorded your vote with ballot tracker <SMART_TRACKER>" (the preview voter has a placeholder tracker), although real invitations contain it only for voters who have already voted. Administrators read it as part of every invitation. | `voters_email` view, `helios/templates/email/vote_body.txt` | P3 | done: the preview marks the conditional parts in square brackets (see third round); real emails unchanged |

## Voter

| # | Finding | Where | Prio | Status |
|---|---------|-------|------|--------|
| V1 | After "Proceed to Login" in the booth, many voters believe they have voted. The review step does not say that the ballot is not cast yet. | `heliosbooth/templates/seal.html` | P1 | done: explicit notice and next step; cast page says "Last step" |
| V2 | Booth: small checkboxes as the only click target, no focus management when steps change (screen readers don't notice), minimum-answers error in an `alert()`, dated look. | `heliosbooth/vote.html`, `templates/question.html`, `css/` | P2 | done: whole answer row clickable, focus moves to the step's heading, inline error, new `booth-ui.css` |
| V3 | Voter login: no autocomplete hints (password managers and autofill don't work, WCAG 3.3.8), voter ID gets auto-capitalised on phones, error message below the button. | `_castconfirm_password.html`, `helios/forms.py` | P2 | done |
| V4 | Vote cast page: tracker in plain text, no way to copy it, no next action. | `cast_done.html` | P2 | done: tracker box with Copy button, link to the tracking center, button back |
| V5 | Login choice shows bare system names ("google", "devlogin") and emits stray `</a></p>` for the password system. | `helios_auth/templates/login_box.html` | P2 | done: buttons with icon and the system's login message (translatable) |
| V6 | Ballot tracker meaning is never explained where it is shown. | cast pages, tracking center | P3 | done |
| V7 | The booth is a separate jQuery 1.2 / jTemplates app with its own styles and a frame that predates phones; the start screen still mentions steps in generic terms. | `heliosbooth/` | P3 | partly done: step list, start screen, structured review, grouped tracker, no popups; the jTemplates/jQuery rewrite was deliberately not attempted |

## Trustee

| # | Finding | Where | Prio | Status |
|---|---------|-------|------|--------|
| T1 | Trustee home lists links without context; the trustee doesn't see their two tasks and where they stand. | `trustee_home.html` | P2 | done: two-step progress |
| T2 | Key setup: no overview, typos ("Bellow", "dowload"), secret key field unlabelled, navigation by `javascript:` links, the file name for the download is not escaped. | `election_keygenerator.html` | P2 | done |
| T3 | Decryption and key check pages: unlabelled text areas, progress messages not announced, "OH OH, your key is bad." | `trustee_decrypt_and_prove.html`, `trustee_check_sk.html` | P3 | done |

## Implemented in this branch

One commit per topic, all with German translations and tests at the
known baseline:

1. **Layout: styles for the elements the application pages use** – component
   section in `layout.css` (buttons, forms, tables, callouts, badges, details,
   focus, phone rules), logo height, skip link, splash layout only on the
   start and login pages. (G1–G5)
2. **Election page: status, progress steps and one clear next action**
   (`helios/election_progress.py`). (A1)
3. **Confirmation pages for freeze, tally, result and voter upload.** (A2–A4)
4. **Forms: labels above fields, hints, optional markers and an error
   summary** (`helios/templates/_form_fields.html`). (A6)
5. **Question editor: working styles, labels and keyboard-usable controls**
   (`helios/media/question-editor.css`). (A5)
6. **Voters and trustees pages: grouped actions, empty states, clear status.**
   (A7, A8; also removes the old Foundation button rules)
7. **Voter pages: casting, confirmation and credentials.** (V1, V3, V4, V6)
8. **Voting booth: clearer steps, large answer targets, focus handling**
   (`heliosbooth/css/booth-ui.css`; encryption untouched). (V1, V2)
9. **Start page and election lists: sections, status badges, empty states.**
   (A10)
10. **Trustee pages: two-step overview, labelled key fields, plain messages.**
    (T1–T3)
11. **Login options and error pages.** (V5, G6)
12. **Results: share of ballots per answer, with a bar.** (A9)

Two tests that matched old markup literally (`>x</button></form>]`,
`opacity: 0.5`) now check the delete form's URL and `aria-disabled`.

## Second round: the deferred items

Each item is its own commit on `ux-redesign`, with German translations,
tests where there is logic, and a Playwright check (EN/DE, 1280 px and
390 px, light and dark):

1. **Election dates in local time** (`helios/timezones.py`,
   `LocalDateTimeField`, `ELECTION_TIME_ZONE`). Administrators enter voting
   start, end and extension in the installation's zone; the hint names the
   zone and its current offset, stored values are pre-filled in local
   time, and pages show e.g. "1 December 2026, 08:00 CET" (rendered on the
   server; the browser adds the visitor's own time if their zone
   differs). Storage and all comparisons stay naive UTC. A time that
   occurs twice when the clocks go back is taken as the first one; a time
   skipped when they go forward is refused with a message. Tests cover
   winter/summer, both DST nights, pre-filling, saving, `UTC` as the zone
   and the typical configuration `USE_TZ=False` with
   `TIME_ZONE=Europe/Berlin` (including `voting_has_started()`).
2. **Guided setup** (`helios/wizard_views.py`, `/helios/elections/new/guided`):
   name and description, voting period, options, then a "check your
   answers" summary with change links; nothing is created before the last
   step, the short name is derived from the name if left empty. The
   single form stays at `/helios/elections/new` (linked both ways). After
   creation the setup pages (questions, voters, trustees, freeze) show
   "Election setup - step n of 5" and Next/Back links until the ballot is
   frozen. No existing URL or form field changed.
3. **Booth and question editor** - presentation only:
   - booth: step list instead of a layout table, exit as a button, start
     screen with the number of questions and "cast only in the last
     step", structured review with named "change" buttons, grouped
     tracker and election fingerprint, status regions, audit buttons that
     no longer submit, the "audited ballot posted" notice in the page
     instead of `alert()`. Encryption, ballot format, hashes and the cast
     form are untouched; the tracker text was checked to equal
     `BOOTH.encrypted_ballot_hash`;
   - question editor (user request): the "add a question" form starts with
     one answer field; "Add answer" adds any number, each row has a
     "Remove" button, rows are renumbered without gaps (labels and the
     `answer_n` names that the save code reads), empty fields are
     ignored, editing shows exactly the existing answers, min/max are
     checked against the actual number of answers; errors are shown in the
     form instead of `alert()`, changes are announced in a live region;
   - question editor: labelled rule fields (minimum, maximum, result type,
     answer order), move down as well as up, buttons named per question,
     "saved" announcement.
4. **Ballot tracker**: shown in groups of four characters everywhere
   (cast pages, election page, tracking center, booth, audit pages). The
   groups are separate elements without spaces, so copying and the Copy
   button give the exact tracker. The vote-cast page adds a link to the
   ballot on the server and a QR code of that link (`/helios/vh/<full
   tracker>`), generated on the server as inline SVG with the vendored
   MIT-licensed `helios/vendor/qrcodegen.py` (Project Nayuki, 1.8.0) -
   no CDN, no JavaScript. The `vh/` route now accepts the "/" that base64
   trackers can contain (such links were 404 before). The cast vote page
   keeps the full fingerprint as one string for verifiers.
5. **Voter groups, admin (stats) and audit pages** in the shared layout:
   h1 titles, shared form fields instead of `as_table`, tables with
   headers, named and confirmed delete/undelete buttons, labelled
   searches, pagers, empty states, no inline styles.
6. **Dark mode** (`DARK_MODE`, default `auto` without `SITE_CSS_URL`,
   `off` with it): all component colors are variables now; the dark block
   only redefines them (text 13:1, muted 7.7:1, links 8.9:1). A site
   stylesheet is never combined with dark colors unless the site opts in
   (see docs/configuration.md). The booth stayed light in this round (now
   follows `DARK_MODE`, see the third round).

Merged into this branch on request: the sticky footer (`footer-fixed`),
the fix for contacting voters of open-registration elections
(`fix-voters-email-openreg`; the "email voters" button on the election
page now uses the same check as the voter list) and the navigation bar
with breadcrumbs (`navigation`). With the breadcrumbs in place, the
`.back-link` links above the page titles were removed.

## Third round: five open items

Each is its own commit on `ux-open-items`, with German translations, tests
and a Playwright check (EN/DE, 1280 px and 390 px):

1. **Email preview** (A12): the preview passes `email_preview` to the
   email templates, which then mark the parts only some voters receive:
   "[Only for voters who have already voted:] ... [End of the part ...]"
   around the ballot tracker of the invitation, and both alternatives of
   the result email (voted / not voted). A sentence above the preview
   explains the notes. Real emails never set the flag; their text was
   compared byte for byte with the old templates.
2. **Questions checked on the server**: `save_questions` rejects what the
   editor already rejects (whole numbers, 0 <= min <= max <= number of
   answers, max at least 1 or unlimited, at least one non-empty answer,
   http(s) links, result type absolute/relative) with HTTP 400 and a
   translated message naming the question; the editor shows it in the
   form's error box (or above the questions for remove/move). The page's
   question list changes only once the server accepted it. Existing
   formats stay valid (max `null`, a missing min, other choice/tally types,
   extra keys, fixtures and legacy data).
3. **Unsaved input in the question editor**: with a question being added
   or a changed edit form, links (setup steps, Next/Back, breadcrumbs,
   menu) ask with a translated confirm; other ways of leaving get the
   browser's leave dialog. The language switch keeps the input (its
   "questions" hook) and is never asked about; there is never a second
   dialog.
4. **Seat table** (voter groups, "prepare election"): a row header with the
   vote number and a hidden label per input, e.g. "Email, vote 3".
5. **Dark mode for the booth**: the static booth loads
   `/booth-dark-mode.css` (Django, outside `/booth/`), which is
   `heliosbooth/css/booth-dark.css` with `DARK_MODE=auto` and empty with
   `off`. Colors only; text contrast at least 5:1 in every step, the
   white GIFs on a light plate. A complete vote (with audit) was cast in
   dark mode; with `DARK_MODE=off` the booth stays light on a dark system.

## Open recommendations

| Recommendation | Effort | Rationale for not doing it here |
|----------------|--------|----------------------------------|
| Replace jTemplates in the question editor and booth with server-rendered or modern client-side templates. (V7) | L | Still open on purpose: the booth's code is security-relevant; the presentation was improved without touching it. Needs an end-to-end test suite (Postgres-based CI) first. |
| Structure the invitation email (what / how / deadline / help). (A12; the preview part is done) | S | Email wording is what voters see first and should be reviewed by the operators. |
| Optional "check answers" page before freezing that lists questions, answers, voters count, trustees and dates in one place. | S–M | The freeze page now summarises the most important points; a full summary needs the same data as the progress panel plus question details. |
| Verifier pages (`heliosverifier/`, `heliosbooth/single-ballot-verify.html`): explanations for non-experts, consistent styling. The audited-ballots page is done. | M | Expert-facing; the explanations should be reviewed by someone who maintains the verification docs. |
| Automated accessibility checks (axe-core in the Playwright run) in CI. | S | Requires a browser in CI; worth adding together with screenshot tests. |

## Things to check in a browser

The pages were rendered with headless Chromium at 1280 px and 390 px width
in English and German, but a person should still look at:

- the site's own stylesheet (`SITE_CSS_URL`) on top of the new component
  styles, especially buttons, the login box and the narrower splash
  (the splash is now used only on the start and login pages);
- the booth on a real phone (answer rows, full-width buttons, focus
  moving to headings with a screen reader);
- keyboard-only use of the question editor (add link, edit, remove);
- the election page in each state (draft, ready, scheduled, open, closed,
  tallying, waiting for trustees, result ready, released) with real data;
- German texts that may wrap awkwardly in buttons ("Wahl anlegen und
  weiter", "Stimmzettel einfrieren und Wahl öffnen").

After the second round, please also check by hand:

- **Time zone**: `ELECTION_TIME_ZONE` (or `TIME_ZONE`) of the real
  installation; edit an existing election and confirm the pre-filled
  start/end match what was intended (they were entered in UTC before);
  an election across a clock change; a browser in another zone (the
  "your time" note).
- **Guided setup** with a real login system, including going back and
  changing a step, and the setup navigation on questions, voters,
  trustees and freeze.
- **QR code** on the vote-cast page with a phone camera; the link must
  open the ballot (also for trackers containing "/").
- **Question editor** with keyboard and a screen reader: adding and
  removing answers, focus and announcements, the error box.
- **Booth** on a real phone and with a screen reader (step list, review
  list, grouped tracker read as one string).
- **Dark mode** with the site's stylesheet: off by default with
  `SITE_CSS_URL`; if the site wants it, set `DARK_MODE=auto` and check the
  site colors, logo and login icons on dark backgrounds.
- **Booth in dark mode** on a real phone, and with a site stylesheet that
  opts into `DARK_MODE=auto` (the booth uses its own colors, not the
  site's variables).
- **Draft elections with questions saved before the server checks**
  (e.g. a maximum above the number of answers): every save of the
  question list is now refused until that question is fixed; the message
  names it. Frozen elections are not affected.
- **Question editor**: leaving with unsaved input in other browsers
  (Firefox and Safari show their own leave dialog text).
- **Breadcrumbs instead of back links** on the trustee pages (key setup,
  check key, decryption) when logged in as a trustee.
