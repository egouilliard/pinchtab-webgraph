"""Tests for pinchtab_webgraph.api — the print-free, dict-returning query surface.

Each group hits a hit-path and a miss/error status, checked against the
hand-authored fixtures (which are provably correct against the real algorithms).
api functions take an explicit graph_path, so no cache isolation is needed here.
"""
from pinchtab_webgraph import api


# --- graph_summary -----------------------------------------------------------

def test_graph_summary_interaction(sample_interaction_graph_path):
    out = api.graph_summary(sample_interaction_graph_path)
    assert out["graph_kind"] == "interaction"
    assert out["states"] == 5
    assert out["edges"] == 3
    assert out["triggers"] == 3
    assert out["meta"]["host"] == "example.test"


def test_graph_summary_link(sample_link_graph_path):
    out = api.graph_summary(sample_link_graph_path)
    assert out["graph_kind"] == "link"
    assert out["nodes"] == 9
    assert out["edges"] == 11


# --- howto -------------------------------------------------------------------

def test_howto_ok(sample_interaction_graph_path):
    out = api.howto(sample_interaction_graph_path, goal="create role")
    assert out["status"] == "ok"
    assert len(out["results"]) == 1
    r = out["results"][0]
    assert r["trigger_label"] == "Create Role"
    assert r["state_id"] == "s2"
    assert r["clicks"] == 3  # 2 edges from root + the trigger click
    assert r["form"]["fieldCount"] == 1
    assert out["candidates"] == []


def test_howto_reports_opens_at(sample_interaction_graph_path):
    out = api.howto(sample_interaction_graph_path, goal="add report")
    assert out["status"] == "ok"
    r = out["results"][0]
    assert r["clicks"] == 2  # 1 edge from root + trigger click
    assert r["opens_at"] == "https://example.test/reports/new"


def test_howto_unreachable(sample_interaction_graph_path):
    out = api.howto(sample_interaction_graph_path, goal="add widget")
    assert out["status"] == "unreachable"
    assert out["results"] == []
    assert "Add Widget" in out["candidates"]


def test_howto_no_match(sample_interaction_graph_path):
    out = api.howto(sample_interaction_graph_path, goal="create nonexistent")
    assert out["status"] == "no_match"
    assert out["results"] == []


def test_howto_match_regex(sample_interaction_graph_path):
    out = api.howto(sample_interaction_graph_path, match="Report")
    assert out["status"] == "ok"
    assert out["results"][0]["trigger_label"] == "Add Report"


def test_howto_no_goal_no_match_is_invalid(sample_interaction_graph_path):
    # neither goal nor match → up-front guard; must NOT broad-match every trigger
    out = api.howto(sample_interaction_graph_path)
    assert out["status"] == "invalid_args"
    assert out["results"] == []
    assert out["candidates"] == []


# --- howto: "Show Me How" tour steps (additive `tour` key) -------------------

def test_howto_tour_multi_step_shape(sample_interaction_graph_path):
    # "create role" routes s0->s1 (Team) ->s2 (Roles tab): 2 nav edges + trigger + form.
    out = api.howto(sample_interaction_graph_path, goal="create role")
    tour = out["results"][0]["tour"]
    assert [s["kind"] for s in tour] == ["nav", "nav", "trigger", "form"]
    # nav steps carry their edge selectors (in routing order).
    assert tour[0]["label"] == "Team"
    assert tour[0]["selector"] == "nav>a:nth-of-type(1)"
    assert tour[1]["label"] == "Roles tab"
    assert tour[1]["selector"] == "div>div>button:nth-of-type(2)"
    # the trigger step carries the trigger label but NO selector (crawler persists none).
    assert tour[2]["label"] == "Create Role"
    assert tour[2]["selector"] is None
    # the terminal form step is exactly {"kind":"form"} — no selector -> never auto-submits.
    assert tour[-1] == {"kind": "form"}


