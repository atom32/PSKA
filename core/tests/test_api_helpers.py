from __future__ import annotations

from pska_core.api import _string_list


def test_string_list_accepts_single_string_for_root_payloads() -> None:
    assert _string_list("/tmp/notes") == ["/tmp/notes"]
    assert _string_list(["/tmp/notes", ""]) == ["/tmp/notes"]
    assert _string_list(None) == []
