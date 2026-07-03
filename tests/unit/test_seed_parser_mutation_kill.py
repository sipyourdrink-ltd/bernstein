"""Targeted tests that kill mutmut survivors in seed_parser.py.

Each test pins one or more invariants identified by
``scripts/mutmut_critical.py --only config_seed_parser``. The harness's
budget is exhausted before any mutations are killed by the baseline, so
this file establishes the foundation for the kill-rate gate.
"""

from __future__ import annotations

import pytest

from bernstein.core.config.seed_config import (
    CORSConfig,
    DashboardAuthConfig,
    NetworkConfig,
    RateLimitBucketConfig,
    SeedError,
)
from bernstein.core.config.seed_parser import (
    _expand_env_value,  # pyright: ignore[reportPrivateUsage]
    _parse_budget,  # pyright: ignore[reportPrivateUsage]
    _parse_cors_config,  # pyright: ignore[reportPrivateUsage]
    _parse_dashboard_auth,  # pyright: ignore[reportPrivateUsage]
    _parse_metric_entry,  # pyright: ignore[reportPrivateUsage]
    _parse_metrics,  # pyright: ignore[reportPrivateUsage]
    _parse_network_config,  # pyright: ignore[reportPrivateUsage]
    _parse_rate_limit_bucket,  # pyright: ignore[reportPrivateUsage]
    _parse_rate_limit_config,  # pyright: ignore[reportPrivateUsage]
    _parse_role_model_policy,  # pyright: ignore[reportPrivateUsage]
    _parse_single_role_policy,  # pyright: ignore[reportPrivateUsage]
    _parse_string_list,  # pyright: ignore[reportPrivateUsage]
    _parse_team,  # pyright: ignore[reportPrivateUsage]
    _parse_tenants,  # pyright: ignore[reportPrivateUsage]
    _require_bool,  # pyright: ignore[reportPrivateUsage]
    _require_positive_int,  # pyright: ignore[reportPrivateUsage]
    _require_positive_number,  # pyright: ignore[reportPrivateUsage]
    _require_str,  # pyright: ignore[reportPrivateUsage]
    _validate_cors_origin,  # pyright: ignore[reportPrivateUsage]
    _validate_openclaw_enabled,  # pyright: ignore[reportPrivateUsage]
)

# ---------------------------------------------------------------------------
# Line 118: f-string for invalid-budget error contains the literal "or".
# Mutant 'or' -> 'and'. Pin the error message wording.
# ---------------------------------------------------------------------------


def test_parse_budget_invalid_string_uses_or_in_error() -> None:
    """The 'Invalid budget format' error literally says ''$N' or a number'."""
    with pytest.raises(SeedError) as exc:
        _parse_budget("twenty dollars")
    msg = str(exc.value)
    assert "Invalid budget format" in msg
    # The literal 'or' separator describes the accepted forms.
    assert "'$N' or a number" in msg


# ---------------------------------------------------------------------------
# Line 133: `if raw is None or raw == "auto"`. Two mutants:
#   - 'or' -> 'and': only None-AND-equals-auto matches (always False).
#   - '==' -> '!=': inverts the auto-match.
# Cover both with direct tests on _parse_team.
# ---------------------------------------------------------------------------


def test_parse_team_none_returns_auto() -> None:
    """None resolves to the literal 'auto'."""
    assert _parse_team(None) == "auto"


def test_parse_team_explicit_auto_string_returns_auto() -> None:
    """The literal string 'auto' returns 'auto' (kills '==' -> '!=')."""
    assert _parse_team("auto") == "auto"


def test_parse_team_non_auto_string_rejected() -> None:
    """A non-auto string must NOT be treated as 'auto' (kills '==' inversion)."""
    with pytest.raises(SeedError):
        _parse_team("manual")


# ---------------------------------------------------------------------------
# Line 137: `if len(items) == 0`. Three mutants:
#   - '==' -> '!=': inverts empty check.
#   - ' 0' -> ' 1': single-item lists are treated as empty.
#   - 'len(' -> '0 * len(': always-zero -> always-empty.
# ---------------------------------------------------------------------------


