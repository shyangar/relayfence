import pytest
from pydantic import ValidationError

from relayfence.quota import QuotaObservation, RouteOption, choose_route


def route(id="a", software="codex", **kwargs):
    return RouteOption(id=id, software=software, provider="provider", model="model", **kwargs)


def observation(route_id="a", **kwargs):
    values = dict(
        route_id=route_id,
        status="available",
        observed_at=10,
        expires_at=100,
        source="provider_api",
    )
    values.update(kwargs)
    return QuotaObservation(**values)


def test_current_software_precedes_other_and_configuration_order_is_preserved():
    routes = [route("x", "other"), route("b"), route("a")]
    result = choose_route(routes, [observation(r.id) for r in routes], "a", 20)
    assert result.route_id == "b"
    assert result.considered == ["b", "a", "x"]
    assert not result.requires_probe


def test_current_can_be_retained_and_other_software_is_fallback():
    routes = [route(), route("b", "other")]
    assert choose_route(routes, [observation()], "a", 20).route_id == "a"
    assert choose_route(routes, [observation("b")], "a", 20).route_id == "b"


def test_verified_availability_precedes_unknown_probe():
    result = choose_route([route(), route("b", "other")], [observation("b")], "a", 20, True)
    assert result.route_id == "b"
    assert not result.requires_probe


@pytest.mark.parametrize(
    "changes",
    [
        {"expires_at": 20},
        {"reset_at": 20},
        {"observed_at": 21},
        {"status": "unknown"},
        {"source": "operator"},
        {"source": "runtime_error"},
    ],
)
def test_unverified_observations_require_explicit_probe(changes):
    observations = [observation(**changes)]
    assert choose_route([route()], observations, "a", 20).route_id is None
    decision = choose_route([route()], observations, "a", 20, allow_unknown=True)
    assert decision.route_id == "a"
    assert decision.requires_probe
    assert "probe required" in decision.reason
    assert observations[0].source in decision.reason


def test_missing_observation_is_unknown():
    assert choose_route([route()], [], "a", 20).route_id is None
    assert choose_route([route()], [], "a", 20, True).requires_probe


@pytest.mark.parametrize("status", ["available", "exhausted", "rate_limited", "unknown"])
def test_zero_remaining_blocks_even_explicit_probe(status):
    decision = choose_route(
        [route()], [observation(status=status, remaining=0, unit="credits")], "a", 20, True
    )
    assert decision.route_id is None
    assert "zero remaining" in decision.reason


@pytest.mark.parametrize("status", ["exhausted", "rate_limited"])
def test_runtime_failure_blocks_until_expiry_or_reset(status):
    obs = observation(status=status, source="runtime_error", reset_at=30)
    blocked = choose_route([route()], [obs], "a", 20, True)
    assert blocked.route_id is None
    assert status in blocked.reason
    reset = choose_route([route()], [obs], "a", 30, True)
    assert reset.route_id == "a" and reset.requires_probe
    assert "reset reached" in reset.reason
    expired = choose_route([route()], [obs], "a", 100, True)
    assert expired.requires_probe


def test_latest_wins_even_if_input_order_is_reversed():
    available = observation()
    exhausted = observation(status="exhausted", observed_at=15)
    for observations in ([available, exhausted], [exhausted, available]):
        assert choose_route([route()], observations, "a", 20, True).route_id is None
    restored = observation(observed_at=16)
    assert choose_route([route()], [exhausted, restored], "a", 20).route_id == "a"


def test_timestamp_ties_use_last_entry():
    assert (
        choose_route([route()], [observation(), observation(status="exhausted")], "a", 20).route_id
        is None
    )


def test_disabled_routes_never_selected_or_considered():
    result = choose_route([route(enabled=False)], [observation()], "a", 20, True)
    assert result.route_id is None
    assert result.considered == []


@pytest.mark.parametrize("now", [-1, float("inf"), float("nan"), True])
def test_invalid_now(now):
    with pytest.raises(ValueError):
        choose_route([route()], [], "a", now)


@pytest.mark.parametrize(
    "routes,current", [([], "a"), ([route()], "missing"), ([route(), route()], "a")]
)
def test_invalid_route_configuration(routes, current):
    with pytest.raises(ValueError):
        choose_route(routes, [], current, 20)


@pytest.mark.parametrize(
    "changes",
    [
        {"remaining": -1, "unit": "credits"},
        {"remaining": float("inf"), "unit": "tokens"},
        {"remaining": float("nan"), "unit": "requests"},
        {"remaining": 1},
        {"status": "exhausted", "remaining": 1, "unit": "percent"},
        {"observed_at": -1},
        {"observed_at": float("inf")},
        {"expires_at": 0},
        {"expires_at": 10},
        {"expires_at": float("nan")},
        {"reset_at": -1},
        {"reset_at": float("inf")},
        {"status": "invented"},
        {"unit": "dollars"},
        {"source": "estimate"},
        {"detail": "x" * 1001},
        {"extra": 1},
    ],
)
def test_invalid_observations(changes):
    with pytest.raises(ValidationError):
        observation(**changes)


def test_route_rejects_extra_fields_and_empty_ids():
    with pytest.raises(ValidationError):
        route(extra=True)
    with pytest.raises(ValidationError):
        route("")


def test_unconfigured_observations_are_ignored():
    decision = choose_route([route()], [observation("unconfigured"), observation()], "a", 20)
    assert decision.route_id == "a"
