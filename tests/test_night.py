"""Solar-altitude night logic: offline, no ephemeris download."""
import unittest
from datetime import datetime, timezone

from magi.night import is_night, night_start_unix, solar_altitude_deg

LAT, LON = 39.78, -98.79  # Smith Center, Kansas area


def stamp(text):
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp())


class NightTests(unittest.TestCase):
    def test_noon_vs_midnight(self):
        noon = solar_altitude_deg(LAT, LON, stamp('2026-11-02T18:00:00'))
        midnight = solar_altitude_deg(LAT, LON, stamp('2026-11-02T06:00:00'))
        self.assertGreater(noon, 20.0)
        self.assertLess(midnight, -20.0)
        self.assertFalse(is_night(LAT, LON, stamp('2026-11-02T18:00:00')))
        self.assertTrue(is_night(LAT, LON, stamp('2026-11-02T06:00:00')))

    def test_summer_and_winter_noon(self):
        self.assertGreater(solar_altitude_deg(LAT, LON, stamp('2026-07-02T18:00:00')), 60.0)
        self.assertLess(solar_altitude_deg(LAT, LON, stamp('2026-01-02T18:00:00')), 35.0)

    def test_night_start_within_current_night(self):
        now = stamp('2026-11-02T06:00:00')  # midnight CST
        start = night_start_unix(LAT, LON, now)
        self.assertIsNotNone(start)
        self.assertLess(start, now)
        self.assertGreater(now - start, 0)
        self.assertLessEqual(now - start, 24 * 3600)
        self.assertTrue(is_night(LAT, LON, start + 3600))
        # The sample just before the reported start is not yet night.
        self.assertFalse(is_night(LAT, LON, start - 300))

    def test_night_start_none_when_day(self):
        self.assertIsNone(night_start_unix(LAT, LON, stamp('2026-11-02T18:00:00')))

    def test_threshold_parameter(self):
        # At the default -12 threshold noon is day; at +40 it is night,
        # proving the threshold actually gates the decision.
        noon = stamp('2026-11-02T18:00:00')
        self.assertFalse(is_night(LAT, LON, noon))
        self.assertTrue(is_night(LAT, LON, noon, threshold_deg=40.0))

    def test_no_crossing_polar_day(self):
        # 78N in late June: the sun never gets below -12, so even at
        # local "night" there is no crossing in the last 24 hours.
        # (At 06:00 UTC the sun is up there in June; assert the
        # day-case None, and that a full 24h of above-threshold sun
        # also yields None when currently above threshold.)
        self.assertIsNone(night_start_unix(78.0, 15.0, stamp('2026-06-21T12:00:00')))


if __name__ == '__main__':
    unittest.main()