def test_parse_team_empty_list_returns_auto() -> None:
    """An empty list collapses to 'auto'."""
    assert _parse_team([]) == "auto"


def test_parse_team_single_element_list_preserved() -> None:
    """A list with exactly one role string is preserved as-is (not collapsed)."""
    # If ' 0' is mutated to ' 1', len==1 would also return 'auto'.
    # If 'len(' is mutated to '0 * len(', any list collapses to 'auto'.
    result = _parse_team(["backend"])
    assert result == ["backend"]


def test_parse_team_two_element_list_preserved() -> None:
    """Multi-element lists are preserved."""
    result = _parse_team(["backend", "qa"])
    assert result == ["backend", "qa"]


def test_parse_team_list_with_non_string_rejected() -> None:
    """A list containing non-string items raises SeedError."""
    with pytest.raises(SeedError):
        _parse_team(["backend", 42])


# ---------------------------------------------------------------------------
# Line 142: invalid-team error message contains literal "or".
# ---------------------------------------------------------------------------


def test_parse_team_invalid_type_error_uses_or() -> None:
    """The 'team must be ...' error reads ''auto' or a list of role names'."""
    with pytest.raises(SeedError) as exc:
        _parse_team(42)
    assert "'auto' or a list of role names" in str(exc.value)


# ---------------------------------------------------------------------------
# Lines 180/185/189/193/200: `if not isinstance(...)` guards in
# _parse_metric_entry. Each test passes the wrong type and expects
# SeedError.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("entry", "match"),
    [
        ("not-a-dict", "must be a mapping"),
        ({"formula": 123}, "formula must be a non-empty string"),
        ({"formula": "   "}, "formula must be a non-empty string"),
        ({"formula": "x", "unit": 5}, "unit must be a string"),
        ({"formula": "x", "description": []}, "description must be a string"),
        ({"formula": "x", "alert_above": "x"}, "alert_above must be a number"),
        ({"formula": "x", "alert_below": "x"}, "alert_below must be a number"),
    ],
)
def test_parse_metric_entry_rejects_invalid(entry: object, match: str) -> None:
    with pytest.raises(SeedError, match=match):
        _parse_metric_entry("foo", entry)


def test_parse_metric_entry_valid_round_trip() -> None:
    """A fully populated metric entry parses to a MetricSchema."""
    schema = _parse_metric_entry(
        "code_per_dollar",
        {
            "formula": "lines / cost",
            "unit": "lines/$",
            "description": "Code produced per dollar",
            "alert_above": 100.0,
            "alert_below": 1.5,
        },
    )
    assert schema.formula == "lines / cost"
    assert schema.alert_above == 100.0
    assert schema.alert_below == 1.5


# ---------------------------------------------------------------------------
# Lines 239/245: _parse_metrics rejects non-dict / non-string keys.
# ---------------------------------------------------------------------------


def test_parse_metrics_non_mapping_rejected() -> None:
    with pytest.raises(SeedError, match="metrics must be a mapping"):
        _parse_metrics("not-a-mapping")


def test_parse_metrics_empty_key_rejected() -> None:
    """Whitespace-only metric name is rejected (kills 'or'->'and')."""
    with pytest.raises(SeedError, match="metrics keys must be non-empty strings"):
        _parse_metrics({"  ": {"formula": "x"}})


def test_parse_metrics_non_string_key_rejected() -> None:
    """A non-string key raises SeedError (kills 'not isinstance' -> 'isinstance' drop).

    The mutation flips ``not isinstance(name, str)`` to ``isinstance(name, str)``.
    With the mutation, an int key would hit ``42.strip()`` and raise AttributeError
    instead of the proper SeedError - so pytest.raises(SeedError, ...) fails.
    """
    with pytest.raises(SeedError, match="metrics keys must be non-empty strings"):
        _parse_metrics({42: {"formula": "x"}})


