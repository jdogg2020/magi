"""Night determination from station location and time, offline.

This module deliberately uses a pure-math solar-position approximation
(NOAA solar-calculator style) instead of a Skyfield Sun position: Magi
ships no Sun ephemeris, and a Skyfield ephemeris lookup could try to
download one over the station's limited cellular link in the field.
Accuracy is well under a degree for the purpose here, which is only to
decide whether it is night and when the current night began — the
stars-visible gate and the solver's own verification gates, not this
module, decide whether a frame is usable.

Provisional threshold (not a validated field threshold): the sun is
considered down for star work when its altitude is below -12 degrees.
"""
import math
from datetime import datetime, timezone

DEFAULT_NIGHT_ALTITUDE_DEG = -12.0
# Grid step for the night-start search. The returned night start is
# always on this epoch-aligned grid, so it is a stable identifier for
# one night: two invocations an hour apart, or a second apart, return
# the same value, and the retry worker's once-per-night check works
# despite the timer's randomized delay. (A value derived from the
# invocation's own seconds would shift with every call and the
# completed-night check would almost never match.)
NIGHT_START_STEP_SECONDS = 300
_NIGHT_START_STEP_SECONDS = NIGHT_START_STEP_SECONDS
_NIGHT_START_MAX_BACK_SECONDS = 24 * 3600


def solar_altitude_deg(latitude, longitude, timestamp_unix):
    """Approximate solar altitude in degrees for a UTC timestamp.

    Longitude is east-positive (Kansas is negative). The result is the
    sun's altitude above the horizon: positive by day, negative at
    night, -12 at the provisional night threshold.
    """
    dt = datetime.fromtimestamp(timestamp_unix, timezone.utc)
    julian_day = timestamp_unix / 86400.0 + 2440587.5
    centuries = (julian_day - 2451545.0) / 36525.0
    mean_long = (280.46646 + centuries * (36000.76983 + 0.0003032 * centuries)) % 360.0
    mean_anom = 357.52911 + centuries * (35999.05029 - 0.0001537 * centuries)
    eccent = 0.016708634 - centuries * (0.000042037 + 0.0000001267 * centuries)
    mean_anom_rad = math.radians(mean_anom)
    eq_of_center = (math.sin(mean_anom_rad) * (1.914602 - centuries * (0.004817 + 0.000014 * centuries))
                    + math.sin(2 * mean_anom_rad) * (0.019993 - 0.000101 * centuries)
                    + math.sin(3 * mean_anom_rad) * 0.000289)
    true_long = mean_long + eq_of_center
    omega = 125.04 - 1934.136 * centuries
    apparent_long = true_long - 0.00569 - 0.00478 * math.sin(math.radians(omega))
    mean_obliq = 23.0 + (26.0 + (21.448 - centuries * (46.815 + centuries * (0.00059 - centuries * 0.001813))) / 60.0) / 60.0
    obliq = mean_obliq + 0.00256 * math.cos(math.radians(omega))
    declination = math.degrees(math.asin(
        math.sin(math.radians(obliq)) * math.sin(math.radians(apparent_long))))
    var_y = math.tan(math.radians(obliq / 2.0)) ** 2
    eq_time = 4.0 * math.degrees(
        var_y * math.sin(2 * math.radians(mean_long))
        - 2.0 * eccent * math.sin(mean_anom_rad)
        + 4.0 * eccent * var_y * math.sin(mean_anom_rad) * math.cos(2 * math.radians(mean_long))
        - 0.5 * var_y * var_y * math.sin(4 * math.radians(mean_long))
        - 1.25 * eccent * eccent * math.sin(2 * mean_anom_rad))
    true_solar_minutes = (dt.hour * 60.0 + dt.minute + dt.second / 60.0
                          + eq_time + 4.0 * longitude) % 1440.0
    hour_angle = true_solar_minutes / 4.0 - 180.0
    lat_rad, decl_rad, ha_rad = math.radians(latitude), math.radians(declination), math.radians(hour_angle)
    cos_zenith = (math.sin(lat_rad) * math.sin(decl_rad)
                  + math.cos(lat_rad) * math.cos(decl_rad) * math.cos(ha_rad))
    cos_zenith = max(-1.0, min(1.0, cos_zenith))
    return 90.0 - math.degrees(math.acos(cos_zenith))


def is_night(latitude, longitude, timestamp_unix, threshold_deg=DEFAULT_NIGHT_ALTITUDE_DEG):
    """True when the sun is below ``threshold_deg`` at that time."""
    return solar_altitude_deg(latitude, longitude, timestamp_unix) < threshold_deg


def night_start_unix(latitude, longitude, now_unix, threshold_deg=DEFAULT_NIGHT_ALTITUDE_DEG):
    """Start of the current night, or None.

    The most recent downward crossing of ``threshold_deg``, found by
    walking back from ``now_unix`` in 5-minute steps, at most 24 hours.
    Returns None when it is currently day, and when no crossing is
    found in the last 24 hours (for example polar day or polar night:
    in that case tonight cannot be established from this rule, and the
    caller must skip rather than guess).

    The search grid is anchored to the Unix epoch, not to
    ``now_unix``: samples are ``floor(now / 300) * 300 - k * 300``.
    The returned value is therefore the first grid sample at or below
    the threshold, within 5 minutes after the true crossing, and it
    is identical for every invocation in the same night — it is safe
    to store as the night's identifier (see ``completed_night``).
    Returns None when the night began within the current grid step:
    the first grid sample at night is still in the future, so the
    next invocation (which will see the same grid value) handles it
    rather than this one inventing a start time.
    """
    if not is_night(latitude, longitude, now_unix, threshold_deg):
        return None
    grid_now = (int(now_unix) // NIGHT_START_STEP_SECONDS) * NIGHT_START_STEP_SECONDS
    steps = _NIGHT_START_MAX_BACK_SECONDS // NIGHT_START_STEP_SECONDS
    for step in range(0, steps + 1):
        candidate = grid_now - step * NIGHT_START_STEP_SECONDS
        if not is_night(latitude, longitude, candidate, threshold_deg):
            night_start = candidate + NIGHT_START_STEP_SECONDS
            return night_start if night_start <= now_unix else None
    return None
