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
