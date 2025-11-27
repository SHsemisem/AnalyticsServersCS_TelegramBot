import datetime as dt

import pytz

from TelegrammBot import (
    aggregate_analyzer_entries,
    format_analyzer_table,
    parse_analyzer_log,
)


SAMPLE_LOG = (
    "L 10/27/2025 - 00:00:37: fullserver.org           STEAM_2:0:1111111111  213.196.103.45   BABAROGA\n"
    "L 10/27/2025 - 01:15:00: gs-monitor.com (AC)      STEAM_2:0:2222222222  10.10.10.10      Some Nick\n"
    "L 10/27/2025 - 02:30:10: fullserver.org           STEAM_2:0:3333333333  213.196.103.45   Another Nick\n"
)


def test_parse_analyzer_log_extracts_entries():
    tz = pytz.timezone("UTC")
    entries = parse_analyzer_log(SAMPLE_LOG, tz)
    assert len(entries) == 3
    assert entries[0].source == "fullserver.org"
    assert entries[1].steam_id == "STEAM_2:0:2222222222"


def test_aggregate_and_format_analyzer_summary():
    tz = pytz.timezone("UTC")
    report_date = dt.date(2025, 10, 27)
    entries = parse_analyzer_log(SAMPLE_LOG, tz)
    summaries, total_events = aggregate_analyzer_entries(entries, report_date, tz)

    assert total_events == 3
    assert len(summaries) == 2  # два источника
    assert summaries[0].source == "fullserver.org"
    assert summaries[0].hits == 2
    assert summaries[0].unique_hits == 1
    assert summaries[1].unique_hits == 1

    table = format_analyzer_table(summaries, tz)
    lines = table.splitlines()
    assert lines[0].strip().startswith("site")
    assert lines[0].strip().endswith("unique")
    assert "fullserver.org" in lines[1]
    assert lines[1].split()[-2] == "2"  # колонка count
    assert lines[1].split()[-1] == "1"  # колонка unique
