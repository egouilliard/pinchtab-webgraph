"""Tests for the file-upload affordance extraction added to crawl.py / recipe.py.

Two flavours, both browser-free:
  - STRUCTURAL: assert the injected JS-string constants carry the new capabilities
    (file-input + ondrop selectors, upload/accept action metadata, FORM_JS accept).
    The repo already leans on these "provably correct against the real algorithm"
    string checks — the JS is a raw-string constant, so an import + substring assert
    is the guard (no jsdom, pure stdlib).
  - BEHAVIORAL: drive Crawler.explore_state with a hand-built upload action and a
    fully-recorded (no-browser) instance, proving an upload is recorded as an
    "upload" node via a skipped action edge and that the file input is NEVER clicked.
"""
import types

from pinchtab_webgraph import crawl, recipe


# --- STRUCTURAL: the injected JS carries the new capabilities -----------------

def test_extract_js_selects_file_inputs_and_ondrop():
    assert 'input[type="file"]' in crawl.EXTRACT_JS
    assert '[ondrop]' in crawl.EXTRACT_JS


def test_extract_js_emits_upload_and_accept_metadata():
    assert 'upload:' in crawl.EXTRACT_JS
    assert 'accept:' in crawl.EXTRACT_JS


def test_form_js_captures_accept():
    # the file-input branch reads the accept attribute into the field record.
    assert 'accept' in recipe.FORM_JS


def test_extract_js_classifies_download_links_and_buttons():
    # file-extension / download-attr / blob-data detection + a download-verb regex.
    assert 'DL_EXT' in crawl.EXTRACT_JS
    assert 'DL_VERB' in crawl.EXTRACT_JS
    assert 'download:' in crawl.EXTRACT_JS
    assert 'dlKind:' in crawl.EXTRACT_JS
    assert 'dlHref:' in crawl.EXTRACT_JS


# --- BEHAVIORAL: explore_state records the upload without ever clicking it -----

def _args():
    """A fully-populated args namespace for Crawler.__init__ + explore_state."""
    return types.SimpleNamespace(
        url="https://example.test/upload",
        server=None,
        strip_tracking=True,
        include_subdomains=False,
        max_actions=2000,
        max_pages=60,
        interaction_depth=1,
        max_actions_per_state=25,
        nav_only=False,
        allow_destructive=False,
        skip_writes=False,
        quiet=True,
        delay=0,
        nav_timeout=60,
        render_timeout=10000,
        auth_path=None,
        relogin_cmd=None,
    )


def test_explore_state_records_upload_without_clicking():
    c = crawl.Crawler(_args())

    clicked = []
    # No real browser: the recorder must show the file input is NEVER clicked
    # (an upload action hits `continue` BEFORE any click).
    c.click = lambda selector: clicked.append(selector) or True
    c.extract = lambda: (_ for _ in ()).throw(AssertionError("extract must not run"))
    c.nav = lambda url: (_ for _ in ()).throw(AssertionError("nav must not run"))
    c.settle = lambda: None
    c.reset_to = lambda *a, **k: True

    entry_url = c.start
    node_id = c.page_id(entry_url)
    c.add_node(node_id, url=entry_url, title="Upload page", type="page")

    info = {
        "url": entry_url,
        "title": "Upload page",
        "links": [],
        "actions": [
            {"selector": "#file", "text": "", "tag": "input", "nav": False,
             "bulk": False, "upload": True, "accept": ".pdf,.docx"},
        ],
    }
    c.explore_state(node_id, entry_url, [], info, depth=0, queue=[])

    uploads = [n for n in c.nodes.values() if n["type"] == "upload"]
    assert len(uploads) == 1
    up = uploads[0]
    assert up["accept"] == ".pdf,.docx"
    assert ".pdf,.docx" in up["title"]

    edges = [e for e in c.edges if e["target"] == up["id"]]
    assert len(edges) == 1
    assert edges[0]["source"] == node_id
    assert edges[0]["skipped"] is True

    # the file input was recorded, never clicked — read-only guarantee.
    assert "#file" not in clicked
    assert clicked == []


def test_explore_state_records_download_link_without_navigating():
    c = crawl.Crawler(_args())
    c.click = lambda selector: (_ for _ in ()).throw(AssertionError("must not click"))
    c.extract = lambda: (_ for _ in ()).throw(AssertionError("extract must not run"))
    c.nav = lambda url: (_ for _ in ()).throw(AssertionError("nav must not run"))

    entry_url = c.start
    node_id = c.page_id(entry_url)
    c.add_node(node_id, url=entry_url, title="Docs", type="page")

    info = {
        "url": entry_url, "title": "Docs",
        # a direct-download link (flagged by EXTRACT_JS) must NOT be crawled as a page.
        "links": [{"href": "https://example.test/files/report.pdf", "text": "Download report",
                   "download": True, "dlKind": "direct"}],
        "actions": [],
    }
    c.explore_state(node_id, entry_url, [], info, depth=0, queue=[])

    dls = [n for n in c.nodes.values() if n["type"] == "download"]
    assert len(dls) == 1
    assert dls[0]["dlHref"] == "https://example.test/files/report.pdf"
    assert dls[0]["dlKind"] == "direct"
    # the file URL was NOT added as a crawlable page node.
    assert not any(n.get("url", "").endswith("report.pdf") for n in c.nodes.values())
    edge = [e for e in c.edges if e["target"] == dls[0]["id"]][0]
    assert edge["skipped"] is True


def test_explore_state_records_js_download_button_without_clicking():
    c = crawl.Crawler(_args())
    clicked = []
    c.click = lambda selector: clicked.append(selector) or True
    c.extract = lambda: (_ for _ in ()).throw(AssertionError("extract must not run"))
    c.reset_to = lambda *a, **k: True
    c.settle = lambda: None

    entry_url = c.start
    node_id = c.page_id(entry_url)
    c.add_node(node_id, url=entry_url, title="Report", type="page")

    info = {
        "url": entry_url, "title": "Report",
        "links": [],
        "actions": [
            {"selector": "#export", "text": "Export CSV", "tag": "button", "nav": False,
             "bulk": False, "upload": False, "accept": None,
             "download": True, "dlKind": "js", "dlHref": None},
        ],
    }
    c.explore_state(node_id, entry_url, [], info, depth=0, queue=[])

    dls = [n for n in c.nodes.values() if n["type"] == "download"]
    assert len(dls) == 1
    assert dls[0]["dlKind"] == "js"
    assert dls[0]["selector"] == "#export"
    assert clicked == []  # JS download button recorded, never clicked
