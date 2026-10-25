"""Unit tests for the PWA helper module (#1218).

Covers manifest generation, service worker source invariants, icon PNG
encoding, auth token + diceware passphrase issuance, and the QR
onboarding URL composer.
"""

from __future__ import annotations

import io
import json
import re
import zlib

import pytest

from bernstein.gui import pwa
from bernstein.gui.pwa import (
    APP_NAME,
    AUTH_TOKEN_BYTES,
    DICEWARE_WORDS,
    OFFLINE_HTML,
    PASSPHRASE_WORDS,
    SCOPE,
    SERVICE_WORKER_JS,
    START_URL,
    AuthIssue,
    build_manifest,
    compose_onboarding_url,
    new_auth_issue,
    new_auth_token,
    new_passphrase,
    render_icon_png,
)

# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def test_manifest_has_required_fields() -> None:
    m = build_manifest()
    for key in ("name", "short_name", "start_url", "scope", "display", "icons", "theme_color", "background_color"):
        assert key in m, f"manifest missing key {key!r}"


def test_manifest_display_is_standalone() -> None:
    assert build_manifest()["display"] == "standalone"


def test_manifest_start_url_matches_scope_root() -> None:
    m = build_manifest()
    assert str(m["start_url"]).startswith(str(m["scope"]))


def test_manifest_two_icons_192_and_512() -> None:
    icons = build_manifest()["icons"]
    sizes = sorted(icon["sizes"] for icon in icons)
    assert sizes == ["192x192", "512x512"]


def test_manifest_icons_use_png_media_type() -> None:
    for icon in build_manifest()["icons"]:
        assert icon["type"] == "image/png"


def test_manifest_icon_purpose_includes_maskable() -> None:
    for icon in build_manifest()["icons"]:
        assert "maskable" in icon["purpose"]


def test_manifest_default_app_name_matches_constant() -> None:
    assert build_manifest()["name"] == APP_NAME


def test_manifest_custom_args_propagate() -> None:
    m = build_manifest(name="X", short_name="X", theme_color="#abc", background_color="#abc")
    assert m["name"] == "X"
    assert m["short_name"] == "X"
    assert m["theme_color"] == "#abc"
    assert m["background_color"] == "#abc"


def test_manifest_is_json_serialisable() -> None:
    raw = json.dumps(build_manifest())
    assert json.loads(raw) == build_manifest()


def test_manifest_is_deterministic() -> None:
    """Same args -> identical dict (snapshot-safe)."""
    a = build_manifest()
    b = build_manifest()
    assert a == b


def test_manifest_scope_and_start_url_constants() -> None:
    m = build_manifest()
    assert m["scope"] == SCOPE
    assert m["start_url"] == START_URL


def test_manifest_snapshot_default(snapshot: object) -> None:
    # syrupy snapshot for the canonical manifest output
    raw = json.dumps(build_manifest(), indent=2, sort_keys=True)
    assert raw == snapshot  # type: ignore[comparison-overlap]


# ---------------------------------------------------------------------------
# Service worker source
# ---------------------------------------------------------------------------


def test_sw_contains_install_handler() -> None:
    assert "addEventListener('install'" in SERVICE_WORKER_JS


def test_sw_contains_activate_handler() -> None:
    assert "addEventListener('activate'" in SERVICE_WORKER_JS


def test_sw_contains_fetch_handler() -> None:
    assert "addEventListener('fetch'" in SERVICE_WORKER_JS


def test_sw_caches_shell_assets() -> None:
    for asset in ("/ui/index.html", "/ui/offline.html", "/ui/manifest.webmanifest"):
        assert asset in SERVICE_WORKER_JS


def test_sw_handles_runtime_api_paths() -> None:
    for path in ("/api/projects", "/api/cost"):
        assert path in SERVICE_WORKER_JS


def test_sw_serves_offline_page_on_navigate_failure() -> None:
    assert "offline.html" in SERVICE_WORKER_JS


def test_sw_has_versioned_cache_name() -> None:
    assert re.search(r"const\s+VERSION\s*=\s*'bernstein-pwa-v\d+'", SERVICE_WORKER_JS)


def test_sw_skip_method_invokes_skipwaiting_and_claim() -> None:
    assert "skipWaiting" in SERVICE_WORKER_JS
    assert "clients.claim" in SERVICE_WORKER_JS