def test_howto_tour_single_nav_step_shape(sample_interaction_graph_path):
    # "add report" routes s0->s3 (Reports): 1 nav edge + trigger + form.
    out = api.howto(sample_interaction_graph_path, goal="add report")
    tour = out["results"][0]["tour"]
    assert [s["kind"] for s in tour] == ["nav", "trigger", "form"]
    # nav count == length of the routing path (1 here).
    navs = [s for s in tour if s["kind"] == "nav"]
    assert len(navs) == out["results"][0]["clicks"] - 1
    assert navs[0]["selector"] == "nav>a:nth-of-type(2)"
    assert tour[1]["selector"] is None  # trigger step
    assert tour[-1] == {"kind": "form"}


# --- howto: issue #11 acceptance (LinkedIn guest surface) --------------------
# Finding 1 — no confident FALSE positives; Finding 2 — form-bearing states route.

def test_howto_post_a_job_is_not_a_confident_false_match(linkedin_guest_graph_path):
    # "post a job" must NOT confidently resolve to the "Find a new job" nav (which only
    # matched via the create-VERB "new" + shared noun "job" and opens a 0-field page).
    out = api.howto(linkedin_guest_graph_path, goal="post a job")
    assert out["status"] == "no_match"
    assert out["results"] == []
    # the false match is surfaced, but flagged low-confidence, not returned as a result.
    assert [c["trigger_label"] for c in out["low_confidence"]] == ["Find a new job"]


def test_howto_create_job_alert_is_not_a_confident_false_match(linkedin_guest_graph_path):
    out = api.howto(linkedin_guest_graph_path, goal="create job alert")
    assert out["status"] == "no_match"
    assert all(r for r in out["results"]) is True and out["results"] == []


def test_howto_sign_in_routes_to_login(linkedin_guest_graph_path):
    # Finding 2: "sign in" routes to /login with an email + password form.
    out = api.howto(linkedin_guest_graph_path, goal="sign in")
    assert out["status"] == "ok"
    r = out["results"][0]
    assert r["trigger_label"] == "Sign in"
    assert r["opens_at"] == "https://www.linkedin.com/login"
    assert r["confidence"] == "high"
    types = {f["type"] for f in r["form"]["fields"]}
    assert "password" in types and r["form"]["fieldCount"] == 2


def test_howto_join_now_routes_to_signup(linkedin_guest_graph_path):
    # Finding 2: "join now" routes to /signup.
    out = api.howto(linkedin_guest_graph_path, goal="join now")
    assert out["status"] == "ok"
    r = out["results"][0]
    assert r["trigger_label"] == "Join now"
    assert r["opens_at"] == "https://www.linkedin.com/signup"


def test_howto_sign_in_does_not_return_find_a_new_job(linkedin_guest_graph_path):
    # regression for the reported false positive: "sign in" ≠ "Find a new job"
    out = api.howto(linkedin_guest_graph_path, goal="sign in")
    labels = [r["trigger_label"] for r in out["results"]]
    assert "Find a new job" not in labels


# --- howto: generality — the SAME logic on non-LinkedIn site archetypes ------
# If any of these regress, the fix has become app-specific. Nothing below is keyed
# on a brand: only form structure + generic EN/ES create-verbs.

def test_generic_saas_new_releases_nav_is_not_confident(saas_generic_graph_path):
    # a 0-field create-VERB nav ("New Releases") must not confidently answer "create release"
    out = api.howto(saas_generic_graph_path, goal="create release")
    assert out["status"] == "no_match"
    assert [c["trigger_label"] for c in out["low_confidence"]] == ["New Releases"]


def test_generic_saas_signup_form_routes(saas_generic_graph_path):
    # a form-bearing state with NO create-verb in its label is still reachable
    out = api.howto(saas_generic_graph_path, goal="sign up")
    assert out["status"] == "ok"
    r = out["results"][0]
    assert r["trigger_label"] == "Sign up"
    assert r["opens_at"].endswith("/register")
    assert "password" in {f["type"] for f in r["form"]["fields"]}


def test_generic_saas_contact_form_routes(saas_generic_graph_path):
    # "us" is a ≤2-char token and dropped, so this keys on "contact" alone
    out = api.howto(saas_generic_graph_path, goal="contact us")
    assert out["status"] == "ok"
    assert out["results"][0]["trigger_label"] == "Contact us"


def test_spanish_nueva_coleccion_nav_is_not_confident(spanish_app_graph_path):
    # ES create-verb "nueva" in a 0-field nav must not confidently answer "crear colección"
    out = api.howto(spanish_app_graph_path, goal="crear colección")
    assert out["status"] == "no_match"
    assert [c["trigger_label"] for c in out["low_confidence"]] == ["Nueva colección"]


