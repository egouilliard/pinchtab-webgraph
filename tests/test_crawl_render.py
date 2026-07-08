"""Tests for render_html — the self-contained, offline Cytoscape viewer.

Pure-stdlib, browser-free. Like tests/test_crawl_extract.py these lean on
"provably correct against the real algorithm" string checks against the HTML
that render_html emits: the six libraries are inlined from pinchtab_webgraph/
vendor/ (no CDN), the fcose layout is tuned for speed, and the injected JSON
can't break out of its <script> tag.
"""
import os

from pinchtab_webgraph import crawl


# A small synthetic link graph in the exact shape Crawler.finish emits.
def _graph():
    return {
        "nodes": [
            {"id": "https://ex.test/", "url": "https://ex.test/",
             "title": "Home", "type": "page"},
            {"id": "https://ex.test/docs", "url": "https://ex.test/docs",
             "title": "Docs", "type": "page"},
            {"id": "state:1", "url": None, "title": "A modal", "type": "state"},
        ],
        "edges": [
            {"source": "https://ex.test/", "target": "https://ex.test/docs",
             "label": "Docs", "kind": "link"},
            {"source": "https://ex.test/docs", "target": "state:1",
             "label": "Open", "kind": "action", "skipped": False},
        ],
        "meta": {"host": "ex.test", "start": "https://ex.test/", "pages": 2,
                 "states": 1, "skipped": 0, "edges": 2, "interaction_depth": 1,
                 "elapsed_sec": 3.2},
    }


def test_render_returns_str_with_embedded_graph():
    html = crawl.render_html(_graph())
    assert isinstance(html, str)
    # a node url / id from the embedded graph is present in the output.
    assert "https://ex.test/docs" in html
    assert "A modal" in html
    # the sentinel was replaced (graph injected), not left as a literal null.
    assert "/*__GRAPH__*/null" not in html
    assert "const GRAPH =" in html


def test_no_cdn_origins():
    html = crawl.render_html(_graph())
    for origin in ("cdnjs.cloudflare.com", "cdn.jsdelivr.net", "unpkg.com"):
        assert origin not in html
    # no remote <script src="http..."> at all — truly offline.
    assert 'src="http' not in html


def test_libraries_inlined():
    html = crawl.render_html(_graph())
    # the ~785KB of vendored JS is present inline.
    assert len(html) > 780000
    # a cytoscape marker string proves the real lib is embedded.
    assert "cytoscape" in html
    # the placeholder was consumed.
    assert "<!--__VENDOR_JS__-->" not in html


def test_layout_tuned():
    html = crawl.render_html(_graph())
    assert "quality:'default'" in html
    assert "randomize:false" in html
    # the on-demand "High quality" preset keeps the expensive proof layout.
    assert "quality:'proof'" in html
    assert "fcose-proof" in html


def test_no_tag_breakout():
    html = crawl.render_html(_graph())
    # vendored JS escaped </ -> <\/, so no doubled closing tag can appear.
    assert "</script></script>" not in html
    # the doc is intact end-to-end and the GRAPH block survived.
    assert html.rstrip().endswith("</html>")
    assert "<script>const GRAPH" in html or "const GRAPH =" in html


def test_title_with_script_tag_cannot_break_out():
    g = _graph()
    g["nodes"][0]["title"] = "Evil </script><script>alert(1)</script>"
    html = crawl.render_html(g)
    # the raw breakout sequence must not survive in the injected JSON.
    assert "</script><script>alert(1)</script>" not in html
    assert "<\\/script>" in html


def test_vendor_files_present_and_nonempty():
    for name in crawl._VENDOR_FILES:
        path = os.path.join(crawl._VENDOR_DIR, name)
        assert os.path.exists(path), name
        assert os.path.getsize(path) > 3000, name
    assert len(crawl._VENDOR_FILES) == 6