def test_offline_html_renders_main_card() -> None:
    assert "<main" in OFFLINE_HTML
    assert "Offline" in OFFLINE_HTML


def test_offline_html_is_self_contained() -> None:
    """No external CSS/JS imports - works inside the SW cache."""
    assert "src=" not in OFFLINE_HTML.split("<style", 1)[0]
    assert "<link" not in OFFLINE_HTML


# ---------------------------------------------------------------------------
# Icon PNG generator
# ---------------------------------------------------------------------------


_PNG_SIG = b"\x89PNG\r\n\x1a\n"


def test_icon_192_starts_with_png_signature() -> None:
    assert render_icon_png(192).startswith(_PNG_SIG)


def test_icon_512_starts_with_png_signature() -> None:
    assert render_icon_png(512).startswith(_PNG_SIG)


def test_icon_ihdr_records_correct_dimensions() -> None:
    blob = render_icon_png(64)
    # IHDR width/height are at byte offsets 16..24 (8 sig + 4 length + 4 type)
    width = int.from_bytes(blob[16:20], "big")
    height = int.from_bytes(blob[20:24], "big")
    assert width == 64
    assert height == 64


def test_icon_chunks_have_iend_terminator() -> None:
    assert render_icon_png(32).endswith(b"IEND\xaeB`\x82")


def test_icon_idat_decompresses_to_expected_size() -> None:
    size = 32
    blob = render_icon_png(size)
    # find the IDAT chunk: scan for the b"IDAT" tag after sig.
    idx = blob.index(b"IDAT")
    length = int.from_bytes(blob[idx - 4 : idx], "big")
    payload = blob[idx + 4 : idx + 4 + length]
    raw = zlib.decompress(payload)
    # 1 filter byte + size*3 RGB bytes per row, size rows.
    assert len(raw) == size * (1 + size * 3)


def test_icon_rejects_zero_size() -> None:
    with pytest.raises(ValueError):
        render_icon_png(0)


def test_icon_rejects_negative_size() -> None:
    with pytest.raises(ValueError):
        render_icon_png(-1)


def test_icon_default_colours_render_dark_background() -> None:
    blob = render_icon_png(8)
    # decode the first pixel after the filter byte; it should equal the bg
    # colour (top-left corner is outside the hexagon for size 8).
    idx = blob.index(b"IDAT")
    length = int.from_bytes(blob[idx - 4 : idx], "big")
    raw = zlib.decompress(blob[idx + 4 : idx + 4 + length])
    # row 0: filter byte at raw[0], pixel 0 at raw[1:4]
    r, g, b = raw[1], raw[2], raw[3]
    assert (r, g, b) == (19, 19, 15)  # the dark ``bg`` design token, #13130F


def test_icon_custom_background_colour_applied() -> None:
    blob = render_icon_png(8, bg=(200, 50, 0))
    idx = blob.index(b"IDAT")
    length = int.from_bytes(blob[idx - 4 : idx], "big")
    raw = zlib.decompress(blob[idx + 4 : idx + 4 + length])
    r, g, b = raw[1], raw[2], raw[3]
    assert (r, g, b) == (200, 50, 0)


def test_icon_decodes_via_pillow_when_available() -> None:
    # Optional smoke test: if pillow is installed, the icon must round-trip.
    PIL = pytest.importorskip("PIL.Image")
    img = PIL.open(io.BytesIO(render_icon_png(48)))
    assert img.size == (48, 48)


# ---------------------------------------------------------------------------
# Auth token + passphrase
# ---------------------------------------------------------------------------


def test_new_auth_token_default_length_is_url_safe() -> None:
    tok = new_auth_token()
    assert re.fullmatch(r"[A-Za-z0-9_-]+", tok) is not None


def test_new_auth_token_default_length_matches_constant() -> None:
    tok = new_auth_token()
    # token_urlsafe(32) -> 43 chars (ceil(32*4/3) = 43, no padding).
    expected_len = (AUTH_TOKEN_BYTES * 4 + 2) // 3
    assert len(tok) == expected_len


def test_new_auth_token_two_tokens_differ() -> None:
    assert new_auth_token() != new_auth_token()


