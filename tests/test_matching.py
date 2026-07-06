"""Unit tests for the generic goal->trigger matching primitives hardened in issue #11:
recipe.goal_nouns / recipe.goal_needle / howto.goal_regex / howto.form_confidence.

These are the substring/word-boundary rules that decide whether a goal matches a
trigger label; regressing them is exactly how the LinkedIn false positives crept in.
"""
import re

from pinchtab_webgraph import recipe, howto


# --- goal_nouns: drop stopwords, create-VERBS, and ≤2-char tokens ------------

def test_goal_nouns_drops_short_and_stopwords():
    # "in", "a", "to" are the reported offenders — all gone.
    assert recipe.goal_nouns("sign in") == ["sign"]
    assert recipe.goal_nouns("post a job") == ["post", "job"]
    assert recipe.goal_nouns("add a new report to my team") == ["report", "team"]


def test_goal_nouns_drops_create_verbs():
    # the create-VERBS are matched via VERBS, never as a noun.
    assert "create" not in recipe.goal_nouns("create role")
    assert recipe.goal_nouns("create role") == ["role"]


def test_goal_nouns_empty_goal():
    assert recipe.goal_nouns("") == []
    assert recipe.goal_nouns(None) == []


# --- goal_regex: word boundaries stop substring false positives --------------

def test_goal_regex_no_substring_match_inside_word():
    # the canonical bug: "in" (from "sign in") must NOT match inside "Find".
    rx = howto.goal_regex("sign in")
    assert not rx.search("Find a new job")


def test_goal_regex_matches_real_create_trigger():
    rx = howto.goal_regex("create role")
    assert rx.search("Create Role")
    assert rx.search("Add Role")            # verb+noun in either order
    assert not rx.search("Delete Widget")


def test_goal_regex_new_job_still_matches_job_goals():
    # this SHOULD still regex-match (verb "new" + noun "job") — it's the confidence
    # gate, not the regex, that later rejects it as a 0-field nav.
    rx = howto.goal_regex("post a job")
    assert rx.search("Find a new job")


# --- noun_alt: whole-word matching, plural-tolerant, prefix-safe -------------

def test_noun_alt_matches_plural_label():
    # singular goal noun must still match a pluralized label ("release" -> "Releases")
    rx = re.compile(recipe.noun_alt(["release"]), re.I)
    assert rx.search("New Releases")
    assert rx.search("Release notes")
    assert re.compile(recipe.noun_alt(["report"]), re.I).search("Add Reports")
    assert re.compile(recipe.noun_alt(["cliente"]), re.I).search("Añadir clientes")


def test_noun_alt_does_not_match_prefix_of_longer_word():
    # "sign" must NOT match inside "signature"; "job" must NOT match inside "jobseeker".
    assert not re.compile(recipe.noun_alt(["sign"]), re.I).search("Signature settings")
    assert not re.compile(recipe.noun_alt(["job"]), re.I).search("Jobseeker hub")


def test_noun_alt_does_not_match_inside_word():
    # the canonical bug again, at the alternation level: "in" inside "Find".
    assert not re.compile(recipe.noun_alt(["in"]), re.I).search("Find")


# --- form_confidence: zero-field forms are low confidence --------------------

def test_form_confidence_zero_fields_is_low():
    assert howto.form_confidence({"form": {"fieldCount": 0, "fields": []}}) == "low"
    assert howto.form_confidence({"form": None}) == "low"
    assert howto.form_confidence({}) == "low"


def test_form_confidence_real_form_is_high():
    assert howto.form_confidence({"form": {"fieldCount": 2}}) == "high"
    # fieldCount missing → fall back to counting fields
    assert howto.form_confidence({"form": {"fields": [{"label": "x"}]}}) == "high"
