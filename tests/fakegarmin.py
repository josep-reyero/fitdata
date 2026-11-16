"""A stand-in for Garmin Connect, for tests: made-up answers, no network, no account.

FakeSession has the two things sync uses from client.Session (`api` and `call`). Every Garmin
method returns a small made-up dict; downloads return zips holding a real (tiny) FIT file built
by make_fit(), which Garmin's FIT SDK decodes.
"""
from __future__ import annotations

import io
import struct
import zipfile

FIT_EPOCH = 631065600  # 1989-12-31T00:00:00Z, the FIT time origin

_CRC_TABLE = [0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
              0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400]


def _crc(data: bytes, crc: int = 0) -> int:
    for byte in data:
        for nibble in (byte & 0xF, byte >> 4):
            tmp = _CRC_TABLE[crc & 0xF]
            crc = (crc >> 4) & 0x0FFF
            crc = crc ^ tmp ^ _CRC_TABLE[nibble]
    return crc


def make_fit(start_unix: int = 1_763_100_000, samples: int = 5, hr0: int = 100) -> bytes:
    """A valid FIT activity file: file_id, `samples` records (timestamp, heart_rate) and one lap."""
    t0 = start_unix - FIT_EPOCH
    body = bytearray()
    # local 0: file_id (global 0): type (enum) = 4 activity, time_created (uint32)
    body += bytes([0x40, 0, 0]) + struct.pack("<H", 0) + bytes([2, 0, 1, 0x00, 4, 4, 0x86])
    body += bytes([0x00, 4]) + struct.pack("<I", t0)
    # local 1: record (global 20): timestamp (uint32), heart_rate (uint8)
    body += bytes([0x41, 0, 0]) + struct.pack("<H", 20) + bytes([2, 253, 4, 0x86, 3, 1, 0x02])
    for i in range(samples):
        body += bytes([0x01]) + struct.pack("<I", t0 + i) + bytes([hr0 + i])
    # local 2: lap (global 19): timestamp, start_time, total_elapsed_time (ms)
    body += bytes([0x42, 0, 0]) + struct.pack("<H", 19) + bytes([3, 253, 4, 0x86, 2, 4, 0x86, 7, 4, 0x86])
    body += bytes([0x02]) + struct.pack("<III", t0 + samples, t0, samples * 1000)
    header = bytearray([14, 0x20]) + struct.pack("<HI", 2132, len(body)) + b".FIT"
    header += struct.pack("<H", _crc(bytes(header)))
    out = bytes(header) + bytes(body)
    return out + struct.pack("<H", _crc(out))


def zip_of(*files: tuple[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files:
            z.writestr(name, data)
    return buf.getvalue()


class FakeApi:
    """Answers every Garmin Connect call with made-up data."""

    class ActivityDownloadFormat:
        ORIGINAL = "original"

    def __init__(self, activities: list[dict] | None = None) -> None:
        self.activities = activities or []
        self.calls: list[str] = []

    def get_user_summary(self, d):
        return {"calendarDate": d, "totalSteps": 4321, "restingHeartRate": 52, "totalKilocalories": 2100}

    def get_sleep_data(self, d):
        return {"dailySleepDTO": {"calendarDate": d, "sleepTimeSeconds": 25200,
                                  "sleepScores": {"overall": {"value": 81}}}}

    def get_hrv_data(self, d):
        return {"hrvSummary": {"lastNightAvg": 48, "status": "BALANCED"}}

    def get_activities_by_date(self, start, end, sortorder="asc"):
        return [a for a in self.activities if start <= a["startTimeLocal"][:10] <= end]

    def get_activity(self, i):
        a = next(x for x in self.activities if x["activityId"] == i)
        return {"activityId": i, "activityName": a.get("activityName"), "activityTypeDTO": a["activityType"],
                "summaryDTO": {"startTimeLocal": a["startTimeLocal"], "duration": 1800.0, "distance": 5000.0}}

    def download_health_snapshot(self, d):
        return zip_of((f"{d.replace('-', '')}_WELLNESS.fit", make_fit(samples=3, hr0=60)))

    def download_activity(self, i, dl_fmt=None):
        return zip_of((f"{i}_ACTIVITY.fit", make_fit(samples=5, hr0=120)))

    def __getattr__(self, name):
        if name.startswith("get_"):
            return lambda *a, **k: {"source": name, "args": [str(x) for x in a]}
        raise AttributeError(name)


class FakeSession:
    def __init__(self, api: FakeApi | None = None, fail_with: Exception | None = None) -> None:
        self.api = api or FakeApi()
        self.calls = 0
        self.fail_with = fail_with

    def connect(self):
        return self

    def call(self, fn, *args, **kwargs):
        self.calls += 1
        if self.fail_with is not None:
            raise self.fail_with
        return fn(*args, **kwargs)


def activity(aid: int, start_local: str, type_key: str = "running", name: str = "Made-up run") -> dict:
    return {"activityId": aid, "activityName": name, "startTimeLocal": start_local,
            "activityType": {"typeKey": type_key}}
