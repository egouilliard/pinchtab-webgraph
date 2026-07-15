#!/usr/bin/env python3
"""
Compile a graph click-path + a terminal action into a runnable PinchTab command block.

This is the deterministic "path -> executable" layer. The rest of the toolkit records
WHERE an affordance is (stable CSS selectors, hrefs, form-field specs) and finds the
SHORTEST click-path to it; this module turns that path plus the terminal action into a
copy-pasteable block of `pinchtab` CLI commands that reproduces it. No LLM, stdlib only,
fully structural — the commands are derived from data already in the graph.

Everything is built as a list of **structured steps** (see `_cmd`/`_note`/`_blank`), which
serve two consumers: `render()` prints the human-readable command block, and
`perform.py` *executes* the same steps against a live bridge (the opt-in). The string
builders (`download_terminal` / `form_terminal` / `for_trigger` / …) are thin `render`
wrappers, so a caller that only wants text is unaffected.

One shared PREFIX (nav to the start URL, then one `nav`/`click` per routing edge) feeds
every terminal-action kind the graph can record but deliberately never executes itself:

  - download : `pinchtab download <url> -o <file>`      (direct href — a real link/file)
               `pinchtab click --css <selector>`         (JS-triggered — the browser
                                                          session captures the file)
  - upload   : `pinchtab upload <file> -s <selector>`
  - form     : `pinchtab fill/select/check ...` per field (values are placeholders);
               the SUBMIT line is emitted COMMENTED OUT by default (safety)
  - nav      : `pinchtab nav <url>`                       (one-shot shortcut to a view)
  - capture  : `pinchtab pdf -o <file>` / `pinchtab screenshot -o <file>`

SAFETY: the click-path only ever navigates; it never clicks a write/destructive control
(those are `skipped` nodes with no selector recorded). The form terminal fills fields but
leaves the submit COMMENTED unless the caller opts in with allow_submit=True. Nothing in a
compiled block mutates data on its own.
"""
import os
from urllib.parse import urlparse

# File extensions that mark an <a href> as a direct download rather than a page to load.
# Structural, not app-specific: a URL ending in one of these is a file, not a view.
DOWNLOAD_EXTS = (
    ".pdf", ".csv", ".tsv", ".xls", ".xlsx", ".doc", ".docx", ".ppt", ".pptx",
    ".zip", ".gz", ".tar", ".rar", ".7z", ".json", ".xml", ".txt", ".rtf", ".ods",
    ".odt", ".odp", ".png", ".jpg", ".jpeg", ".svg", ".ics", ".vcf", ".epub", ".mp3",
    ".mp4", ".wav", ".mov", ".bin", ".dmg", ".exe", ".apk", ".parquet", ".sql",
)


def is_direct_download(href):
    """True if a URL resolves to a file rather than a page — a `blob:`/`data:` URL or a
    path ending in a known download extension. Mirrors EXTRACT_JS's `dlDirect` (minus the
    DOM `download` attribute, which only the live JS can see). Used to classify a live
    how-to trigger as a direct download without touching the browser."""
    if not href:
        return False
    low = href.lower()
    if low.startswith(("blob:", "data:")):
        return True
    path = urlparse(href).path.lower()
    return path.endswith(DOWNLOAD_EXTS)


def shq(s):
    """POSIX single-quote a value so it is safe as a shell argument.

    Wraps in single quotes and escapes embedded single quotes the standard way
    ('...'\\''...'). None becomes an empty quoted string."""
    return "'" + str("" if s is None else s).replace("'", "'\\''") + "'"


