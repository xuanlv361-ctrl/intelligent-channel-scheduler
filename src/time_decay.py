"""Configurable deterministic exponentially weighted moving averages."""

from __future__ import annotations

from math import isfinite
from typing import Iterable


class TimeDecayError(ValueError):
    pass


def validate_alpha(alpha: float) -> float:
    value = float(alpha)
    if not 0 < value <= 1:
        raise TimeDecayError("alpha must satisfy 0 < alpha <= 1")
    return value


def update_ewma(
    previous_metric: float | None,
    latest_value: float,
    alpha: float,
    *,
    lower_bound: float | None = None,
    upper_bound: float | None = None,
) -> float:
    weight = validate_alpha(alpha)
    latest = float(latest_value)
    if not isfinite(latest):
        raise TimeDecayError("latest_value must be finite")
    if lower_bound is not None and latest < lower_bound:
        raise TimeDecayError("latest_value is below configured bound")
    if upper_bound is not None and latest > upper_bound:
        raise TimeDecayError("latest_value is above configured bound")
    if previous_metric is None:
        return latest
    previous = float(previous_metric)
    if not isfinite(previous):
        raise TimeDecayError("previous_metric must be finite")
    result = weight * latest + (1.0 - weight) * previous
    if lower_bound is not None:
        result = max(result, lower_bound)
    if upper_bound is not None:
        result = min(result, upper_bound)
    return result


def calculate_ewma(
    values: Iterable[float],
    alpha: float,
    *,
    lower_bound: float | None = None,
    upper_bound: float | None = None,
) -> float | None:
    result: float | None = None
    for value in values:
        result = update_ewma(
            result, value, alpha,
            lower_bound=lower_bound, upper_bound=upper_bound,
        )
    return result
