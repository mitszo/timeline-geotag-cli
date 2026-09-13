from __future__ import annotations

from contextlib import redirect_stdout
from datetime import datetime, timedelta
from io import StringIO
import unittest
from unittest.mock import patch

from timeline_geotag.cli import Match, Position, TimelinePoint, Visit, find_match, google_maps_url, main, parse_duration, parse_offset, parse_position


UTC = datetime.fromisoformat("2026-09-12T10:00:00+00:00").tzinfo


def at(minutes: int) -> datetime:
    return datetime(2026, 9, 12, 10, 0, tzinfo=UTC) + timedelta(minutes=minutes)


class MatchingTests(unittest.TestCase):
    def test_visit_has_priority(self) -> None:
        match = find_match(
            at(5),
            [Visit(at(0), at(10), Position(34.0, 135.0))],
            [TimelinePoint(at(0), Position(0.0, 0.0)), TimelinePoint(at(10), Position(1.0, 1.0))],
            timedelta(minutes=15), 5,
        )
        self.assertEqual(match, Match(Position(34.0, 135.0), "visit"))

    def test_interpolates_between_safe_points(self) -> None:
        match = find_match(
            at(4), [],
            [TimelinePoint(at(0), Position(34.0, 135.0)), TimelinePoint(at(10), Position(34.01, 135.02))],
            timedelta(minutes=15), 5,
        )
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.source, "interpolated")
        self.assertEqual(match.position.latitude, 34.004)
        self.assertEqual(match.position.longitude, 135.008)

    def test_refuses_interpolation_over_large_gap(self) -> None:
        match = find_match(
            at(10), [],
            [TimelinePoint(at(0), Position(34.0, 135.0)), TimelinePoint(at(30), Position(34.01, 135.02))],
            timedelta(minutes=15), 5,
        )
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.source, "nearest")

    def test_parses_duration_and_offset(self) -> None:
        self.assertEqual(parse_duration("15m"), timedelta(minutes=15))
        self.assertEqual(parse_offset("-7m"), -timedelta(minutes=7))
        self.assertEqual(parse_offset("+01:30"), timedelta(hours=1, minutes=30))

    def test_parses_object_location(self) -> None:
        self.assertEqual(
            parse_position({"latLng": "34.6872571°, 135.5258546°"}),
            Position(34.6872571, 135.5258546),
        )

    def test_builds_google_maps_url(self) -> None:
        self.assertEqual(
            google_maps_url(Position(34.6872571, 135.5258546)),
            "https://www.google.com/maps/search/?api=1&query=34.6872571%2C135.5258546",
        )

    def test_prints_bash_completion(self) -> None:
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["completion", "bash"]), 0)
        completion = output.getvalue()
        self.assertIn("complete -F _shtab_timeline_geotag timeline-geotag", completion)
        self.assertIn("_shtab_timeline_geotag_pos_0_COMPGEN=_shtab_compgen_dirs", completion)
        self.assertIn("_shtab_timeline_geotag_pos_1_COMPGEN=_shtab_compgen_files", completion)

    def test_missing_exiftool_stops_before_processing_with_exit_code_one(self) -> None:
        error = StringIO()
        with patch("timeline_geotag.cli.shutil.which", return_value=None), redirect_stdout(StringIO()):
            with unittest.mock.patch("sys.stderr", error):
                self.assertEqual(main(["missing-photos", "missing-timeline.json"]), 1)
        self.assertIn("exiftool was not found on PATH", error.getvalue())
