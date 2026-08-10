"""Re-capturing the browser renders is all-or-nothing.

``scripts/capture_webui_renders.py`` drives a browser over seven screens, and
any one of them can fail: the server dies, a route stops resolving, a selector
never settles. If each screenshot landed straight in ``docs/assets`` the run
would leave a mixed set - some screens from today's bundle, the rest from
whenever they were last taken.

That matters because of what happens next. The documented follow-up,
``bind_webui_renders.py --update``, preserves each render's prior provenance,
so the untouched ones keep the word ``captured`` while being rebound to a
bundle they were never captured from. The binding gate would go green on a
claim that is false, which is the one thing it exists to prevent.

So the capture stages into a scratch directory and publishes only once every
requested screen is in hand. These tests drive the real ``capture()`` with a
stubbed Playwright, so the property is checked without a browser.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "capture_webui_renders.py"

SCREENS = {"tasks": "/ui/tasks", "agents": "/ui/agents", "costs": "/ui/costs"}


@pytest.fixture
def capturer() -> Any:
    """Load scripts/capture_webui_renders.py without executing main()."""
    spec = importlib.util.spec_from_file_location("capture_webui_renders_under_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Page:
    """A browser page that writes plausible PNG bytes, and can be made to fail."""

    def __init__(self, fail_on: str | None) -> None:
        self._fail_on = fail_on
        self.shots: list[Path] = []

    def goto(self, url: str, **_: object) -> None:
        pass

    def wait_for_timeout(self, _ms: int) -> None:
        pass

    def screenshot(self, path: str) -> None:
        target = Path(path)
        if self._fail_on and target.name == f"webui-{self._fail_on}.png":
            raise RuntimeError("the page never settled")
        target.write_bytes(b"\x89PNG\r\n\x1a\n" + target.name.encode())
        self.shots.append(target)


class _Browser:
    def __init__(self, page: _Page) -> None:
        self._page = page
        self.closed = False

    def new_page(self, **_: object) -> _Page:
        return self._page

    def close(self) -> None:
        self.closed = True


def _install_stub_playwright(monkeypatch: pytest.MonkeyPatch, page: _Page) -> _Browser:
    """Make ``from playwright.sync_api import sync_playwright`` resolve to a stub.

    ``capture()`` imports Playwright inside the function, so seeding
    ``sys.modules`` is enough - and it keeps a browser out of the unit suite,
    which is why Playwright is not a project dependency in the first place.
    """
    browser = _Browser(page)

    class _Chromium:
        @staticmethod
        def launch() -> _Browser:
            return browser

    class _Playwright:
        chromium = _Chromium()

    @contextlib.contextmanager
    def sync_playwright() -> Any:
        yield _Playwright()

    module = type(sys)("playwright.sync_api")
    module.sync_playwright = sync_playwright  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright", type(sys)("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", module)
    return browser


def _digest(directory: Path) -> dict[str, str]:
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(directory.glob("webui-*.png"))}


@pytest.fixture
def assets(capturer: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An asset directory holding renders from some earlier, older capture."""
    directory = tmp_path / "assets"
    directory.mkdir()
    for name in SCREENS:
        (directory / f"webui-{name}.png").write_bytes(b"an older render of " + name.encode())
    monkeypatch.setattr(capturer, "ASSET_DIR", directory)
    return directory


def test_a_complete_run_replaces_every_requested_render(capturer: Any, assets: Path, monkeypatch: Any) -> None:
    browser = _install_stub_playwright(monkeypatch, _Page(fail_on=None))

    written = capturer.capture(SCREENS, "http://127.0.0.1:1234", "token")

    assert [path.name for path in written] == [f"webui-{name}.png" for name in SCREENS]
    for name in SCREENS:
        assert (assets / f"webui-{name}.png").read_bytes().startswith(b"\x89PNG")
    assert browser.closed, "the browser must be closed even on the happy path"


def test_a_run_that_fails_part_way_leaves_every_render_untouched(capturer: Any, assets: Path, monkeypatch: Any) -> None:
    """The property that keeps ``captured`` provenance honest.

    ``tasks`` captures fine and ``agents`` blows up. Publishing the one that
    worked would leave a set that ``bind_webui_renders.py --update`` rebinds
    wholesale, marking two screens as captured from a bundle they never saw.
    """
    before = _digest(assets)
    _install_stub_playwright(monkeypatch, _Page(fail_on="agents"))

    with pytest.raises(RuntimeError, match="never settled"):
        capturer.capture(SCREENS, "http://127.0.0.1:1234", "token")

    assert _digest(assets) == before, "a failed capture must not publish the screens that did work"


def test_the_staging_directory_never_survives_the_run(capturer: Any, assets: Path, monkeypatch: Any) -> None:
    """It lives inside docs/assets, so a leftover would be a stray in the next commit."""
    _install_stub_playwright(monkeypatch, _Page(fail_on="agents"))
    with pytest.raises(RuntimeError):
        capturer.capture(SCREENS, "http://127.0.0.1:1234", "token")
    assert [path.name for path in assets.iterdir() if path.is_dir()] == [], "staging survived a failed run"

    _install_stub_playwright(monkeypatch, _Page(fail_on=None))
    capturer.capture(SCREENS, "http://127.0.0.1:1234", "token")
    assert [path.name for path in assets.iterdir() if path.is_dir()] == [], "staging survived a clean run"


def test_publish_moves_only_the_screens_it_was_given(capturer: Any, assets: Path, tmp_path: Path) -> None:
    """A partial re-capture (``capture_webui_renders.py tasks``) is legitimate.

    Publishing the whole staging directory would be wrong in a different way:
    it would only ever be right when every screen was requested.
    """
    staging = tmp_path / "staging"
    staging.mkdir()
    for name in SCREENS:
        (staging / f"webui-{name}.png").write_bytes(b"fresh " + name.encode())
    untouched = (assets / "webui-costs.png").read_bytes()

    published = capturer.publish(staging, ["tasks"])

    assert [path.name for path in published] == ["webui-tasks.png"]
    assert (assets / "webui-tasks.png").read_bytes() == b"fresh tasks"
    assert (assets / "webui-costs.png").read_bytes() == untouched


def test_every_screen_the_script_owns_has_a_committed_render(capturer: Any) -> None:
    """The screen table is also the claim about which renders this script owns.

    A screen added here without a committed render, or renamed out from under
    one, would silently capture into a file nothing publishes or reads.
    """
    committed = {path.name for path in (REPO_ROOT / "docs" / "assets").glob("webui-*.png")}

    assert {f"webui-{name}.png" for name in capturer.SCREENS} <= committed