def test_parse_metrics_none_returns_empty_dict() -> None:
    assert _parse_metrics(None) == {}


# ---------------------------------------------------------------------------
# Line 266: _parse_network_config rejects non-dict.
# Line 271: ipaddress.ip_network(... strict=False). Mutant flips False to
# True; with strict=True a CIDR with host bits set raises ValueError.
# ---------------------------------------------------------------------------


def test_parse_network_config_non_dict_rejected() -> None:
    with pytest.raises(SeedError, match="network must be a mapping"):
        _parse_network_config(42)


def test_parse_network_config_accepts_non_canonical_cidr() -> None:
    """A CIDR with host bits set (10.0.0.5/24) is accepted via strict=False."""
    # Under strict=True this would raise ValueError.
    cfg = _parse_network_config({"allowed_ips": ["10.0.0.5/24"]})
    assert cfg is not None
    assert "10.0.0.5/24" in cfg.allowed_ips


def test_parse_network_config_rejects_truly_invalid_cidr() -> None:
    """A garbage CIDR is still rejected with the proper error."""
    with pytest.raises(SeedError, match="invalid CIDR"):
        _parse_network_config({"allowed_ips": ["not-a-cidr"]})


def test_parse_network_config_none_returns_none() -> None:
    assert _parse_network_config(None) is None


# ---------------------------------------------------------------------------
# Line 300: `if "*" not in origin or origin == "*"`. Three mutants:
#   - 'not' drop: inverts -- now treats every origin containing '*' as
#     unsupported.
#   - 'or' -> 'and': bare '*' becomes a SeedError.
#   - '==' -> '!=': any '*' string other than '*' bypasses the check.
# ---------------------------------------------------------------------------


def test_validate_cors_origin_bare_star_accepted() -> None:
    """The bare ``*`` origin is accepted."""
    _validate_cors_origin("*")  # must not raise


def test_validate_cors_origin_no_wildcard_accepted() -> None:
    """Origins without any wildcard pass through."""
    _validate_cors_origin("https://example.com")


def test_validate_cors_origin_port_glob_accepted() -> None:
    """The port-glob form ``scheme://host:*`` is accepted."""
    _validate_cors_origin("http://localhost:*")
    _validate_cors_origin("https://app.example.com:*")


def test_validate_cors_origin_unsupported_wildcard_rejected() -> None:
    """A wildcard outside the port-glob form is rejected."""
    with pytest.raises(SeedError, match="unsupported"):
        _validate_cors_origin("https://*.example.com")


def test_validate_cors_origin_error_message_contains_or_marker() -> None:
    """The unsupported-wildcard error mentions both the port-glob form and the alternative."""
    with pytest.raises(SeedError) as exc:
        _validate_cors_origin("https://*.example.com")
    msg = str(exc.value)
    # The error string contains 'or remove the' - kills 'or' -> 'and'.
    assert "or remove the" in msg
    # And ' and rely on' - kills 'and' -> 'or' on the same line.
    assert "and rely on" in msg


# ---------------------------------------------------------------------------
# Lines 329/330: _parse_cors_config rejects non-dict, non-bool.
# Line 335/341/345: empty list -> falls back to CORSConfig defaults.
# Line 348: allow_credentials default is True.
# Line 349/353: bool/int/non-negative checks.
# ---------------------------------------------------------------------------


def test_parse_cors_config_non_dict_non_bool_rejected() -> None:
    with pytest.raises(SeedError, match="cors must be a mapping or boolean"):
        _parse_cors_config(42)


def test_parse_cors_config_false_returns_none() -> None:
    """cors=False yields None (kills False->True mutation on the bool branch)."""
    assert _parse_cors_config(False) is None


def test_parse_cors_config_true_returns_defaults() -> None:
    """cors=True yields a defaulted CORSConfig (kills True->False)."""
    cfg = _parse_cors_config(True)
    assert isinstance(cfg, CORSConfig)


