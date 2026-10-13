from pathlib import Path

from mailru_bridge.cache import Cache


def _cache(tmp_path) -> Cache:
    return Cache(Path(tmp_path) / "c.sqlite3")


def test_uid_assignment_is_stable_and_monotonic(tmp_path):
    c = _cache(tmp_path)
    c.upsert_folder("0", "INBOX", "-1", "inbox", True, 0, 0)
    u1 = c.add_message("0", "100", 10, 500, {"\\Seen"}, {"subject": "a"})
    u2 = c.add_message("0", "200", 20, 600, set(), {"subject": "b"})
    assert (u1, u2) == (1, 2)
    # re-adding the same mailru id keeps the uid and refreshes flags
    u1b = c.add_message("0", "100", 10, 500, set(), {"subject": "a"})
    assert u1b == 1
    msg = c.message_by_uid("0", 1)
    assert msg.flags == set()
    # uidnext advanced past the two assigned
    assert c.folder("0")["uidnext"] == 3


def test_counts_and_flags(tmp_path):
    c = _cache(tmp_path)
    c.upsert_folder("0", "INBOX", "-1", "inbox", True, 0, 0)
    c.add_message("0", "1", 1, 1, {"\\Seen"}, {})
    c.add_message("0", "2", 2, 2, set(), {})
    exists, unseen = c.counts("0")
    assert exists == 2 and unseen == 1
    c.update_flags("0", 2, {"\\Seen", "\\Flagged"})
    assert c.counts("0") == (2, 0)


def test_raw_roundtrip_and_forget(tmp_path):
    c = _cache(tmp_path)
    c.upsert_folder("0", "INBOX", "-1", "inbox", True, 0, 0)
    uid = c.add_message("0", "1", 1, 0, set(), {})
    assert c.get_raw("0", uid) is None
    c.set_raw("0", uid, b"RFC822 DATA")
    assert c.get_raw("0", uid) == b"RFC822 DATA"
    assert c.message_by_uid("0", uid).size == len(b"RFC822 DATA")
    c.forget("0", [uid])
    assert c.message_by_uid("0", uid) is None


def test_live_ids(tmp_path):
    c = _cache(tmp_path)
    c.upsert_folder("5", "F", "-1", "user", False, 0, 0)
    c.add_message("5", "a", 1, 0, set(), {})
    c.add_message("5", "b", 2, 0, set(), {})
    assert c.live_mailru_ids("5") == {"a", "b"}


def test_move_over_existing_destination_refreshes_source_and_keeps_destination_uid(tmp_path):
    c = _cache(tmp_path)
    try:
        for fid in ("0", "1"):
            c.upsert_folder(fid, fid, "-1", "user", False, 0, 0)
        src = c.add_message("0", "100", 20, 1, {"\\Seen", "\\Deleted"}, {"subject": "current"})
        dst = c.add_message("1", "100", 10, 1, set(), {"subject": "stale"})
        raw = b"Subject: current\r\n\r\nbody"
        c.set_raw("0", src, raw)
        assert c.move_messages("0", "1", [src]) == {src: dst}
        assert c.get_raw("1", dst) == raw
        assert c.message_by_uid("1", dst).flags == {"\\Seen"}
        assert c.message_by_uid("1", dst).meta["subject"] == "current"
        assert c.messages("0") == []
    finally:
        c.close()


def test_multi_message_move_rolls_back_on_destination_uid_conflict(tmp_path):
    import sqlite3
    import pytest
    c = _cache(tmp_path)
    try:
        for fid in ("0", "1"):
            c.upsert_folder(fid, fid, "-1", "user", False, 0, 0)
        first = c.add_message("0", "100", 1, 1, set(), {})
        second = c.add_message("0", "200", 2, 1, set(), {})
        c.add_message("1", "300", 3, 1, set(), {})
        c.add_message("1", "400", 4, 1, set(), {})
        c.forget("1", [1])
        # Simulate corrupted bookkeeping: first insert succeeds but second would collide.
        c._conn.execute("UPDATE folders SET uidnext=1 WHERE id='1'")
        c._conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            c.move_messages("0", "1", [first, second])
        assert c.live_mailru_ids("0") == {"100", "200"}
        assert c.live_mailru_ids("1") == {"400"}
    finally:
        c.close()


def test_client_flags_survive_refresh_restart_and_move(tmp_path):
    c = _cache(tmp_path)
    for fid in ("0", "9"):
        c.upsert_folder(fid, fid, "-1", "user", False, 0, 0)
    uid = c.add_message("0", "100", 1, 1, {"\\Draft"}, {})
    wanted = {"\\Answered", "$Forwarded", "$label1", "\\Deleted"}
    c.update_client_flags("0", uid, wanted)
    c.set_raw("0", uid, b"Message-ID: <original@example.com>\r\n\r\nbody")
    c.close()
    c = _cache(tmp_path)
    try:
        # Exercise all refresh paths, including replacement summaries and removals.
        c.add_message("0", "100", 1, 1, {"\\Draft"}, {"subject": "current"})
        c.update_remote_flags("0", uid, {"\\Draft"})
        c.update_remote_flags_batch("0", {"100": {"\\Draft"}})
        c.update_remote_messages_batch("0", [("100", 1, 1, {"\\Draft"}, {})])
        assert c.message_by_uid("0", uid).flags == wanted
        assert c.mailru_id_for_message_id("<original@example.com>") == "100"
        mapping = c.move_messages("0", "9", [uid])
        c.update_remote_messages_batch("9", [("100", 1, 1, set(), {})])
        assert c.message_by_uid("9", mapping[uid]).flags == wanted - {"\\Deleted"}
        assert "\\Draft" not in c.message_by_uid("9", mapping[uid]).meta["flag_overrides"]
    finally:
        c.close()


def test_client_removal_of_remote_reply_marker_survives_refresh(tmp_path):
    c = _cache(tmp_path)
    try:
        c.upsert_folder("0", "INBOX", "-1", "inbox", True, 0, 0)
        c.add_message("0", "100", 1, 1, {"\\Answered", "$Forwarded"}, {})
        c.update_client_flags("0", 1, set())
        c.update_remote_flags("0", 1, {"\\Answered", "$Forwarded", "\\Seen"})
        assert c.message_by_uid("0", 1).flags == {"\\Seen"}
    finally:
        c.close()


def test_message_id_lookup_does_not_guess_on_ambiguous_or_missing_headers(tmp_path):
    c = _cache(tmp_path)
    try:
        c.upsert_folder("0", "INBOX", "-1", "inbox", True, 0, 0)
        for mid in ("100", "200"):
            uid = c.add_message("0", mid, 1, 1, set(), {})
            c.set_raw("0", uid, b"Message-ID: <same@example.com>\r\n\r\nbody")
        assert c.mailru_id_for_message_id("<same@example.com>") is None
        assert c.mailru_id_for_message_id("<missing@example.com>") is None
        assert c.mailru_id_for_message_id(None) is None
        c.forget("0", [2])
        assert c.mailru_id_for_message_id(" <same@example.com> ") == "100"
    finally:
        c.close()
