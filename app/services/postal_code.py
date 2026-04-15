"""Postal code → nearest M&H chapel resolution.

Uses pgeocode for Canadian postal code geocoding and haversine for distance.
Falls back to area-based mapping when postal code is invalid.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import logging

logger = logging.getLogger(__name__)

_CA_POSTAL_RE = re.compile(
    r"^[ABCEGHJ-NPRSTVXY]\d[ABCEGHJ-NPRSTV-Z]\s?\d[ABCEGHJ-NPRSTV-Z]\d$",
    re.IGNORECASE,
)

_nomi = None


def _get_nomi():
    """Lazy-init pgeocode Nominatim so import-time download failures don't crash the app."""
    global _nomi
    if _nomi is not None:
        return _nomi
    try:
        import pgeocode
        _nomi = pgeocode.Nominatim("CA")
    except Exception as exc:
        logger.warning("pgeocode init failed (will use FSA fallback): %s", exc)
    return _nomi


# Fallback: map FSA (first 3 chars) to approximate lat/lng for Calgary-area codes
_FSA_FALLBACK: Dict[str, Tuple[float, float]] = {
    "T1S": (50.94, -113.96), "T1X": (50.90, -113.95), "T1Y": (51.07, -113.95),
    "T2A": (51.03, -113.97), "T2B": (51.04, -113.98), "T2C": (50.98, -114.02),
    "T2E": (51.08, -114.03), "T2G": (51.04, -114.05), "T2H": (50.99, -114.04),
    "T2J": (50.97, -114.04), "T2K": (51.09, -114.06), "T2L": (51.10, -114.11),
    "T2M": (51.08, -114.08), "T2N": (51.07, -114.13), "T2P": (51.05, -114.07),
    "T2R": (51.04, -114.08), "T2S": (51.02, -114.07), "T2T": (51.03, -114.08),
    "T2V": (50.98, -114.08), "T2W": (50.95, -114.07), "T2X": (50.91, -114.00),
    "T2Y": (50.93, -114.10), "T2Z": (50.91, -113.97), "T3A": (51.13, -114.18),
    "T3B": (51.09, -114.17), "T3C": (51.05, -114.12), "T3E": (51.02, -114.12),
    "T3G": (51.15, -114.15), "T3H": (51.05, -114.18), "T3J": (51.12, -113.95),
    "T3K": (51.15, -114.05), "T3L": (51.17, -114.08), "T3M": (50.87, -114.07),
    "T3N": (51.13, -114.01), "T3P": (51.12, -114.22), "T3R": (51.10, -114.24),
    "T3Z": (51.08, -114.50), "T4A": (51.27, -114.00), "T4B": (51.29, -114.02),
    "T4C": (51.20, -114.38),
}


@dataclass
class Chapel:
    slug: str
    name: str
    lat: float
    lng: float


CHAPELS: List[Chapel] = [
    Chapel("park_memorial", "Park Memorial Chapel", 51.0185, -114.0735),
    Chapel("eastside", "Eastside Memorial Chapel", 51.0565, -114.0095),
    Chapel("fish_creek", "Fish Creek Chapel", 50.9365, -114.0315),
    Chapel("deerfoot_south", "Deerfoot South", 50.9535, -113.9675),
    Chapel("chapel_of_the_bells", "Chapel Of The Bells", 51.0675, -114.0625),
    Chapel("calgary_crematorium", "Calgary Crematorium", 51.0625, -114.0795),
    Chapel("heritage", "Heritage Funeral Services", 51.0595, -114.0975),
    Chapel("crowfoot", "Crowfoot Chapel", 51.1215, -114.1595),
    Chapel("airdrie", "Airdrie Funeral Home", 51.2695, -114.0235),
    Chapel("cochrane", "Cochrane Funeral Home", 51.1825, -114.4675),
]

AREA_MAP: Dict[str, str] = {
    "south_calgary": "fish_creek",
    "north_calgary": "chapel_of_the_bells",
    "airdrie": "airdrie",
    "cochrane": "cochrane",
}


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2) ** 2
    )
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def validate_postal_code(raw: str) -> Optional[str]:
    """Return cleaned uppercase postal code or None if invalid format."""
    cleaned = raw.strip().upper()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if len(cleaned) == 6:
        cleaned = cleaned[:3] + " " + cleaned[3:]
    if _CA_POSTAL_RE.match(cleaned):
        return cleaned
    return None


def geocode_postal_code(postal_code: str) -> Optional[Tuple[float, float]]:
    """Return (lat, lng) for a Canadian postal code, or None if lookup fails."""
    fsa = postal_code.replace(" ", "")[:3].upper()
    nomi = _get_nomi()
    if nomi is not None:
        try:
            row = nomi.query_postal_code(fsa)
            if row is not None and not math.isnan(row.latitude) and not math.isnan(row.longitude):
                return (float(row.latitude), float(row.longitude))
        except Exception as exc:
            logger.warning("pgeocode lookup failed for %s: %s", fsa, exc)
    fallback = _FSA_FALLBACK.get(fsa)
    if fallback:
        logger.info("Using FSA fallback for %s", fsa)
        return fallback
    return None


def find_nearest_chapel(lat: float, lng: float) -> Tuple[Chapel, float]:
    """Return the nearest chapel and distance in km."""
    best: Optional[Chapel] = None
    best_dist = float("inf")
    for c in CHAPELS:
        d = _haversine_km(lat, lng, c.lat, c.lng)
        if d < best_dist:
            best_dist = d
            best = c
    assert best is not None
    return best, round(best_dist, 1)


def resolve_postal_code(raw: str) -> Dict:
    """Full pipeline: validate → geocode → find nearest chapel."""
    cleaned = validate_postal_code(raw)
    if not cleaned:
        return {"ok": False, "error": "invalid_postal_code"}
    coords = geocode_postal_code(cleaned)
    if not coords:
        return {"ok": False, "error": "postal_code_not_found"}
    chapel, dist = find_nearest_chapel(coords[0], coords[1])
    return {
        "ok": True,
        "location_slug": chapel.slug,
        "location_name": chapel.name,
        "distance_km": dist,
    }


def resolve_area(area_key: str) -> Dict:
    """Map a Calgary area to a chapel."""
    slug = AREA_MAP.get(area_key.lower().strip().replace(" ", "_"))
    if not slug:
        return {"ok": False, "error": f"unknown area: {area_key}"}
    chapel = next((c for c in CHAPELS if c.slug == slug), None)
    if not chapel:
        return {"ok": False, "error": f"chapel not found for area: {area_key}"}
    return {
        "ok": True,
        "location_slug": chapel.slug,
        "location_name": chapel.name,
    }
