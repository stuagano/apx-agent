from server import tools


def teardown_function():
    tools.reset_runtime()


def test_web_research_active_by_default():
    tools.reset_runtime()
    fns = tools.active_tools()
    assert len(fns) == 1  # web_research is enabled out of the box
    assert fns[0].__name__


def test_fetch_web_page_returns_error_string_not_raises(monkeypatch):
    import urllib.error
    import urllib.request

    def _boom(*a, **k):
        raise urllib.error.HTTPError("http://x/missing", 404, "Not Found", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    out = tools.fetch_web_page("http://example.org/missing")
    # Must not raise — a tool that raises kills the whole agent turn. It returns
    # a readable error string the LLM can react to instead.
    assert isinstance(out, str)
    assert "error" in out.lower()
    assert "404" in out


def test_fetch_web_page_returns_error_string_on_url_error(monkeypatch):
    import urllib.error
    import urllib.request

    def _boom(*a, **k):
        raise urllib.error.URLError("name resolution failed")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    out = tools.fetch_web_page("http://nonexistent.invalid")
    assert isinstance(out, str)
    assert "error" in out.lower()


def test_fetch_web_page_surfaces_embedded_third_party_hosts(monkeypatch):
    import urllib.request

    html = (
        b"<html><body><h1>Support Gateway Homes</h1>"
        b'<iframe src="https://gatewayhomes.app.etapestry.com/donate"></iframe>'
        b'<script src="https://www.google-analytics.com/ga.js"></script>'
        b'<a href="/about">About us</a>'
        b"</body></html>"
    )

    class _FakeResp:
        def read(self, n=-1):
            return html

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _FakeResp())
    out = tools.fetch_web_page("https://www.gatewayhomes.org/donate")

    assert "Support Gateway Homes" in out  # visible text preserved
    assert "[embedded third-party services & links]" in out
    assert "gatewayhomes.app.etapestry.com" in out  # donation platform host surfaced
    assert "google-analytics.com" in out  # other 3rd-party surfaced
    assert (
        "gatewayhomes.org" not in out.split("[embedded")[1]
    )  # own host excluded from the list


def test_fetch_web_page_survives_malformed_embedded_url(monkeypatch):
    import urllib.request

    # A malformed URL token in the page would make urlparse raise ValueError.
    # fetch_web_page must still RETURN (a raising tool aborts the whole turn).
    html = b'<html><body>hi <a href="https://["></a> <script>x="https://["</script></body></html>'

    class _FakeResp:
        def read(self, n=-1):
            return html

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _FakeResp())
    out = tools.fetch_web_page("https://example.org/")  # must not raise
    assert isinstance(out, str)
    assert "[embedded third-party services & links]" in out


def test_load_default_skills_registers_nonprofit_discovery():
    tools.reset_runtime()
    tools.load_default_skills()
    skills = {s["name"]: s["description"] for s in tools.list_skills()}
    assert "nonprofit_discovery" in skills
    assert (
        "990" in skills["nonprofit_discovery"]
        or "ProPublica" in skills["nonprofit_discovery"]
    )
    # the content (returned when the agent calls the tool) carries the prescriptive steps
    fn = [f for f in tools.active_tools() if f.__name__ == "nonprofit_discovery"][0]
    body = fn()
    assert "ProPublica" in body
    assert "etapestry" in body.lower()


def test_author_skill_becomes_callable_returning_markdown():
    tools.set_skill(
        "food_safety", "Food safety compliance notes", "## Food Safety\nKeep temp logs."
    )
    fns = tools.active_tools()
    skill = [f for f in fns if f.__name__ == "food_safety"][0]
    assert skill() == "## Food Safety\nKeep temp logs."
    assert skill.__doc__ is not None
    assert "compliance" in skill.__doc__


def test_list_and_remove_skill():
    tools.set_skill("s1", "d", "c")
    assert any(s["name"] == "s1" for s in tools.list_skills())
    tools.remove_skill("s1")
    assert all(s["name"] != "s1" for s in tools.list_skills())
