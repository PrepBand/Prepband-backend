"""Raw score -> estimated IELTS band (§4.3): "optional estimated band
conversion tables with documented provenance and revision date... clearly
explain that practice score conversions are approximations and not
official IELTS scores."

Provenance: a simplified, evenly-spaced percentage-to-band mapping
(0% -> band 0, 100% -> band 9), not a calibrated per-module official
IELTS conversion table - those vary by test version and aren't published
in a form this project can license. Revision date: 2026-11-19. Replace
with a real per-module/per-track table (a new DB-backed table, admin
editable) once content exists to calibrate against; until then this is
intentionally approximate, and every caller must present it as such
(FR-READ-06/FR-LIST-07: "clearly distinguish" estimated from official).
"""


def estimate_band(raw_score: float, max_score: float) -> float | None:
    if max_score <= 0:
        return None
    percentage = max(0.0, min(1.0, raw_score / max_score))
    band = percentage * 9
    # IELTS bands are reported in 0.5 increments.
    return round(band * 2) / 2
