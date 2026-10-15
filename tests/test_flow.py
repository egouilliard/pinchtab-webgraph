"""Tests for flow.py — the flow document model (pure: parse / validate / substitute).

validate() is the gate a saved flow crosses BEFORE a browser is ever leased, so it must
reject at save time everything the runner would otherwise blow up on at 3am. The reference
checks carry three regressions that did exactly that (see the `regression:` tests).
"""
import json

import pytest

from pinchtab_webgraph import flow


def _flow(**kw):
    doc = {"name": "f", "steps": [{"op": "goto", "url": "https://x.test/"}]}
    doc.update(kw)
    return doc


# --- structure ---------------------------------------------------------------

def test_valid_minimal_flow():
    assert flow.validate(_flow()) is not None


@pytest.mark.parametrize("doc, needle", [
    ("not a dict", "JSON object"),
    ({"steps": []}, "missing required key 'name'"),
    ({"name": "f"}, "missing required key 'steps'"),
    ({"name": "  ", "steps": [{"op": "log", "message": "x"}]}, "non-empty string"),
    ({"name": "f", "steps": []}, "non-empty list"),
    ({"name": "f", "steps": [{"op": "frobnicate"}]}, "unknown op"),
    ({"name": "f", "steps": ["nope"]}, "must be an object"),
    ({"name": "f", "steps": [{"op": "fill", "selector": "#a"}]}, "requires 'value'"),
    ({"name": "f", "steps": [{"op": "click"}]}, "requires one of"),
    ({"name": "f", "steps": [{"op": "for_each", "match": {}}]}, "non-empty `body`"),
    ({"name": "f", "steps": [{"op": "log", "message": "x", "body": []}]}, "takes no `body`"),
    ({"name": "f", "steps": [{"op": "log", "message": "x"}],
      "capabilities": {"allow_everything": True}}, "unknown capability"),
    ({"name": "f", "steps": [{"op": "log", "message": "x"}],
      "inputs": {"a": {"type": "blob"}}}, "unsupported type"),
])
def test_structural_rejections(doc, needle):
    with pytest.raises(flow.FlowError) as exc:
        flow.validate(doc)
    assert needle in str(exc.value)


def test_nesting_depth_guard():
    step = {"op": "log", "message": "x"}
    for _ in range(flow.MAX_DEPTH + 1):
        step = {"op": "paginate", "body": [step]}
    with pytest.raises(flow.FlowError, match="nested deeper"):
        flow.validate(_flow(steps=[step]))


def test_total_step_guard():
    steps = [{"op": "log", "message": "x"}] * (flow.MAX_STEPS + 1)
    with pytest.raises(flow.FlowError, match="more than"):
        flow.validate(_flow(steps=steps))


def test_error_carries_a_path():
    with pytest.raises(flow.FlowError) as exc:
        flow.validate(_flow(steps=[{"op": "log", "message": "x"},
                                   {"op": "paginate", "body": [{"op": "nope"}]}]))
    assert exc.value.path == "steps[1].body[0]"


# --- references --------------------------------------------------------------

def test_declared_input_is_in_scope():
    flow.validate(_flow(inputs={"since": {"type": "string"}},
                        steps=[{"op": "fill", "selector": "#d", "value": "${since}"}]))


def test_undeclared_variable_is_rejected():
    with pytest.raises(flow.FlowError, match=r"\$\{nope\}"):
        flow.validate(_flow(steps=[{"op": "fill", "selector": "#d", "value": "${nope}"}]))


def test_loop_variable_is_in_scope_inside_the_body_only():
    flow.validate(_flow(steps=[
        {"op": "for_each", "match": {"kind": "download"}, "as": "row",
         "body": [{"op": "download", "href": "${row.href}"}]}]))
    with pytest.raises(flow.FlowError, match="row"):
        flow.validate(_flow(steps=[
            {"op": "for_each", "match": {}, "as": "row",
             "body": [{"op": "log", "message": "x"}]},
            {"op": "log", "message": "${row.text}"}]))


def test_set_var_is_in_scope_afterwards():
    flow.validate(_flow(steps=[{"op": "set", "var": "n", "value": 1},
                               {"op": "log", "message": "${n}"}]))


def test_run_builtin_is_always_in_scope():
    flow.validate(_flow(steps=[{"op": "log", "message": "${run.name}"}]))