def test_parse_cors_config_none_returns_none() -> None:
    assert _parse_cors_config(None) is None


def test_parse_cors_config_empty_origins_use_default() -> None:
    """allowed_origins=[] (empty) falls back to CORSConfig.allowed_origins."""
    cfg = _parse_cors_config({"allowed_origins": []})
    assert cfg is not None
    assert cfg.allowed_origins == CORSConfig.allowed_origins


def test_parse_cors_config_explicit_methods_preserved() -> None:
    """An explicit non-empty allow_methods list is preserved (kills 'not' drop on line 341)."""
    cfg = _parse_cors_config({"allow_methods": ["POST"]})
    assert cfg is not None
    assert tuple(cfg.allow_methods) == ("POST",)
    # The default is NOT applied when the user supplies a value.
    assert cfg.allow_methods != CORSConfig.allow_methods


def test_parse_cors_config_explicit_headers_preserved() -> None:
    """An explicit non-empty allow_headers list is preserved (kills 'not' drop on line 345)."""
    cfg = _parse_cors_config({"allow_headers": ["X-Custom"]})
    assert cfg is not None
    assert tuple(cfg.allow_headers) == ("X-Custom",)
    assert cfg.allow_headers != CORSConfig.allow_headers


def test_parse_cors_config_explicit_origins_preserved() -> None:
    """A non-empty origins list is preserved (kills 'not' drop on line 335)."""
    cfg = _parse_cors_config({"allowed_origins": ["https://example.com"]})
    assert cfg is not None
    assert tuple(cfg.allowed_origins) == ("https://example.com",)
    assert cfg.allowed_origins != CORSConfig.allowed_origins


def test_parse_cors_config_allow_credentials_default_is_true() -> None:
    """When unset, allow_credentials defaults to True (kills True->False)."""
    cfg = _parse_cors_config({})
    assert cfg is not None
    assert cfg.allow_credentials is True


def test_parse_cors_config_explicit_credentials_false() -> None:
    """allow_credentials=False is preserved."""
    cfg = _parse_cors_config({"allow_credentials": False})
    assert cfg is not None
    assert cfg.allow_credentials is False


def test_parse_cors_config_non_bool_credentials_rejected() -> None:
    with pytest.raises(SeedError, match="allow_credentials must be a bool"):
        _parse_cors_config({"allow_credentials": "yes"})


def test_parse_cors_config_max_age_zero_accepted() -> None:
    """max_age=0 is the boundary value and must be accepted (kills '<' -> '<=')."""
    cfg = _parse_cors_config({"max_age": 0})
    assert cfg is not None
    assert cfg.max_age == 0


def test_parse_cors_config_max_age_negative_rejected() -> None:
    """max_age=-1 is rejected (kills ' 0' -> ' 1' and '<' inversion)."""
    with pytest.raises(SeedError, match="max_age must be a non-negative integer"):
        _parse_cors_config({"max_age": -1})


def test_parse_cors_config_max_age_non_int_rejected() -> None:
    with pytest.raises(SeedError, match="max_age must be a non-negative integer"):
        _parse_cors_config({"max_age": "x"})


# ---------------------------------------------------------------------------
# Lines 379/385/391: _parse_dashboard_auth field-type guards.
# ---------------------------------------------------------------------------


def test_parse_dashboard_auth_non_dict_rejected() -> None:
    with pytest.raises(SeedError, match="dashboard_auth must be a mapping"):
        _parse_dashboard_auth(42)


def test_parse_dashboard_auth_non_string_password_rejected() -> None:
    with pytest.raises(SeedError, match="password must be a string"):
        _parse_dashboard_auth({"password": 42})


def test_parse_dashboard_auth_zero_timeout_accepted() -> None:
    """session_timeout_seconds=0 is the boundary and must be accepted."""
    cfg = _parse_dashboard_auth({"password": "pw", "session_timeout_seconds": 0})
    assert isinstance(cfg, DashboardAuthConfig)
    assert cfg.session_timeout_seconds == 0