def test_spanish_anadir_cliente_create_trigger_routes(spanish_app_graph_path):
    # ES create-verb "añadir" + noun → a real create form (2 fields) routes confidently
    out = api.howto(spanish_app_graph_path, goal="añadir cliente")
    assert out["status"] == "ok"
    r = out["results"][0]
    assert r["trigger_label"] == "Añadir cliente"
    assert r["form"]["fieldCount"] == 2


def test_spanish_iniciar_sesion_form_routes(spanish_app_graph_path):
    # form-bearing login whose label ("Iniciar sesión") carries NO create-verb
    out = api.howto(spanish_app_graph_path, goal="iniciar sesión")
    assert out["status"] == "ok"
    r = out["results"][0]
    assert r["trigger_label"] == "Iniciar sesión"
    assert r["opens_at"].endswith("/acceso")


# --- find_content ------------------------------------------------------------

def test_find_content_hit(sample_interaction_graph_path):
    out = api.find_content(sample_interaction_graph_path, "Alice")
    assert out["status"] == "ok"
    assert out["total_matches"] == 1
    assert out["views_matched"] == 1
    v = out["views"][0]
    assert v["view_label"] == "Team"
    assert v["reachable"] is True
    assert v["items"][0]["text"] == "Alice Martin"


def test_find_content_table(sample_interaction_graph_path):
    out = api.find_content(sample_interaction_graph_path, "Q1 Report")
    assert out["status"] == "ok"
    assert out["total_matches"] == 1
    assert out["views"][0]["view_label"] == "Reports"


def test_find_content_miss(sample_interaction_graph_path):
    out = api.find_content(sample_interaction_graph_path, "nothinghere")
    assert out["status"] == "no_match"
    assert out["total_matches"] == 0
    assert out["views"] == []


# --- list_content ------------------------------------------------------------

def test_list_content(sample_interaction_graph_path):
    out = api.list_content(sample_interaction_graph_path)
    assert out["status"] == "ok"
    labels = {v["view_label"] for v in out["views"]}
    assert labels == {"Team", "Reports"}
    reports = next(v for v in out["views"] if v["view_label"] == "Reports")
    assert reports["collections"][0]["kind"] == "table"
    assert reports["collections"][0]["count"] == 2


def test_list_content_empty(sample_link_graph_path):
    # a link graph has no `states`/`collections` → empty
    out = api.list_content(sample_link_graph_path)
    assert out["status"] == "empty"
    assert out["views"] == []


# --- find_content_hosts / list_content_hosts (cross-host) --------------------

def test_find_content_hosts_ranks_and_labels(sample_interaction_graph_path,
                                             second_host_graph_path):
    host_paths = [("example.test", str(sample_interaction_graph_path)),
                  ("shop.test", str(second_host_graph_path))]
    out = api.find_content_hosts(host_paths, "alice")
    assert out["status"] == "ok"
    # "alice" matches on BOTH hosts (Alice Martin / Alice Cooper).
    assert set(out["hosts_matched"]) == {"example.test", "shop.test"}
    assert out["total_matches"] == 2
    # every returned view is tagged with its origin host, and reachable ones sort first.
    assert all("host" in v for v in out["views"])
    assert all(v["reachable"] for v in out["views"])
    hosts = {v["host"] for v in out["views"]}
    assert hosts == {"example.test", "shop.test"}


def test_find_content_hosts_miss(sample_interaction_graph_path, second_host_graph_path):
    host_paths = [("example.test", str(sample_interaction_graph_path)),
                  ("shop.test", str(second_host_graph_path))]
    out = api.find_content_hosts(host_paths, "Widget Zeta")
    assert out["status"] == "ok"
    # "Widget Zeta" is unique to shop.test.
    assert out["hosts_matched"] == ["shop.test"]
    assert {v["host"] for v in out["views"]} == {"shop.test"}
    assert out["views"][0]["items"][0]["text"] == "Widget Zeta"