# regression (A4): a `collect` step's `into` var must be added to scope — the runner sets it
# at runtime, so a document that reads it back used to pass at run time and fail validate().
def test_regression_collect_into_is_in_scope():
    flow.validate(_flow(steps=[{"op": "collect", "into": "rows"},
                               {"op": "log", "message": "${rows}"}]))


# regression (A5): `page`/`index` are SCOPED, not global builtins. Referencing them outside a
# paginate/for_each body used to validate, then raise a FlowError at RUNTIME — which the
# runner turns into an abort. A document that validates must not kill its own run.
def test_regression_page_is_scoped_to_a_paginate_body():
    flow.validate(_flow(steps=[{"op": "paginate", "body": [{"op": "log", "message": "p${page}"}]}]))
    with pytest.raises(flow.FlowError, match="page"):
        flow.validate(_flow(steps=[{"op": "log", "message": "${page}"}]))


def test_regression_index_is_scoped_to_a_for_each_body():
    flow.validate(_flow(steps=[
        {"op": "for_each", "match": {}, "body": [{"op": "log", "message": "${index}"}]}]))
    with pytest.raises(flow.FlowError, match="index"):
        flow.validate(_flow(steps=[{"op": "log", "message": "${index}"}]))


# regression (A6): `goto` can target a trigger by `match` alone — the runner forwards it to
# api.resolve_action, which resolves on a label regex with no goal.
def test_regression_goto_accepts_match_alone():
    flow.validate(_flow(steps=[{"op": "goto", "match": "Invoices"}]))


# --- capabilities ------------------------------------------------------------

def test_capabilities_defaults():
    caps = flow.capabilities(_flow())
    assert caps == {"allow_submit": False, "allow_download": True, "allow_upload": False}


def test_undeclared_upload_is_rejected():
    with pytest.raises(flow.FlowError, match="allow_upload"):
        flow.validate(_flow(steps=[{"op": "upload", "selector": "#f", "file": "/x.pdf"}]))


def test_declared_upload_is_accepted():
    flow.validate(_flow(capabilities={"allow_upload": True},
                        steps=[{"op": "upload", "selector": "#f", "file": "/x.pdf"}]))


def test_undeclared_submit_is_rejected_even_nested():
    with pytest.raises(flow.FlowError, match="allow_submit"):
        flow.validate(_flow(steps=[
            {"op": "paginate", "body": [{"op": "do", "goal": "create team", "submit": True}]}]))


def test_declared_submit_is_accepted():
    flow.validate(_flow(capabilities={"allow_submit": True},
                        steps=[{"op": "do", "goal": "create team", "submit": True}]))


# --- goto query (deep links) ---------------------------------------------------

def test_goto_query_with_url_is_accepted():
    flow.validate(_flow(steps=[{"op": "goto", "url": "https://x.test/l",
                                "query": {"q": "hello world", "n": 3, "ok": True}}]))


@pytest.mark.parametrize("step, needle", [
    ({"op": "goto", "goal": "invoices", "query": {"q": "x"}}, "only together with `url`"),
    ({"op": "goto", "match": "Invoices", "query": {"q": "x"}}, "only together with `url`"),
    ({"op": "goto", "url": "https://x.test/", "query": "q=x"}, "an object"),
    ({"op": "goto", "url": "https://x.test/", "query": ["q"]}, "an object"),
    ({"op": "goto", "url": "https://x.test/", "query": {"q": {"a": 1}}}, "string, number"),
    ({"op": "goto", "url": "https://x.test/", "query": {"q": None}}, "string, number"),
    ({"op": "goto", "url": "https://x.test/", "query": {"": "x"}}, "empty name"),
])
def test_goto_query_rejections(step, needle):
    with pytest.raises(flow.FlowError, match=needle):
        flow.validate(_flow(steps=[step]))


def test_goto_query_values_are_checked_as_variable_references():
    # the values are substituted at run time, so an unknown ${ref} must fail HERE.
    with pytest.raises(flow.FlowError, match=r"\$\{nope\}"):
        flow.validate(_flow(steps=[{"op": "goto", "url": "https://x.test/",
                                    "query": {"q": "${nope}"}}]))
    flow.validate(_flow(inputs={"subject": {"type": "string"}},
                        steps=[{"op": "goto", "url": "https://x.test/",
                                "query": {"q": "${subject}"}}]))


