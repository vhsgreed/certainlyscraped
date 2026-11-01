"""Pagination loop tests (no network). Run: python3 -m pytest tests/ -q"""
import sys

sys.path.insert(0, ".")

from certainlyscraped.outcome import PageFetch
from certainlyscraped.paginate import follow_links, paginate


def _site(pages: dict):
    fetched = []

    def get_html(url):
        fetched.append(url)
        if url not in pages:
            return 404, ""
        return 200, pages[url]
    return get_html, fetched


def _next(href):
    return f'<html><body><a class="next" href="{href}">Next</a></body></html>'


CFG = {"max_pages": 10, "rate_limit_sec": 0,
       "paginate": {"selector": "a.next"}}


def test_relative_next_link_resolves_against_current_page():
    # Page 2 links "?page=3" relative to /cat/page/2/, not the start URL.
    pages = {
        "https://s/cat/": _next("page/2/"),
        "https://s/cat/page/2/": _next("?page=3"),
        "https://s/cat/page/2/?page=3": "<html>end</html>",
    }
    get_html, fetched = _site(pages)
    n = paginate(get_html, "https://s/cat/", CFG, lambda h: None)
    assert fetched == list(pages)
    assert n == 3


def test_stops_when_next_link_cycles_back():
    pages = {"https://s/a": _next("/b"), "https://s/b": _next("/a")}
    get_html, fetched = _site(pages)
    n = paginate(get_html, "https://s/a", CFG, lambda h: None)
    assert fetched == ["https://s/a", "https://s/b"]
    assert n == 2


def test_stops_on_error_status():
    get_html, fetched = _site({"https://s/a": _next("/missing")})
    n = paginate(get_html, "https://s/a", CFG, lambda h: None)
    assert fetched == ["https://s/a", "https://s/missing"]
    assert n == 1


def test_error_status_stop_is_logged():
    import io
    get_html, _ = _site({"https://s/a": _next("/missing")})
    log = io.StringIO()
    paginate(get_html, "https://s/a", CFG, lambda h: None, log=log)
    assert "[paginate] stopped: HTTP 404 on https://s/missing" in log.getvalue()


def test_normal_end_logs_nothing():
    import io
    get_html, _ = _site({"https://s/a": "<html>end</html>"})
    log = io.StringIO()
    paginate(get_html, "https://s/a", CFG, lambda h: None, log=log)
    assert log.getvalue() == ""


def test_follow_links_trailing_404_is_not_found():
    get_html, _ = _site({"https://s/a": _next("/missing")})
    seen = []

    def on_page(html, url):
        seen.append(url)
        return 1

    outcomes = follow_links(
        lambda u: PageFetch(*get_html(u)), "https://s/a", CFG, on_page)
    assert seen == ["https://s/a"]
    assert [o.outcome for o in outcomes] == ["ok", "not_found"]
    assert outcomes[1].status == 404
    assert outcomes[1].url == "https://s/missing"

URL_CFG = {"max_pages": 10, "rate_limit_sec": 0,
           "paginate": {"url": "https://s/list?page={page}"}}


def _rows(_html):
    return 20


def test_url_template_paginates_a_list_that_has_no_next_link():
    # Blocket renders no next-page anchor: the list is addressable by page
    # number and nothing else. Without this the walk stops at one page.
    pages = {"https://s/list": "a", "https://s/list?page=2": "b",
             "https://s/list?page=3": "c"}
    get_html, fetched = _site(pages)
    n = paginate(get_html, "https://s/list", URL_CFG, _rows)
    # It asks for the page after the last one, gets the 404 and stops: there is
    # no way to know a numbered list ended without asking once.
    assert fetched == [*pages, "https://s/list?page=4"]
    assert n == 3


def test_url_template_stops_at_max_pages():
    pages = {"https://s/list": "x"}
    pages.update({f"https://s/list?page={i}": "x" for i in range(2, 20)})
    get_html, fetched = _site(pages)
    n = paginate(get_html, "https://s/list", dict(URL_CFG, max_pages=3), _rows)
    assert n == 3 and len(fetched) == 3


def test_url_template_stops_when_a_page_yields_no_rows():
    # A page past the end comes back 200 with nothing on it: stop rather than
    # walk to max_pages against a site that has nothing left to give.
    pages = {"https://s/list": "a", "https://s/list?page=2": "b",
             "https://s/list?page=3": "c"}
    get_html, fetched = _site(pages)
    rows = {"a": 20, "b": 0}          # the callback sees the HTML, not the URL
    n = paginate(get_html, "https://s/list", URL_CFG, lambda h: rows.get(h, 0))
    assert fetched == ["https://s/list", "https://s/list?page=2"] and n == 2


def test_url_template_keeps_the_query_string_of_the_template():
    cfg = {"max_pages": 2, "rate_limit_sec": 0,
           "paginate": {"url": "/search?q=cykel&page={page}"}}
    pages = {"https://s/search?q=cykel": "a",
             "https://s/search?q=cykel&page=2": "b"}
    get_html, fetched = _site(pages)
    paginate(get_html, "https://s/search?q=cykel", cfg, _rows)
    assert fetched == list(pages)