# --- structured step model -----------------------------------------------------
# A step is a dict. Executable steps carry `argv` (the pinchtab args WITHOUT the leading
# "pinchtab", unquoted — render()/perform quote as needed). `role` classifies it for the
# executor; `needs_input` marks a step whose value is a placeholder the user must supply;
# `value_index` points at that placeholder arg; `disabled` renders the line commented out
# and is never auto-run (e.g. a form submit). Notes/blanks carry `text` and no argv.
#
# EVERY click step is emitted as `["click", "--css", <sel>, "--wait-nav"]`. `--wait-nav` is
# not an optimisation, it is required for CORRECTNESS: PinchTab's action guard returns
# `409: unexpected page navigation` when a click moves the page — AFTER the click already
# succeeded — so a caller that treats a nonzero rc as fatal (perform.execute_steps aborts on
# rc != 0 for role nav/click) would report FAILURE for an action that worked. That hits the
# common cases: a submit that redirects, a trigger that opens the form on a new URL, a
# path-prefix tab click. The flag is safe unconditionally — a non-navigating click still
# returns immediately. Fixing it HERE fixes all three consumers at once: the printed block,
# `perform.execute_steps` (runs argv verbatim), and the flow VM (runner._exec_command_step
# passes only argv[2], the selector, to browser.click(), which appends its own --wait-nav —
# so the trailing flag never double-applies). The flag is also index-safe: it is APPENDED,
# and no click step has a `value_index`.

def _cmd(argv, comment=None, role="", needs_input=False, value_index=None,
         disabled=False, label=None):
    return {"argv": list(argv), "comment": comment, "role": role,
            "needs_input": needs_input, "value_index": value_index,
            "disabled": disabled, "label": label, "text": None}


# A checkbox is the one `needs_input` step with NO `value_index`: `["check", <sel>]` has no
# value slot to substitute into. Its supplied value is therefore a BOOLEAN — check it when
# truthy, skip it when falsy. Every executor (perform.execute_steps, runner._exec_command_step)
# must branch on `value_index is None` rather than indexing argv with it.
_FALSY = {"", "0", "false", "no", "off", "none", "null", "unchecked"}