def test_with_query_percent_encodes_spaces_amp_and_unicode():
    url = flow.with_query("https://x.test/l", {"subject": "Q3 review & plan", "who": "Zoë/Ana"})
    assert url == "https://x.test/l?subject=Q3%20review%20%26%20plan&who=Zo%C3%AB%2FAna"
    assert "+" not in url                         # %20, never the form-encoding `+`


def test_with_query_appends_with_an_ampersand_when_the_url_already_has_a_query():
    assert flow.with_query("https://x.test/l?a=1", {"b": "2"}) == "https://x.test/l?a=1&b=2"
    assert flow.with_query("https://x.test/l?", {"b": "2"}) == "https://x.test/l?b=2"


def test_with_query_goes_before_the_fragment():
    assert flow.with_query("https://x.test/l#top", {"b": "2"}) == "https://x.test/l?b=2#top"
    assert flow.with_query("https://x.test/l?a=1#top", {"b": "2"}) == "https://x.test/l?a=1&b=2#top"


def test_with_query_stringifies_scalars_and_keeps_param_order():
    url = flow.with_query("https://x.test/", {"z": 1, "a": 1.5, "t": True, "f": False})
    assert url == "https://x.test/?z=1&a=1.5&t=true&f=false"


def test_with_query_with_no_params_returns_the_url_unchanged():
    assert flow.with_query("https://x.test/l#h", {}) == "https://x.test/l#h"


@pytest.mark.parametrize("value, needle", [(None, "has no value"), ([1], "string, number"),
                                           ({"a": 1}, "string, number")])
def test_with_query_refuses_a_value_that_resolved_to_a_non_scalar(value, needle):
    with pytest.raises(flow.FlowError, match=needle):
        flow.with_query("https://x.test/", {"q": value})


# --- press ---------------------------------------------------------------------

@pytest.mark.parametrize("key", ["Enter", "Escape", "Backspace", "ArrowDown", "Tab"])
def test_press_accepts_a_single_key_name(key):
    flow.validate(_flow(steps=[{"op": "press", "key": key}]))
    flow.validate(_flow(steps=[{"op": "press", "key": key, "selector": "#box"}]))


@pytest.mark.parametrize("key", ["Control+a", "Shift+Tab", "Meta+Enter", "+"])
def test_press_rejects_a_key_combination_with_a_clear_message(key):
    with pytest.raises(flow.FlowError) as exc:
        flow.validate(_flow(steps=[{"op": "press", "key": key}]))
    assert "ONE key name" in str(exc.value) and repr(key) in str(exc.value)
    assert "contains '+'" in str(exc.value)
    assert exc.value.path == "steps[0]"


def test_press_requires_a_key_and_a_non_empty_one():
    with pytest.raises(flow.FlowError, match="requires 'key'"):
        flow.validate(_flow(steps=[{"op": "press"}]))
    for bad in ("", "  ", 7):
        with pytest.raises(flow.FlowError, match="non-empty key name"):
            flow.validate(_flow(steps=[{"op": "press", "key": bad}]))


def test_press_with_a_variable_key_is_checked_at_run_time_not_here():
    flow.validate(_flow(inputs={"k": {"type": "string"}},
                        steps=[{"op": "press", "key": "${k}"}]))


def test_press_selector_must_be_a_non_empty_string():
    with pytest.raises(flow.FlowError, match="`selector` to be a non-empty string"):
        flow.validate(_flow(steps=[{"op": "press", "key": "Enter", "selector": ""}]))


# --- type ----------------------------------------------------------------------

def test_type_needs_a_target_and_a_value():
    flow.validate(_flow(steps=[{"op": "type", "selector": "#to", "value": "ana"}]))
    flow.validate(_flow(steps=[{"op": "type", "label": "To", "value": "ana"}]))
    with pytest.raises(flow.FlowError, match="requires 'value'"):
        flow.validate(_flow(steps=[{"op": "type", "selector": "#to"}]))
    with pytest.raises(flow.FlowError, match="requires one of: selector, label"):
        flow.validate(_flow(steps=[{"op": "type", "value": "ana"}]))


# --- extract ---------------------------------------------------------------------

def test_extract_needs_into_and_selector():
    flow.validate(_flow(steps=[{"op": "extract", "into": "title", "selector": "h1"}]))
    with pytest.raises(flow.FlowError, match="requires 'into'"):
        flow.validate(_flow(steps=[{"op": "extract", "selector": "h1"}]))
    with pytest.raises(flow.FlowError, match="requires 'selector'"):
        flow.validate(_flow(steps=[{"op": "extract", "into": "title"}]))


