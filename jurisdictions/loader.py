"""
Jurisdiction ruleset loader.

Composes federal + (optional) state + (optional) city YAML files into a
single ``EffectiveRuleset`` consumable by the tax-estimate computation.

Design notes
------------
- Composition order: federal → state → city. Each layer deep-merges
  over the previous; list fields are *replaced* (not appended) when
  overridden — a state's bracket schedule fully replaces federal's.
- Fallback: if the requested year's YAML is missing, walk back up to
  five prior years and emit ``UserWarning``. Set ``allow_fallback=False``
  to disable.
- No business logic lives here. The loader reads, merges, and validates;
  the math is in ``crowtax_engine.tax_estimate``.

Field-name lineage: the YAML schema (``capital_gains_treatment``,
``conformity_type``, etc.) was designed independently from standard tax
terminology; primary YAML data is sourced from state DOR PDFs to keep
the engine MIT-clean of any AGPL parameter-tree conventions.
"""

from __future__ import annotations

import dataclasses
import warnings
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

JURISDICTIONS_DIR = Path(__file__).parent

__all__ = ["load_ruleset", "EffectiveRuleset"]


@dataclasses.dataclass(frozen=True)
class EffectiveRuleset:
    """
    Fully-resolved ruleset for a (year, state, city) tuple.

    ``effective`` is the merged dict that the computation functions read;
    the per-layer dicts are kept verbatim for debugging and audit so a
    consumer can confirm which rate came from which file.
    """

    tax_year: int
    state: str | None
    city: str | None

    federal_layer: dict
    state_layer: dict | None
    city_layer: dict | None

    effective: dict
    source_files: tuple[str, ...]
    fallback_years: dict[str, int]


@lru_cache(maxsize=256)
def load_ruleset(
    year: int,
    state: str | None = None,
    city: str | None = None,
    *,
    allow_fallback: bool = True,
) -> EffectiveRuleset:
    """
    Load and compose the effective tax ruleset for ``(year, state, city)``.

    Parameters
    ----------
    year : int
        Tax year (e.g., ``2026``).
    state : str | None
        2-letter state abbreviation, lowercase (``"ca"``). ``None`` =
        federal only.
    city : str | None
        City slug matching the folder name (``"new_york_city"``,
        ``"yonkers"``, ``"philadelphia"``). ``None`` = no city layer.
    allow_fallback : bool, keyword-only
        If ``True`` (default), fall back to the nearest prior year if
        the requested year's YAML is missing (max 5 years back). Emits
        ``UserWarning``. If ``False``, raises ``FileNotFoundError``.

    Returns
    -------
    EffectiveRuleset

    Raises
    ------
    FileNotFoundError
        Required YAML file (or any fallback within the 5-year window)
        is missing.
    ValueError
        Merged result is missing one of the ``_REQUIRED_EFFECTIVE_FIELDS``.
    """
    federal_data, federal_year = _load_layer("federal", year, allow_fallback)

    state_data: dict | None
    state_year: int | None
    if state:
        state_data, state_year = _load_layer(
            f"states/{state}", year, allow_fallback
        )
    else:
        state_data, state_year = None, None

    city_data: dict | None
    city_year: int | None
    if state and city:
        city_data, city_year = _load_layer(
            f"states/{state}/cities/{city}", year, allow_fallback
        )
    else:
        city_data, city_year = None, None

    effective = _deep_merge(federal_data, state_data or {}, city_data or {})
    _validate_required_fields(effective, year, state, city)

    fallback_years: dict[str, int] = {}
    if federal_year != year:
        fallback_years["federal"] = federal_year
    if state_year is not None and state_year != year:
        fallback_years["state"] = state_year
    if city_year is not None and city_year != year:
        fallback_years["city"] = city_year

    if fallback_years:
        warnings.warn(
            f"load_ruleset: using fallback years for layers {fallback_years}. "
            f"Requested year={year}. Verify rates manually.",
            UserWarning,
            stacklevel=2,
        )

    source_files: list[str] = [f"federal/{federal_year}.yaml"]
    if state_year is not None:
        source_files.append(f"states/{state}/{state_year}.yaml")
    if city_year is not None:
        source_files.append(f"states/{state}/cities/{city}/{city_year}.yaml")

    return EffectiveRuleset(
        tax_year=year,
        state=state,
        city=city,
        federal_layer=federal_data,
        state_layer=state_data,
        city_layer=city_data,
        effective=effective,
        source_files=tuple(source_files),
        fallback_years=fallback_years,
    )


def _load_layer(
    prefix: str, year: int, allow_fallback: bool
) -> tuple[dict, int]:
    """
    Load a single YAML layer for ``prefix`` + ``year``, with optional
    fall-back to prior years (max 5 back).

    Returns ``(data_dict, actual_year_loaded)``.
    """
    candidate = JURISDICTIONS_DIR / prefix / f"{year}.yaml"
    if candidate.exists():
        return yaml.safe_load(candidate.read_text()) or {}, year

    if not allow_fallback:
        raise FileNotFoundError(f"Ruleset not found: {candidate}")

    for fallback_year in range(year - 1, year - 6, -1):
        candidate = JURISDICTIONS_DIR / prefix / f"{fallback_year}.yaml"
        if candidate.exists():
            return yaml.safe_load(candidate.read_text()) or {}, fallback_year

    raise FileNotFoundError(
        f"No ruleset found for {prefix!r} in years {year - 5}–{year}. "
        "Cannot compute taxes without a base ruleset."
    )


def _deep_merge(*layers: dict) -> dict:
    """
    Merge dicts left-to-right; later layers override earlier ones.

    Lists are *replaced*, not extended. Nested dicts merge recursively.
    """
    result: dict[str, Any] = {}
    for layer in layers:
        if not layer:
            continue
        for key, val in layer.items():
            if isinstance(val, dict) and isinstance(result.get(key), dict):
                result[key] = _deep_merge(result[key], val)
            else:
                result[key] = val
    return result


# Required after the federal layer is merged in. ``no_income_tax`` and
# ``is_excise_tax`` drive the computation branch selector — a missing
# value would silently default to False in Python and cause WA excise
# to be skipped.
_REQUIRED_EFFECTIVE_FIELDS = (
    "tax_year",
    "has_ltcg_preference",
    "capital_gains_treatment",
    "loss_carryforward",
    "no_income_tax",
    "is_excise_tax",
)


def _validate_required_fields(
    effective: dict,
    year: int,
    state: str | None,
    city: str | None,
) -> None:
    missing = [f for f in _REQUIRED_EFFECTIVE_FIELDS if f not in effective]
    if missing:
        raise ValueError(
            f"Merged ruleset for (year={year}, state={state}, city={city}) "
            f"is missing required fields: {missing}"
        )
