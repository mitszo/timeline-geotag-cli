"""Command-line interface for timeline-geotag."""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import shtab


PHOTO_SUFFIXES = {".jpg", ".rw2"}
GEO_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)°?\s*,\s*(-?\d+(?:\.\d+)?)°?\s*$")


@dataclass(frozen=True)
class Position:
    latitude: float
    longitude: float


@dataclass(frozen=True)
class Visit:
    start: datetime
    end: datetime
    position: Position


@dataclass(frozen=True)
class TimelinePoint:
    timestamp: datetime
    position: Position


@dataclass(frozen=True)
class Match:
    position: Position
    source: str


class DependencyError(Exception):
    """A required external tool is unavailable or unusable."""


def parse_timestamp(value: str) -> datetime:
    """Parse an ISO 8601 timestamp from a Timeline export."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def parse_position(value: object) -> Position:
    """Parse either supported Google Timeline coordinate representation."""
    if isinstance(value, dict):
        value = value.get("latLng")
    if not isinstance(value, str):
        raise ValueError(f"unsupported coordinate value: {value!r}")
    match = GEO_RE.fullmatch(value)
    if not match:
        raise ValueError(f"unsupported coordinate format: {value!r}")
    return Position(float(match.group(1)), float(match.group(2)))


def load_timeline(path: Path) -> tuple[list[Visit], list[TimelinePoint]]:
    """Load visits and path points from an Android Timeline JSON export."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read Timeline JSON: {error}") from error

    segments = payload.get("semanticSegments")
    if not isinstance(segments, list):
        raise ValueError("Timeline JSON does not contain a semanticSegments list")

    visits: list[Visit] = []
    points: list[TimelinePoint] = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        try:
            if "visit" in segment:
                candidate = segment["visit"]["topCandidate"]
                visits.append(
                    Visit(
                        parse_timestamp(segment["startTime"]),
                        parse_timestamp(segment["endTime"]),
                        parse_position(candidate["placeLocation"]),
                    )
                )
            for item in segment.get("timelinePath", []):
                points.append(TimelinePoint(parse_timestamp(item["time"]), parse_position(item["point"])))
        except (KeyError, TypeError, ValueError):
            # Export formats occasionally contain incomplete semantic segments.
            continue

    return visits, sorted(points, key=lambda point: point.timestamp)


def haversine_km(first: Position, second: Position) -> float:
    """Return the great-circle distance between two coordinates in kilometres."""
    lat1, lon1, lat2, lon2 = map(math.radians, (first.latitude, first.longitude, second.latitude, second.longitude))
    a = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371.0088 * 2 * math.asin(math.sqrt(a))


def google_maps_url(position: Position) -> str:
    """Build the cross-platform Google Maps URL for an exact coordinate."""
    query = f"{position.latitude:.7f},{position.longitude:.7f}"
    return f"https://www.google.com/maps/search/?{urlencode({'api': '1', 'query': query})}"


def interpolate(first: TimelinePoint, second: TimelinePoint, target: datetime) -> Position:
    ratio = (target - first.timestamp).total_seconds() / (second.timestamp - first.timestamp).total_seconds()
    return Position(
        first.position.latitude + (second.position.latitude - first.position.latitude) * ratio,
        first.position.longitude + (second.position.longitude - first.position.longitude) * ratio,
    )


def find_match(
    captured_at: datetime,
    visits: list[Visit],
    points: list[TimelinePoint],
    max_gap: timedelta,
    max_distance_km: float,
) -> Match | None:
    """Find the safest available location for a capture time."""
    for visit in visits:
        if visit.start <= captured_at <= visit.end:
            return Match(visit.position, "visit")

    before: TimelinePoint | None = None
    after: TimelinePoint | None = None
    for point in points:
        if point.timestamp <= captured_at:
            before = point
            continue
        after = point
        break

    if before and after:
        gap = after.timestamp - before.timestamp
        if gap <= max_gap and haversine_km(before.position, after.position) <= max_distance_km:
            return Match(interpolate(before, after, captured_at), "interpolated")

    nearest = min(points, key=lambda point: abs(point.timestamp - captured_at), default=None)
    if nearest and abs(nearest.timestamp - captured_at) <= max_gap:
        return Match(nearest.position, "nearest")
    return None