def test_parse_dashboard_auth_negative_timeout_rejected() -> None:
    """Negative timeout is rejected."""
    with pytest.raises(SeedError, match="must be a non-negative integer"):
        _parse_dashboard_auth({"password": "pw", "session_timeout_seconds": -5})


def test_parse_dashboard_auth_non_int_timeout_rejected() -> None:
    with pytest.raises(SeedError, match="must be a non-negative integer"):
        _parse_dashboard_auth({"password": "pw", "session_timeout_seconds": "bad"})


def test_parse_dashboard_auth_none_returns_none() -> None:
    assert _parse_dashboard_auth(None) is None


# ---------------------------------------------------------------------------
# Lines 406/410/414/419/421/423: _parse_rate_limit_bucket guards.
# ---------------------------------------------------------------------------


def test_rate_limit_bucket_int_value() -> None:
    """An int shorthand yields a positive-request bucket on known names."""
    bucket = _parse_rate_limit_bucket("auth", 5)
    assert isinstance(bucket, RateLimitBucketConfig)
    assert bucket.requests == 5
    assert bucket.window_seconds == 60


def test_rate_limit_bucket_dict_requests_one_accepted() -> None:
    """requests_per_minute=1 is the smallest positive value and is accepted."""
    bucket = _parse_rate_limit_bucket("x", {"requests_per_minute": 1, "paths": ["/p"]})
    assert bucket.requests == 1


@pytest.mark.parametrize(
    ("raw", "match"),
    [
        ({"requests_per_minute": 0, "paths": ["/a"]}, "requests_per_minute must be a positive integer"),
        # Non-int requests_per_minute kills 'not isinstance' drop on line 406.
        ({"requests_per_minute": "x", "paths": ["/a"]}, "requests_per_minute must be a positive integer"),
        ({"requests_per_minute": 1.5, "paths": ["/a"]}, "requests_per_minute must be a positive integer"),
        ({"requests_per_minute": 5, "window_seconds": 0, "paths": ["/a"]}, "window_seconds must be a positive integer"),
        (
            {"requests_per_minute": 5, "window_seconds": -10, "paths": ["/a"]},
            "window_seconds must be a positive integer",
        ),
        # Non-int window_seconds kills 'not isinstance' drop on line 410.
        (
            {"requests_per_minute": 5, "window_seconds": "x", "paths": ["/a"]},
            "window_seconds must be a positive integer",
        ),
        ("not-int-not-dict", "must be an integer or mapping"),
    ],
)
def test_rate_limit_bucket_invalid_inputs_rejected(raw: object, match: str) -> None:
    with pytest.raises(SeedError, match=match):
        _parse_rate_limit_bucket("auth", raw)


@pytest.mark.parametrize("value", [0, -1])
def test_rate_limit_bucket_int_non_positive_rejected_via_post_check(value: int) -> None:
    """Int bucket values <=0 fail the post-check ``if requests <= 0``."""
    with pytest.raises(SeedError):
        _parse_rate_limit_bucket("auth", value)


def test_rate_limit_bucket_custom_name_without_paths_rejected() -> None:
    """A custom bucket (no default paths) without 'paths' is rejected."""
    with pytest.raises(SeedError, match="paths is required"):
        _parse_rate_limit_bucket("custom", 10)


# ---------------------------------------------------------------------------
# Lines 438/440/442: _parse_rate_limit_config wrapper.
# ---------------------------------------------------------------------------


def test_parse_rate_limit_config_non_dict_rejected() -> None:
    with pytest.raises(SeedError, match="rate_limit must be a mapping"):
        _parse_rate_limit_config(42)


def test_parse_rate_limit_config_empty_name_rejected() -> None:
    """A bucket name that is empty string is rejected."""
    with pytest.raises(SeedError, match="bucket names must be non-empty strings"):
        _parse_rate_limit_config({"": 5})