def test_extract_into_is_a_defined_variable_for_later_steps():
    flow.validate(_flow(steps=[{"op": "extract", "into": "link", "selector": "a",
                                "attr": "href"},
                               {"op": "goto", "url": "${link}"},
                               {"op": "log", "message": "at ${link}"}]))
    # …and only AFTER the extract, like set/collect.
    with pytest.raises(flow.FlowError, match=r"\$\{link\}"):
        flow.validate(_flow(steps=[{"op": "log", "message": "${link}"},
                                   {"op": "extract", "into": "link", "selector": "a"}]))


def test_extract_into_inside_a_body_does_not_leak_out_of_it():
    with pytest.raises(flow.FlowError, match=r"\$\{row\}"):
        flow.validate(_flow(steps=[
            {"op": "for_each", "match": {}, "body": [
                {"op": "extract", "into": "row", "selector": "td"}]},
            {"op": "log", "message": "${row}"}]))


@pytest.mark.parametrize("step, needle", [
    ({"op": "extract", "into": "a b", "selector": "h1"}, "variable name"),
    ({"op": "extract", "into": "a.b", "selector": "h1"}, "variable name"),
    ({"op": "extract", "into": "1a", "selector": "h1"}, "variable name"),
    ({"op": "extract", "into": "run", "selector": "h1"}, "reserved"),
    ({"op": "extract", "into": "t", "selector": ""}, "`selector` to be a non-empty string"),
    ({"op": "extract", "into": "t", "selector": "h1", "attr": ""}, "`attr`"),
    ({"op": "extract", "into": "t", "selector": "h1", "pattern": 5}, "`pattern`"),
    ({"op": "extract", "into": "t", "selector": "h1", "all": "yes"}, "`all` to be true or false"),
    ({"op": "extract", "into": "t", "selector": "h1", "required": "false"},
     "`required` to be true or false"),
])
def test_extract_rejections(step, needle):
    with pytest.raises(flow.FlowError, match=needle):
        flow.validate(_flow(steps=[step]))


def test_extract_accepts_every_optional_key():
    flow.validate(_flow(steps=[{"op": "extract", "into": "ids", "selector": "li a",
                                "attr": "href", "all": True, "pattern": "^https://",
                                "required": False}]))


# --- submit / js flags on click and press ------------------------------------------

@pytest.mark.parametrize("step", [
    {"op": "click", "selector": "#send", "submit": True},
    {"op": "click", "text": "Send", "submit": True, "js": True},
    {"op": "press", "key": "Enter", "selector": "#box", "submit": True},
])
def test_a_submit_marked_click_or_press_needs_allow_submit(step):
    with pytest.raises(flow.FlowError, match="allow_submit"):
        flow.validate(_flow(steps=[step]))
    with pytest.raises(flow.FlowError, match="allow_submit"):     # …even nested
        flow.validate(_flow(steps=[{"op": "paginate", "body": [step]}]))
    flow.validate(_flow(capabilities={"allow_submit": True}, steps=[step]))


def test_submit_false_or_absent_is_not_a_write():
    flow.validate(_flow(steps=[{"op": "click", "selector": "#a", "submit": False},
                               {"op": "press", "key": "Enter"},
                               {"op": "click", "selector": "#b", "js": True}]))


@pytest.mark.parametrize("step, needle", [
    ({"op": "click", "selector": "#a", "submit": "false"}, "`submit` to be true or false"),
    ({"op": "click", "selector": "#a", "js": 1}, "`js` to be true or false"),
    ({"op": "press", "key": "Enter", "submit": "yes"}, "`submit` to be true or false"),
])
def test_write_flags_must_be_real_booleans(step, needle):
    # a string "false" is truthy: a flag that reads as OFF but behaves as ON is the bug.
    with pytest.raises(flow.FlowError, match=needle):
        flow.validate(_flow(capabilities={"allow_submit": True}, steps=[step]))


def test_the_new_ops_are_in_the_op_tables():
    assert {"press", "type", "extract"} <= set(flow.LEAF_OPS)
    assert "query" in flow.LEAF_OPS["goto"]["opt"]
    assert {"submit", "js"} <= set(flow.LEAF_OPS["click"]["opt"])
    assert len(flow.LEAF_OPS) == 15 and len(flow.BODY_OPS) == 2


# --- substitute --------------------------------------------------------------

