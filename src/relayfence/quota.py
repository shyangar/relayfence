"""Pure routing policy over quota observations, without I/O or execution.

An observation is evidence, not a guarantee that a subsequent request succeeds.
This policy never derives balances from token consumption or assumes a reset
restored credit. Callers must obtain observations and perform any required probe,
execution, or retry themselves.
"""

from math import isfinite
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Nonnegative = Annotated[float, Field(ge=0, allow_inf_nan=False)]
Positive = Annotated[float, Field(gt=0, allow_inf_nan=False)]


class RouteOption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    software: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    enabled: bool = True


class QuotaObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    route_id: str = Field(min_length=1)
    status: Literal["available", "exhausted", "rate_limited", "unknown"]
    remaining: Nonnegative | None = None
    unit: Literal["tokens", "requests", "credits", "percent"] | None = None
    observed_at: Nonnegative
    expires_at: Positive
    reset_at: Nonnegative | None = None
    source: Literal["provider_api", "runtime_error", "operator"]
    detail: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def consistent_observation(self):
        if self.expires_at <= self.observed_at:
            raise ValueError("expires_at must be greater than observed_at")
        if self.remaining is not None and self.unit is None:
            raise ValueError("remaining requires a unit")
        if self.status == "exhausted" and self.remaining is not None and self.remaining > 0:
            raise ValueError("exhausted observations cannot have positive remaining")
        return self


class RouteDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    route_id: str | None
    reason: str
    requires_probe: bool
    considered: list[str]


def _state(observation: QuotaObservation | None, now: float) -> tuple[str, str]:
    if observation is None:
        return "unknown", "no quota observation"
    source = observation.source
    if observation.observed_at > now:
        return "unknown", f"future observation from {source} requires recheck"
    if observation.expires_at <= now:
        return "unknown", f"expired observation from {source} requires recheck"
    if observation.reset_at is not None and observation.reset_at <= now:
        return "unknown", f"reset reached for {source} observation; credit requires recheck"
    if observation.remaining == 0:
        return "exhausted", f"zero remaining reported by {source}"
    if observation.status == "available" and source != "provider_api":
        return "unknown", f"unverified available observation from {source}"
    return observation.status, f"{observation.status} reported by {source}"


def choose_route(
    routes: list[RouteOption],
    observations: list[QuotaObservation],
    current_route_id: str,
    now: float,
    allow_unknown: bool = False,
) -> RouteDecision:
    """Choose verified availability before an explicitly permitted probe.

    Within each evidence class, current software comes first and configuration
    order is preserved. The latest observation wins; later list entries break
    timestamp ties. Disabled routes and known exhaustion/rate limits never qualify.
    ``considered`` lists enabled routes in software preference order.
    """
    if isinstance(now, bool) or not isfinite(now) or now < 0:
        raise ValueError("now must be nonnegative and finite")
    ids = [route.id for route in routes]
    if len(ids) != len(set(ids)):
        raise ValueError("route IDs must be distinct")
    current = next((route for route in routes if route.id == current_route_id), None)
    if current is None:
        raise ValueError("current_route_id must refer to a configured route")
    latest: dict[str, QuotaObservation] = {}
    for observation in observations:
        previous = latest.get(observation.route_id)
        if previous is None or observation.observed_at >= previous.observed_at:
            latest[observation.route_id] = observation
    ordered = sorted(
        (route for route in routes if route.enabled),
        key=lambda route: route.software != current.software,
    )
    considered = [route.id for route in ordered]
    states = [(route, *_state(latest.get(route.id), now)) for route in ordered]
    for route, status, reason in states:
        if status == "available":
            return RouteDecision(
                route_id=route.id, reason=reason, requires_probe=False, considered=considered
            )
    if allow_unknown:
        for route, status, reason in states:
            if status == "unknown":
                return RouteDecision(
                    route_id=route.id,
                    reason=f"Unknown quota explicitly allowed; probe required: {reason}",
                    requires_probe=True,
                    considered=considered,
                )
    details = "; ".join(f"{route.id}: {reason}" for route, _, reason in states)
    return RouteDecision(
        route_id=None,
        reason=f"No eligible route. {details}" if details else "No enabled routes.",
        requires_probe=False,
        considered=considered,
    )