def test_parse_rate_limit_config_non_string_name_rejected() -> None:
    with pytest.raises(SeedError, match="bucket names must be non-empty strings"):
        _parse_rate_limit_config({42: 5})


def test_parse_rate_limit_config_none_returns_none() -> None:
    assert _parse_rate_limit_config(None) is None


def test_parse_rate_limit_config_real_buckets_no_phantom_entries() -> None:
    """The buckets tuple matches the input dict size exactly (kills [] -> [None])."""
    cfg = _parse_rate_limit_config({"auth": 3})
    assert cfg is not None
    assert len(cfg.buckets) == 1
    assert cfg.buckets[0].name == "auth"


# ---------------------------------------------------------------------------
# Lines 453/455/458/462: _parse_tenants guards.
# ---------------------------------------------------------------------------


def test_parse_tenants_non_list_rejected() -> None:
    with pytest.raises(SeedError, match="tenants must be a list"):
        _parse_tenants({"a": "b"})


def test_parse_tenants_non_mapping_item_rejected() -> None:
    with pytest.raises(SeedError, match=r"tenants\[0\] must be a mapping"):
        _parse_tenants(["not-a-mapping"])


def test_parse_tenants_empty_id_rejected() -> None:
    """A tenant with whitespace-only id is rejected."""
    with pytest.raises(SeedError, match="id must be a non-empty string"):
        _parse_tenants([{"id": "   "}])


def test_parse_tenants_none_returns_empty_tuple() -> None:
    """None -> empty tuple (kills [] -> [None] on the parsed list init)."""
    result = _parse_tenants(None)
    assert result == ()


def test_parse_tenants_duplicate_id_rejected() -> None:
    with pytest.raises(SeedError, match="Duplicate tenant id"):
        _parse_tenants([{"id": "alpha"}, {"id": "alpha"}])


def test_parse_tenants_valid_round_trip() -> None:
    """A valid tenants list parses to one TenantConfig per entry."""
    result = _parse_tenants([{"id": "alpha"}, {"id": "beta"}])
    assert len(result) == 2
    assert result[0].id == "alpha"
    assert result[1].id == "beta"


# ---------------------------------------------------------------------------
# Lines 488/495: _expand_env_value.
# ---------------------------------------------------------------------------


def test_expand_env_non_string_passthrough() -> None:
    """Non-string raw values pass through unchanged."""
    assert _expand_env_value(42, "field") == 42
    assert _expand_env_value(None, "field") is None


def test_expand_env_unset_var_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """An ${VAR} reference to an unset variable raises SeedError."""
    monkeypatch.delenv("UNSET_TEST_VAR_XYZ", raising=False)
    with pytest.raises(SeedError, match="references unset environment variable"):
        _expand_env_value("${UNSET_TEST_VAR_XYZ}", "field")


def test_expand_env_empty_var_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """An ${VAR} reference to an EMPTY variable is also rejected (kills 'or'->'and')."""
    monkeypatch.setenv("EMPTY_TEST_VAR", "   ")
    with pytest.raises(SeedError, match="references unset environment variable"):
        _expand_env_value("${EMPTY_TEST_VAR}", "field")


def test_expand_env_set_var_returns_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOME_VAR_FOR_TEST", "the-value")
    assert _expand_env_value("${SOME_VAR_FOR_TEST}", "field") == "the-value"


def test_expand_env_non_reference_string_passthrough() -> None:
    """A regular string (no ${} pattern) passes through verbatim."""
    assert _expand_env_value("plain", "field") == "plain"


# ---------------------------------------------------------------------------
# Lines 503/511/519/527: _require_* validators.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fn", "phrase"),
    [
        (_require_bool, "Extract and validate a boolean field"),
        (_require_str, "Extract and validate a string field"),
        (_require_positive_number, "Extract and validate a positive numeric field"),
        (_require_positive_int, "Extract and validate a positive integer field"),
    ],
)
def test_require_helper_docstrings_use_and(fn: object, phrase: str) -> None:
    """The helper docstrings literally say 'Extract and validate' (kills 'and' -> 'or')."""
    doc = fn.__doc__  # type: ignore[attr-defined]
    assert doc is not None
    assert phrase in doc, f"expected {phrase!r} in docstring, got: {doc!r}"