def test_substitute_whole_string_keeps_the_native_type():
    scope = {"item": {"count": 3, "ok": True, "rows": [1, 2]}}
    assert flow.substitute("${item.count}", scope) == 3
    assert flow.substitute("${item.ok}", scope) is True
    assert flow.substitute("${item.rows}", scope) == [1, 2]


def test_substitute_embedded_reference_interpolates_as_text():
    assert flow.substitute("page ${p} of 9", {"p": 3}) == "page 3 of 9"


def test_substitute_recurses_into_dicts_and_lists():
    got = flow.substitute({"a": ["${x}", "n=${x}"], "b": {"c": "${x}"}}, {"x": 7})
    assert got == {"a": [7, "n=7"], "b": {"c": 7}}


def test_substitute_leaves_non_strings_alone():
    assert flow.substitute(5, {}) == 5
    assert flow.substitute(None, {}) is None


def test_substitute_unknown_variable_raises():
    with pytest.raises(flow.FlowError, match="unknown variable"):
        flow.substitute("${nope.deep}", {"nope": {}})


def test_variable_names_finds_every_reference():
    assert sorted(flow.variable_names({"a": "${x}", "b": ["${y.z}"]})) == ["x", "y.z"]


# --- bind_inputs -------------------------------------------------------------

_INPUTS = {"inputs": {
    "since": {"type": "string", "required": True},
    "limit": {"type": "integer", "default": 10},
    "ratio": {"type": "number"},
    "deep": {"type": "boolean"},
}}


def test_bind_inputs_coerces_types():
    got = flow.bind_inputs(_flow(**_INPUTS),
                           {"since": "2026-01-01", "limit": "5", "ratio": "1.5",
                            "deep": "true"})
    assert got == {"since": "2026-01-01", "limit": 5, "ratio": 1.5, "deep": True}


def test_bind_inputs_applies_defaults_and_nulls():
    got = flow.bind_inputs(_flow(**_INPUTS), {"since": "x"})
    assert got["limit"] == 10 and got["ratio"] is None


def test_bind_inputs_requires_required():
    with pytest.raises(flow.FlowError, match="missing required input"):
        flow.bind_inputs(_flow(**_INPUTS), {})


def test_bind_inputs_rejects_unknown_keys():
    # a typo'd param must NOT silently run the flow with a default.
    with pytest.raises(flow.FlowError, match="unknown input"):
        flow.bind_inputs(_flow(**_INPUTS), {"since": "x", "sinse": "y"})


def test_bind_inputs_rejects_a_bad_coercion():
    with pytest.raises(flow.FlowError, match="not a valid integer"):
        flow.bind_inputs(_flow(**_INPUTS), {"since": "x", "limit": "many"})


def test_bind_inputs_with_no_declaration_takes_nothing():
    assert flow.bind_inputs(_flow(), None) == {}


# --- bind_inputs: the `file` type --------------------------------------------
# A file input's VALUE is an absolute local path (what the `upload` op hands the bridge).
# It is checked HERE so a missing file is a clear rejection before anything is leased,
# rather than an opaque bridge error halfway through a flow that has already clicked things.

_FILE_FLOW = {"inputs": {"file": {"type": "file", "required": True}}}


def test_bind_inputs_accepts_an_existing_file(tmp_path):
    p = tmp_path / "invoice.pdf"
    p.write_bytes(b"%PDF-1.4")
    assert flow.bind_inputs(_flow(**_FILE_FLOW), {"file": str(p)}) == {"file": str(p)}


def test_bind_inputs_rejects_a_missing_file(tmp_path):
    missing = str(tmp_path / "nope.pdf")
    with pytest.raises(flow.FlowError) as exc:
        flow.bind_inputs(_flow(**_FILE_FLOW), {"file": missing})
    assert "no such file" in exc.value.message and missing in exc.value.message


def test_bind_inputs_rejects_a_directory_as_a_file(tmp_path):
    with pytest.raises(flow.FlowError, match="not a regular file"):
        flow.bind_inputs(_flow(**_FILE_FLOW), {"file": str(tmp_path)})


def test_bind_inputs_rejects_an_empty_file_value():
    with pytest.raises(flow.FlowError, match="no file given"):
        flow.bind_inputs(_flow(**_FILE_FLOW), {"file": ""})


def test_bind_inputs_requires_a_required_file():
    with pytest.raises(flow.FlowError, match="missing required input"):
        flow.bind_inputs(_flow(**_FILE_FLOW), {})


# --- bind_inputs: `enum` ------------------------------------------------------