def is_truthy(value):
    """Interpret a caller-supplied value (a CLI string, or a real bool from JSON) as a
    boolean — the semantics a valueless `check` step needs."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() not in _FALSY
    return bool(value)


def _note(text):
    return {"argv": None, "comment": None, "role": "note", "needs_input": False,
            "value_index": None, "disabled": True, "label": None, "text": text}


def _blank():
    return {"argv": None, "comment": None, "role": "blank", "needs_input": False,
            "value_index": None, "disabled": True, "label": None, "text": ""}


def _render_cmd(argv):
    # argv[0] is the subcommand (nav/click/…): bare. Quote positional VALUES; leave
    # flags (-o, -s, --css) unquoted.
    parts = ["pinchtab", argv[0]]
    for a in argv[1:]:
        parts.append(a if a.startswith("-") else shq(a))
    return " ".join(parts)


def render_step(step):
    """Render one structured step back to its command-block line."""
    if step["argv"] is None:
        return step["text"] or ""
    line = _render_cmd(step["argv"])
    if step["disabled"]:
        line = "# " + line
    if step["comment"]:
        line += "   # " + step["comment"]
    return line


def render(steps):
    """Render a list of structured steps to command-block lines (list of str)."""
    return [render_step(s) for s in steps]


# --- filename / value suggestions (deterministic — no randomness) --------------

def _slug(text, default="file"):
    keep = [c.lower() if c.isalnum() else "-" for c in (text or "")]
    s = "".join(keep).strip("-")
    while "--" in s:
        s = s.replace("--", "-")
    return s[:40] or default


def suggest_filename(href=None, accept=None, label=None):
    """A sensible `-o` output name for a download. Prefer the URL's own basename when
    it already carries a file extension; else derive one from the button label + the
    first `accept` extension; else a generic fallback."""
    if href:
        base = os.path.basename(urlparse(href).path)
        if base and "." in base:
            return base
    ext = ""
    if accept:
        first = accept.split(",")[0].strip()
        if first.startswith("."):
            ext = first
        elif "/" in first and not first.endswith("*"):
            ext = "." + first.split("/")[-1]
    return _slug(label, "download") + (ext or "")


# --- path prefix (nav to start, then one step per routing edge) ----------------

def path_from_edges(epath, states):
    """Normalize cached-graph edges into `[{label, selector, href}]` steps.

    A `link` edge navigates to its destination state's URL (robust — no selector
    staleness); every other edge kind (a tab/menu `click`, an `iframe` hop) is
    reproduced by clicking its stable CSS selector on the current page."""
    steps = []
    for e in epath or []:
        if e.get("kind") == "link":
            dest = states.get(e.get("to"), {}) if states else {}
            steps.append({"label": e.get("label"), "selector": e.get("selector"),
                          "href": dest.get("url")})
        else:
            steps.append({"label": e.get("label"), "selector": e.get("selector"),
                          "href": None})
    return steps


def _path_steps(start_url, steps):
    """Structured steps for the shared prefix: nav to start, then replay each routing step."""
    out = [_cmd(["nav", start_url], role="nav")]
    for st in steps or []:
        href = st.get("href")
        label = (st.get("label") or "").strip()
        if href:
            out.append(_cmd(["nav", href], comment=label or None, role="nav"))
        elif st.get("selector"):
            out.append(_cmd(["click", "--css", st["selector"], "--wait-nav"],
                            comment=label or None, role="click"))
        else:
            out.append(_note("# (no selector recorded) click %s"
                             % (shq(label) if label else "the control")))
    return out


def nav_prefix(start_url, steps):
    """The shared prefix as rendered command lines (see `_path_steps`)."""
    return render(_path_steps(start_url, steps))


# --- terminal actions ----------------------------------------------------------

def _download_steps(href=None, selector=None, accept=None, label=None):
    if href:
        name = suggest_filename(href, accept, label)
        return [_cmd(["download", href, "-o", name], role="download")]
    if selector:
        return [_cmd(["click", "--css", selector, "--wait-nav"], role="download",
                     comment="JS-triggered — the session captures the file")]
    return [_note("# download control had neither a URL nor a selector recorded")]


def download_terminal(href=None, selector=None, accept=None, label=None):
    """Commands that perform the download itself. A direct href is fetched in-session
    (cookies intact) to a chosen path; a JS-triggered button is clicked and the browser
    session captures whatever it downloads."""
    return render(_download_steps(href, selector, accept, label))


def _upload_steps(selector=None, accept=None, file_placeholder="<FILE>"):
    if not selector:
        return [_note("# upload target had no selector recorded")]
    comment = ("accepts: %s" % accept) if accept else None
    return [_cmd(["upload", file_placeholder, "-s", selector], comment=comment,
                 role="upload", needs_input=True, value_index=1)]


def upload_terminal(selector=None, accept=None, file_placeholder="<FILE>"):
    """Command to upload a file to a file input, by its CSS selector."""
    return render(_upload_steps(selector, accept, file_placeholder))


def capture_terminal(kind="pdf", name=None):
    """Command to capture the current view as a PDF or a screenshot."""
    if kind == "screenshot":
        return render([_cmd(["screenshot", "-o", name or "view.png"], role="capture")])
    return render([_cmd(["pdf", "-o", name or "view.pdf"], role="capture")])


_FILL_TYPES = {"text", "textarea", "email", "number", "password", "search", "tel",
               "url", "date", "datetime-local", "month", "week", "time", "color",
               "range", "control"}
_CHECK_TYPES = {"checkbox", "radio", "toggle", "switch"}
_SELECT_TYPES = {"select", "dropdown", "radiogroup", "listbox", "combobox"}


def _field_value(field):
    """A readable placeholder value for a field. Prefers a real option/default when the
    spec captured one, else a `<label>`-shaped placeholder."""
    if field.get("options"):
        return field["options"][0]
    if field.get("value"):
        return field["value"]
    lab = (field.get("label") or "value").strip().lower()
    return "<%s>" % (_slug(lab, "value").replace("-", " ") or "value")


def _field_step(field):
    """One form field -> one structured step (or a note when a field has no selector —
    e.g. a cache built before selectors were captured)."""
    ftype = (field.get("type") or "text").lower()
    sel = field.get("selector")
    lab = (field.get("label") or "(unlabeled)").strip()
    req = " (required)" if field.get("required") else ""
    if not sel:
        return _note("# set %s%s [%s] — re-crawl to capture a selector"
                     % (shq(lab), req, ftype))
    if ftype in _CHECK_TYPES:
        return _cmd(["check", sel], comment=lab + req, role="check",
                    needs_input=True, label=lab)
    if field.get("accept") or ftype == "file":
        return _cmd(["upload", "<FILE>", "-s", sel],
                    comment="%s%s  accepts: %s" % (lab, req, field.get("accept") or "any"),
                    role="upload", needs_input=True, value_index=1, label=lab)
    if ftype in _SELECT_TYPES:
        return _cmd(["select", sel, _field_value(field)], comment=lab + req,
                    role="select", needs_input=True, value_index=2, label=lab)
    return _cmd(["fill", sel, _field_value(field)], comment=lab + req,
                role="fill", needs_input=True, value_index=2, label=lab)


def _form_steps(form, trigger_selector=None, opens_at=None, allow_submit=False):
    form = form or {}
    out = []
    if opens_at:
        out.append(_cmd(["nav", opens_at], comment="opens the form", role="nav"))
    elif trigger_selector:
        out.append(_cmd(["click", "--css", trigger_selector, "--wait-nav"],
                        comment="opens the form", role="click"))
    for f in form.get("fields", []):
        out.append(_field_step(f))
    sub_sel = form.get("submitSelector")
    subs = form.get("submitButtons") or []
    if sub_sel:
        if allow_submit:
            out.append(_cmd(["click", "--css", sub_sel, "--wait-nav"], role="submit",
                            comment=("submit: %s" % subs[0]) if subs else "submit"))
        else:
            out.append(_cmd(["click", "--css", sub_sel, "--wait-nav"], role="submit",
                            disabled=True, comment="submit (uncomment to save)"))
    elif subs:
        out.append(_note("# then click %s to submit" % shq(subs[0])))
    return out


def form_terminal(form, trigger_selector=None, opens_at=None, allow_submit=False):
    """Commands to open a form and fill every field with a placeholder value. The submit
    line is COMMENTED OUT unless allow_submit=True — the block never saves by itself."""
    return render(_form_steps(form, trigger_selector, opens_at, allow_submit))


# --- top-level: a full block for a how-to trigger ------------------------------

def steps_for_trigger(trigger, steps, start_url, allow_submit=False):
    """The full STRUCTURED step list for a cached/live how-to trigger: the nav prefix,
    then the terminal chosen by the trigger's `kind` (download vs. form). `steps` is a
    normalized `[{label, selector, href}]` list (see path_from_edges for cached edges).
    This is what perform.py executes."""
    out = list(_path_steps(start_url, steps))
    kind = (trigger.get("kind") or "").lower()
    tsel = trigger.get("selector") or trigger.get("triggerSelector")
    if kind == "download":
        out.append(_blank())
        out.append(_note("# --- download ---"))
        out += _download_steps(href=trigger.get("href"), selector=tsel,
                               accept=trigger.get("accept"), label=trigger.get("label"))
    else:
        out.append(_blank())
        out.append(_note("# --- fill the form (values are placeholders — edit them) ---"))
        out += _form_steps(trigger.get("form"), trigger_selector=tsel,
                           opens_at=trigger.get("opensAt"), allow_submit=allow_submit)
    return out


def for_trigger(trigger, steps, start_url, allow_submit=False):
    """The full command block (list of str) for a how-to trigger — `steps_for_trigger`
    rendered to text."""
    return render(steps_for_trigger(trigger, steps, start_url, allow_submit))


def block(lines):
    """Join a command list into a single copy-pasteable string."""
    return "\n".join(lines)