def test_require_bool_non_bool_rejected() -> None:
    with pytest.raises(SeedError, match="must be a bool"):
        _require_bool({"k": "yes"}, "k", False, "prefix")


@pytest.mark.parametrize(("value", "expected"), [(True, True), (False, False)])
def test_require_bool_passthrough(value: bool, expected: bool) -> None:
    assert _require_bool({"k": value}, "k", not value, "p") is expected


def test_require_bool_default_used_when_missing() -> None:
    assert _require_bool({}, "k", True, "p") is True


def test_require_str_non_string_rejected() -> None:
    with pytest.raises(SeedError, match="must be a string"):
        _require_str({"k": 42}, "k", "", "p")


def test_require_str_returns_stripped() -> None:
    assert _require_str({"k": "  hi  "}, "k", "", "p") == "hi"


@pytest.mark.parametrize("value", [0, -0.5, "x"])
def test_require_positive_number_invalid_rejected(value: object) -> None:
    """0, negative, and non-numeric values are rejected (kills '<=' boundary + 'not' drop)."""
    with pytest.raises(SeedError, match="must be a positive number"):
        _require_positive_number({"k": value}, "k", 1.0, "p")


def test_require_positive_number_one_accepted() -> None:
    """1.0 is positive and must be accepted."""
    assert _require_positive_number({"k": 1.0}, "k", 0.5, "p") == 1.0


@pytest.mark.parametrize("value", [0, -3, 1.5])
def test_require_positive_int_invalid_rejected(value: object) -> None:
    """0, negative, and float values fail (kills '< 1' boundary mutations)."""
    with pytest.raises(SeedError, match="must be a positive integer"):
        _require_positive_int({"k": value}, "k", 1, "p")


@pytest.mark.parametrize("value", [1, 2])
def test_require_positive_int_positive_accepted(value: int) -> None:
    """1 and 2 are accepted (pins ' 1' vs ' 2' threshold mutation)."""
    assert _require_positive_int({"k": value}, "k", 1, "p") == value


# ---------------------------------------------------------------------------
# Line 534: `if not url_text` in _validate_openclaw_enabled.
# ---------------------------------------------------------------------------


def test_validate_openclaw_enabled_empty_url_rejected() -> None:
    """An empty URL is rejected with the expected error message."""
    with pytest.raises(SeedError, match="url is required when the bridge is enabled"):
        _validate_openclaw_enabled("", "key", "agent")


def test_validate_openclaw_enabled_invalid_scheme_rejected() -> None:
    """A non-ws URL is rejected."""
    with pytest.raises(SeedError, match="must be a valid ws:// or wss:// URL"):
        _validate_openclaw_enabled("http://example.com", "key", "agent")


def test_validate_openclaw_enabled_missing_api_key_rejected() -> None:
    with pytest.raises(SeedError, match="api_key is required"):
        _validate_openclaw_enabled("ws://example.com", "", "agent")


def test_validate_openclaw_enabled_missing_agent_id_rejected() -> None:
    with pytest.raises(SeedError, match="agent_id is required"):
        _validate_openclaw_enabled("ws://example.com", "key", "")


def test_validate_openclaw_enabled_valid_passes() -> None:
    """A fully populated set of fields does NOT raise."""
    _validate_openclaw_enabled("wss://example.com:443/x", "key", "agent")


# ---------------------------------------------------------------------------
# Line 145/158: _parse_string_list type guards.
# ---------------------------------------------------------------------------


def test_parse_string_list_none_returns_empty_tuple() -> None:
    assert _parse_string_list(None, "field") == ()