_ENUM_FLOW = {"inputs": {"mode": {"type": "string", "enum": ["draft", "final"]}}}


def test_bind_inputs_accepts_a_value_in_the_enum():
    assert flow.bind_inputs(_flow(**_ENUM_FLOW), {"mode": "final"}) == {"mode": "final"}


def test_bind_inputs_rejects_a_value_not_in_the_enum():
    # a typo'd choice must NOT silently reach the site as a literal.
    with pytest.raises(flow.FlowError, match="must be one of"):
        flow.bind_inputs(_flow(**_ENUM_FLOW), {"mode": "finel"})


def test_bind_inputs_checks_the_enum_after_coercion():
    doc = _flow(inputs={"n": {"type": "integer", "enum": [1, 2, 3]}})
    assert flow.bind_inputs(doc, {"n": "2"}) == {"n": 2}     # "2" -> 2, and 2 IS in the enum
    with pytest.raises(flow.FlowError, match="must be one of"):
        flow.bind_inputs(doc, {"n": "9"})


def test_validate_rejects_a_malformed_enum():
    with pytest.raises(flow.FlowError, match="non-empty list"):
        flow.validate(_flow(inputs={"mode": {"type": "string", "enum": []}}))


# --- json_schema -------------------------------------------------------------

def test_json_schema_is_a_typed_object_schema():
    doc = _flow(inputs={"since": {"type": "string", "required": True,
                                  "description": "ISO date"},
                        "mode": {"type": "string", "enum": ["a", "b"], "default": "a"}})
    schema = flow.json_schema(doc)
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["since"]
    assert schema["properties"]["since"]["description"] == "ISO date"
    assert schema["properties"]["mode"]["enum"] == ["a", "b"]
    assert schema["properties"]["mode"]["default"] == "a"


def test_json_schema_without_inputs_has_no_required():
    assert "required" not in flow.json_schema(_flow())


def test_json_schema_maps_file_to_a_string_with_format_path():
    # {"type":"file"} is NOT valid JSON Schema; a file input's value IS a string (a path),
    # and `format` is JSON Schema's own extension point for saying which kind.
    doc = _flow(inputs={"file": {"type": "file", "required": True,
                                 "description": "the document to upload"}})
    p = flow.json_schema(doc)["properties"]["file"]
    assert p["type"] == "string" and p["format"] == "path"
    assert p["description"] == "the document to upload"
    assert flow.json_schema(doc)["required"] == ["file"]


def test_json_schema_emits_a_declared_enum():
    doc = _flow(inputs={"mode": {"type": "string", "enum": ["draft", "final"]}})
    assert flow.json_schema(doc)["properties"]["mode"]["enum"] == ["draft", "final"]


# --- load / loads ------------------------------------------------------------

def test_load_reads_and_validates(tmp_path):
    p = tmp_path / "f.json"
    p.write_text(json.dumps(_flow()))
    assert flow.load(str(p))["name"] == "f"


def test_load_rejects_bad_json(tmp_path):
    p = tmp_path / "f.json"
    p.write_text("{ nope")
    with pytest.raises(flow.FlowError, match="not valid JSON"):
        flow.load(str(p))


def test_loads_validates():
    with pytest.raises(flow.FlowError):
        flow.loads(json.dumps({"name": "f", "steps": [{"op": "bogus"}]}))


# --- validate_report: the ONE doc -> verdict function -------------------------

def test_validate_report_ok_shape():
    report = flow.validate_report(_flow(host="x.test",
                                        inputs={"since": {"type": "string"}}))
    assert report == {"status": "ok", "name": "f", "host": "x.test", "steps": 1,
                      "capabilities": {"allow_submit": False, "allow_download": True,
                                       "allow_upload": False},
                      "inputs": ["since"]}


def test_validate_report_invalid_carries_path_and_error():
    report = flow.validate_report({"name": "f", "steps": [{"op": "bogus"}]})
    assert report["status"] == "invalid"
    assert report["path"] == "steps[0]"
    assert "bogus" in report["error"]


def test_validate_report_never_raises_on_garbage():
    # a non-object body (a list, a string, None) is a structured miss, NOT an exception —
    # this is what lets an HTTP handler and an MCP tool call it on untrusted input.
    for junk in ([], "nope", None, 7, {"steps": []}):
        report = flow.validate_report(junk)
        assert report["status"] == "invalid"
        assert set(report) == {"status", "path", "error"}