def local_timezone(name: str | None) -> tzinfo:
    if name:
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError as error:
            raise ValueError(f"unknown timezone: {name}") from error
    return datetime.now().astimezone().tzinfo or ZoneInfo("UTC")


def parse_offset(value: str) -> timedelta:
    match = re.fullmatch(r"([+-]?)(\d+)(?::(\d{2})|([smhd]))?", value.strip())
    if not match:
        raise ValueError("time offset must be like -7m, +01:30, or 30s")
    sign, amount, clock_minutes, unit = match.groups()
    number = int(amount)
    if clock_minutes is not None:
        delta = timedelta(hours=number, minutes=int(clock_minutes))
    else:
        delta = timedelta(**{ {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}.get(unit or "s"): number })
    return -delta if sign == "-" else delta


def parse_duration(value: str) -> timedelta:
    match = re.fullmatch(r"(\d+)([smhd])", value.strip())
    if not match:
        raise ValueError("duration must be like 15m or 30s")
    amount, unit = match.groups()
    return timedelta(**{{"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}[unit]: int(amount)})


def photo_paths(directory: Path, recursive: bool) -> list[Path]:
    iterator = directory.rglob("*") if recursive else directory.glob("*")
    return sorted(path for path in iterator if path.is_file() and path.suffix.lower() in PHOTO_SUFFIXES)


def read_photo_metadata(path: Path) -> dict[str, object]:
    command = [
        "exiftool", "-j", "-n", "-DateTimeOriginal", "-CreateDate", "-ModifyDate", "-GPSLatitude", "-GPSLongitude", str(path),
    ]
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise ValueError(result.stderr.strip() or "exiftool failed")
    records = json.loads(result.stdout)
    return records[0] if records else {}


def ensure_exiftool() -> None:
    """Confirm exiftool is on PATH and can be executed before processing files."""
    executable = shutil.which("exiftool")
    if not executable:
        raise DependencyError("exiftool was not found on PATH. Install exiftool and try again.")
    try:
        result = subprocess.run([executable, "-ver"], check=False, capture_output=True, text=True)
    except OSError as error:
        raise DependencyError(f"exiftool could not be executed: {error}") from error
    if result.returncode != 0:
        detail = result.stderr.strip()
        suffix = f" ({detail})" if detail else ""
        raise DependencyError(f"exiftool failed its availability check{suffix}")


def capture_time(metadata: dict[str, object], timezone: tzinfo, offset: timedelta) -> datetime | None:
    for key in ("DateTimeOriginal", "CreateDate", "ModifyDate"):
        value = metadata.get(key)
        if not isinstance(value, str):
            continue
        try:
            return datetime.strptime(value, "%Y:%m:%d %H:%M:%S").replace(tzinfo=timezone) + offset
        except ValueError:
            continue
    return None


def has_gps(metadata: dict[str, object]) -> bool:
    return metadata.get("GPSLatitude") is not None and metadata.get("GPSLongitude") is not None


def write_gps(path: Path, position: Position, backup: bool) -> None:
    latitude_ref = "N" if position.latitude >= 0 else "S"
    longitude_ref = "E" if position.longitude >= 0 else "W"
    command = ["exiftool"]
    if not backup:
        command.append("-overwrite_original")
    command.extend([
        f"-GPSLatitude={abs(position.latitude):.7f}", f"-GPSLatitudeRef={latitude_ref}",
        f"-GPSLongitude={abs(position.longitude):.7f}", f"-GPSLongitudeRef={longitude_ref}", str(path),
    ])
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise ValueError(result.stderr.strip() or "exiftool failed")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="timeline-geotag",
        description="Geotag JPG and RW2 photos from an Android Google Maps Timeline export.",
    )
    parser.add_argument("photo_dir", type=Path, help="directory containing .JPG and .RW2 photos")
    parser.add_argument("timeline_json", type=Path, help="Android Google Maps Timeline JSON export")
    parser.add_argument("--write", action="store_true", help="write matched GPS coordinates with exiftool")
    parser.add_argument("--recursive", action="store_true", help="search photo directories recursively")
    parser.add_argument("--timezone", help="IANA timezone for timezone-less EXIF timestamps (default: system local timezone)")
    parser.add_argument("--time-offset", default="0s", help="camera clock adjustment, e.g. -7m or +01:30")
    parser.add_argument("--max-gap", default="15m", help="maximum timeline point gap / nearest age (default: 15m)")
    parser.add_argument("--max-distance", type=float, default=5.0, help="maximum interpolation endpoint distance in km (default: 5)")
    parser.add_argument("--overwrite-gps", action="store_true", help="replace existing GPS coordinates")
    backup = parser.add_mutually_exclusive_group()
    backup.add_argument("--backup", dest="backup", action="store_true", default=True, help="keep exiftool _original backups (default)")
    backup.add_argument("--no-backup", dest="backup", action="store_false", help="do not keep exiftool _original backups")
    parser.add_argument("--verbose", action="store_true", help="show detailed skips and matching diagnostics")
    return parser


def run(args: argparse.Namespace) -> int:
    ensure_exiftool()
    if not args.photo_dir.is_dir():
        raise ValueError(f"photo directory does not exist: {args.photo_dir}")
    if not args.timeline_json.is_file():
        raise ValueError(f"Timeline JSON does not exist: {args.timeline_json}")
    timezone = local_timezone(args.timezone)
    offset = parse_offset(args.time_offset)
    max_gap = parse_duration(args.max_gap)
    if args.max_distance < 0:
        raise ValueError("--max-distance must not be negative")
    visits, points = load_timeline(args.timeline_json)
    if not visits and not points:
        raise ValueError("Timeline JSON contains no usable visits or path points")

    photos = photo_paths(args.photo_dir, args.recursive)
    matched = written = unmatched = skipped = 0
    for path in photos:
        try:
            metadata = read_photo_metadata(path)
            taken_at = capture_time(metadata, timezone, offset)
            if taken_at is None:
                skipped += 1
                print(f"{path}\n  source:   skipped (no usable EXIF capture time)")
                continue
            if has_gps(metadata) and not args.overwrite_gps:
                skipped += 1
                print(f"{path}\n  taken_at: {taken_at.isoformat()}\n  source:   skipped (existing GPS)")
                continue
            match = find_match(taken_at, visits, points, max_gap, args.max_distance)
            if match is None:
                unmatched += 1
                print(f"{path}\n  taken_at: {taken_at.isoformat()}\n  gps:      -\n  source:   no-match")
                continue
            matched += 1
            print(
                f"{path}\n"
                f"  taken_at: {taken_at.isoformat()}\n"
                f"  gps:      {match.position.latitude:.6f}, {match.position.longitude:.6f}\n"
                f"  map:      {google_maps_url(match.position)}\n"
                f"  source:   {match.source}"
            )
            if args.write:
                write_gps(path, match.position, args.backup)
                written += 1
        except ValueError as error:
            skipped += 1
            print(f"{path}\n  source:   skipped ({error})", file=sys.stderr)

    print(f"\nmatched:   {matched}\nunmatched: {unmatched}\nskipped:   {skipped}\nwritten:   {written}")
    if not args.write:
        print("\nDry run. Use --write to update EXIF.")
    return 0


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    if raw_argv[:1] == ["completion"]:
        completion_parser = argparse.ArgumentParser(prog="timeline-geotag completion")
        completion_parser.add_argument("shell", choices=["bash"], help="shell to generate completion for")
        completion_args = completion_parser.parse_args(raw_argv[1:])
        print(shtab.complete(build_parser(), completion_args.shell), end="")
        return 0

    parser = build_parser()
    args = parser.parse_args(raw_argv)
    try:
        return run(args)
    except DependencyError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except ValueError as error:
        parser.error(str(error))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