def test_new_auth_token_rejects_zero_bytes() -> None:
    with pytest.raises(ValueError):
        new_auth_token(nbytes=0)


def test_new_auth_token_rejects_negative_bytes() -> None:
    with pytest.raises(ValueError):
        new_auth_token(nbytes=-1)


def test_new_auth_token_custom_length() -> None:
    tok = new_auth_token(nbytes=8)
    assert len(tok) >= 8


def test_new_passphrase_default_word_count() -> None:
    phrase = new_passphrase()
    assert phrase.count("-") == PASSPHRASE_WORDS - 1


def test_new_passphrase_words_are_lowercase() -> None:
    phrase = new_passphrase()
    assert phrase == phrase.lower()


def test_new_passphrase_all_words_from_wordlist() -> None:
    phrase = new_passphrase()
    for word in phrase.split("-"):
        assert word in DICEWARE_WORDS, f"unknown word: {word!r}"


def test_new_passphrase_rejects_zero_words() -> None:
    with pytest.raises(ValueError):
        new_passphrase(words=0)


def test_new_passphrase_rejects_empty_wordlist() -> None:
    with pytest.raises(ValueError):
        new_passphrase(wordlist=())


def test_new_passphrase_custom_word_count() -> None:
    phrase = new_passphrase(words=3)
    assert phrase.count("-") == 2


def test_diceware_wordlist_has_no_duplicates() -> None:
    """Probability of duplicates would lower the effective entropy."""
    assert len(DICEWARE_WORDS) == len(set(DICEWARE_WORDS))


def test_diceware_wordlist_is_lowercase() -> None:
    for word in DICEWARE_WORDS:
        assert word == word.lower(), f"non-lowercase word: {word!r}"


def test_diceware_wordlist_has_no_separator_chars() -> None:
    """Dashes inside words would break parsing of the joined output."""
    for word in DICEWARE_WORDS:
        assert "\u2014" not in word
        assert " " not in word


def test_new_auth_issue_pairs_token_and_passphrase() -> None:
    issue = new_auth_issue()
    assert isinstance(issue, AuthIssue)
    assert issue.token
    assert issue.passphrase


def test_new_auth_issue_is_frozen_dataclass() -> None:
    issue = new_auth_issue()
    with pytest.raises((AttributeError, Exception)):
        issue.token = "x"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Onboarding URL composer
# ---------------------------------------------------------------------------


def test_compose_onboarding_url_drops_trailing_slash() -> None:
    url = compose_onboarding_url("https://x.example.com/", "abc")
    assert url == "https://x.example.com/ui/#t=abc"


def test_compose_onboarding_url_preserves_token_fragment() -> None:
    url = compose_onboarding_url("https://x.example.com", "TOKEN-VALUE_42")
    assert url.endswith("#t=TOKEN-VALUE_42")


def test_compose_onboarding_url_passes_through_host_path() -> None:
    url = compose_onboarding_url("https://x.example.com", "tok")
    assert "/ui/" in url


def test_compose_onboarding_url_works_with_long_token() -> None:
    long_token = "a" * 256
    url = compose_onboarding_url("https://x.example.com", long_token)
    assert long_token in url


# ---------------------------------------------------------------------------
# Constants / sanity
# ---------------------------------------------------------------------------


def test_diceware_wordlist_size_is_power_of_two_friendly() -> None:
    # 256 entries gives a clean 8 bits of entropy per word; the embedded
    # list should not silently shrink under maintenance.
    assert len(DICEWARE_WORDS) >= 256


def test_module_re_exports_public_surface() -> None:
    """Smoke-check that the helpers stay importable as documented."""
    assert hasattr(pwa, "build_manifest")
    assert hasattr(pwa, "SERVICE_WORKER_JS")
    assert hasattr(pwa, "OFFLINE_HTML")
    assert hasattr(pwa, "render_icon_png")
    assert hasattr(pwa, "new_auth_token")
    assert hasattr(pwa, "new_passphrase")
    assert hasattr(pwa, "new_auth_issue")
    assert hasattr(pwa, "compose_onboarding_url")


def test_offline_html_doctype_first_byte() -> None:
    assert OFFLINE_HTML.startswith("<!doctype html>")


def test_offline_html_has_viewport_meta() -> None:
    assert "viewport" in OFFLINE_HTML
