from __future__ import annotations

from plex_queue.tidy_select import parse_batch, select_rows


def test_parse_batch_kinds():
    assert parse_batch("renames 1-50") == ("rename", "1-50")
    assert parse_batch("dupes") == ("trash", "")
    assert parse_batch("abcd1234") == (None, "abcd1234")


def test_select_count_and_range():
    rows = [{"id": str(i)} for i in range(1, 11)]
    chosen, err = select_rows(rows, "3")
    assert err is None
    assert [row["id"] for row in chosen] == ["1", "2", "3"]
    chosen, err = select_rows(rows, "2-4")
    assert err is None
    assert [row["id"] for row in chosen] == ["2", "3", "4"]
    chosen, err = select_rows(rows, "1,5,10")
    assert err is None
    assert [row["id"] for row in chosen] == ["1", "5", "10"]


def test_select_season_filter():
    rows = [
        {"id": "a", "title": "Show S01E01"},
        {"id": "b", "title": "Show S02E01"},
        {"id": "c", "title": "Show S01E02"},
    ]
    chosen, err = select_rows(rows, "s01")
    assert err is None
    assert [row["id"] for row in chosen] == ["a", "c"]
    chosen, err = select_rows(rows, "s01e02")
    assert err is None
    assert [row["id"] for row in chosen] == ["c"]