def test_find_content_hosts_no_match(sample_interaction_graph_path,
                                     second_host_graph_path):
    host_paths = [("example.test", str(sample_interaction_graph_path)),
                  ("shop.test", str(second_host_graph_path))]
    out = api.find_content_hosts(host_paths, "nothinghere")
    assert out["status"] == "no_match"
    assert out["hosts_matched"] == []
    assert out["views"] == []


def test_find_content_hosts_resilient_to_bad_cache(sample_interaction_graph_path,
                                                   tmp_path):
    bad = tmp_path / "garbage.json"
    bad.write_text("{ not json")
    host_paths = [("example.test", str(sample_interaction_graph_path)),
                  ("broken.test", str(bad))]
    out = api.find_content_hosts(host_paths, "alice")
    # the good host still answers; the bad one is recorded as an error, not a crash.
    assert out["status"] == "ok"
    assert out["hosts_matched"] == ["example.test"]
    broken = next(h for h in out["per_host"] if h["host"] == "broken.test")
    assert broken["status"] == "error"


def test_list_content_hosts(sample_interaction_graph_path, second_host_graph_path):
    host_paths = [("example.test", str(sample_interaction_graph_path)),
                  ("shop.test", str(second_host_graph_path))]
    out = api.list_content_hosts(host_paths)
    assert out["status"] == "ok"
    assert set(out["hosts_with_content"]) == {"example.test", "shop.test"}
    assert [h["host"] for h in out["hosts"]] == ["example.test", "shop.test"]
    shop = next(h for h in out["hosts"] if h["host"] == "shop.test")
    assert shop["views"][0]["view_label"] == "Customers"


def test_list_content_hosts_empty(sample_link_graph_path):
    # a single link graph has no collections → empty across the board.
    out = api.list_content_hosts([("links.test", str(sample_link_graph_path))])
    assert out["status"] == "empty"
    assert out["hosts_with_content"] == []


# --- list_forms --------------------------------------------------------------

def test_list_forms(sample_interaction_graph_path):
    out = api.list_forms(sample_interaction_graph_path)
    assert out["meta"]["host"] == "example.test"
    assert out["meta"]["triggers"] == 3
    labels = [f["label"] for f in out["forms"]]
    assert set(labels) == {"Create Role", "Add Report", "Add Widget"}
    # sorted by (state_url, label.lower()): /orphan < /reports < /team/roles
    assert labels == ["Add Widget", "Add Report", "Create Role"]
    cr = next(f for f in out["forms"] if f["label"] == "Create Role")
    assert cr["clicks"] == 3
    assert cr["field_count"] == 1


# --- link_paths --------------------------------------------------------------

def test_link_paths_shortest(sample_link_graph_path):
    out = api.link_paths(sample_link_graph_path, "home", "guide")
    assert out["status"] == "ok"
    assert out["shortest"]["clicks"] == 1  # home -> guide direct edge
    assert out["from"]["id"] == "home"
    assert out["to"]["id"] == "guide"


def test_link_paths_all(sample_link_graph_path):
    out = api.link_paths(sample_link_graph_path, "home", "guide", all=True)
    assert out["status"] == "ok"
    assert len(out["all_paths"]) == 2  # direct + via docs
    clicks = sorted(p["clicks"] for p in out["all_paths"])
    assert clicks == [1, 2]


def test_link_paths_structural_no_path(sample_link_graph_path):
    # every inbound edge to dashboard is a hub (glob) edge → structural has no path
    out = api.link_paths(sample_link_graph_path, "home", "dashboard", structural=True)
    assert out["status"] == "no_path"
    assert out["shortest"] is None


def test_link_paths_nonstructural_dashboard(sample_link_graph_path):
    out = api.link_paths(sample_link_graph_path, "home", "dashboard")
    assert out["status"] == "ok"
    assert out["shortest"]["clicks"] == 1


def test_link_paths_ambiguous_to(sample_link_graph_path):
    # "port" is a substring of both "reports" and "import-center"
    out = api.link_paths(sample_link_graph_path, "home", "port")
    assert out["status"] == "ambiguous_to"
    assert len(out["candidates"]) == 2


def test_link_paths_not_found_from(sample_link_graph_path):
    out = api.link_paths(sample_link_graph_path, "zzz", "guide")
    assert out["status"] == "not_found_from"
    assert out["candidates"] == []