def test_parse_string_list_non_list_rejected() -> None:
    with pytest.raises(SeedError, match="must be a list of strings"):
        _parse_string_list("not-a-list", "field")


def test_parse_string_list_non_string_element_rejected() -> None:
    with pytest.raises(SeedError, match="must be a list of strings"):
        _parse_string_list(["ok", 42], "field")


def test_parse_string_list_valid_round_trip() -> None:
    assert _parse_string_list(["a", "b"], "field") == ("a", "b")


# ---------------------------------------------------------------------------
# Boundary test: _parse_budget recognises numeric forms and the $N regex.
# ---------------------------------------------------------------------------


def test_parse_budget_none_returns_none() -> None:
    assert _parse_budget(None) is None


def test_parse_budget_int_passthrough() -> None:
    assert _parse_budget(20) == 20.0


def test_parse_budget_float_passthrough() -> None:
    assert _parse_budget(9.99) == 9.99


def test_parse_budget_dollar_form() -> None:
    assert _parse_budget("$30") == 30.0


def test_parse_budget_bare_numeric_string() -> None:
    assert _parse_budget("42.5") == 42.5


# ---------------------------------------------------------------------------
# Network config returns a NetworkConfig type with the proper structure.
# ---------------------------------------------------------------------------


def test_parse_network_config_empty_dict_yields_empty_allowed_ips() -> None:
    cfg = _parse_network_config({})
    assert isinstance(cfg, NetworkConfig)
    assert cfg.allowed_ips == ()


# ---------------------------------------------------------------------------
# role_model_policy.<role>.max_tokens (D2 OpenRouter KILL-NOTE regression):
# a seed file setting this key used to be rejected at parse time with
# "unknown keys: max_tokens" because _ROLE_POLICY_KEYS never listed it even
# though config_schema.py's RoleModelPolicyEntry and the spawner fold both
# already supported it (parser/schema divergence). Pin that it now parses
# and lands correctly typed (as an int, not a string) on the normalized
# role-policy dict that flows into AgentSpawner.role_model_policy.
# ---------------------------------------------------------------------------


def test_parse_single_role_policy_accepts_max_tokens() -> None:
    """A role policy with max_tokens=8000 must parse, not raise SeedError."""
    normalized = _parse_single_role_policy("adversary", {"provider": "openai_agents", "max_tokens": 8000})
    assert normalized["max_tokens"] == 8000
    assert isinstance(normalized["max_tokens"], int)
    assert not isinstance(normalized["max_tokens"], bool)
    assert normalized["provider"] == "openai_agents"


def test_parse_single_role_policy_max_tokens_only() -> None:
    """max_tokens alone (no other keys) is a valid, minimal role policy."""
    normalized = _parse_single_role_policy("backend", {"max_tokens": 4096})
    assert normalized == {"max_tokens": 4096}


@pytest.mark.parametrize("bad_value", [0, -1, "8000", 3.5, True])
def test_parse_single_role_policy_rejects_invalid_max_tokens(bad_value: object) -> None:
    """max_tokens must be a positive int - not zero/negative/string/float/bool."""
    with pytest.raises(SeedError, match="max_tokens.*positive integer"):
        _parse_single_role_policy("qa", {"max_tokens": bad_value})


def test_parse_role_model_policy_max_tokens_round_trip() -> None:
    """Full role_model_policy section with max_tokens round-trips through
    the public entry point _parse_role_model_policy (what the seed loader
    actually calls), matching the seed-file shape from the KILL-NOTE:
    ``role_model_policy: {adversary: {provider: openai_agents, max_tokens: 8000}}``.
    """
    parsed = _parse_role_model_policy(
        {"adversary": {"provider": "openai_agents", "model": "deepseek/deepseek-chat", "max_tokens": 8000}}
    )
    assert parsed is not None
    assert parsed["adversary"]["max_tokens"] == 8000
    assert parsed["adversary"]["provider"] == "openai_agents"
    assert parsed["adversary"]["model"] == "deepseek/deepseek-chat"
