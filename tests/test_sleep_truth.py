"""The parent's verdicts on what the detectors claimed.

These labels are the only ground truth in the system — the crib monitor's stillness
guess and the vision model's phone guess are both uncheckable otherwise, and the
model's own confidence is worthless (every phone event on 2026-08-28 was "high", and
several were a bottle held during a feed). So what matters here is that a verdict,
once given, is never silently lost: not by an unknown value, not by a file written
before the feature existed.

Run:  venv/bin/python tests/test_sleep_truth.py
"""
import json
import os
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import storage  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def fresh():
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    os.remove(path)
    storage.SLEEP_TRUTH_FILE = path
    return path


def test_phone_verdicts():
    print("phone verdicts")
    fresh()
    key = "2026-08-28T10:13:23"
    rec = storage.set_phone_verdict(key, "not_a_phone")
    check("stored", rec and rec["verdict"] == "not_a_phone", str(rec))
    check("readable", storage.get_sleep_truth()["phone"][key]["verdict"] == "not_a_phone")

    check("unknown verdict rejected", storage.set_phone_verdict(key, "maybe") is None)
    check("unparseable key rejected", storage.set_phone_verdict("not-a-time", "correct") is None)
    check("empty key rejected", storage.set_phone_verdict("", "correct") is None)
    # A rejected write must not damage the good one already there.
    check("a rejected write leaves the stored verdict alone",
          storage.get_sleep_truth()["phone"][key]["verdict"] == "not_a_phone")

    storage.set_phone_verdict(key, "correct")
    check("re-labelling overwrites",
          storage.get_sleep_truth()["phone"][key]["verdict"] == "correct")


def test_sleep_and_phone_share_the_file():
    print("sleep and phone verdicts coexist")
    fresh()
    storage.set_sleep_verdict("2026-08-27T01:05:00", "false_alarm")
    storage.set_phone_verdict("2026-08-27T13:03:12", "not_a_phone")
    storage.add_missed_sleep("2026-08-27T15:00:00", "2026-08-27T16:00:00")
    t = storage.get_sleep_truth()
    check("all three kinds survive one file",
          len(t["verdicts"]) == 1 and len(t["phone"]) == 1 and len(t["missed"]) == 1,
          str({k: len(v) for k, v in t.items()}))


def test_a_file_written_before_phone_existed():
    print("older truth files upgrade in place")
    path = fresh()
    # Exactly what sleep_truth.json looked like before phone verdicts — no "phone".
    with open(path, "w") as f:
        json.dump({"verdicts": {"2026-08-01T01:00:00": {"verdict": "correct"}},
                   "missed": []}, f)
    t = storage.get_sleep_truth()
    check("loads without error", isinstance(t, dict))
    check("phone key materialises", t.get("phone") == {}, str(t.get("phone")))
    check("existing sleep verdict untouched", len(t["verdicts"]) == 1)
    # And writing a phone verdict onto it must not drop what was already there.
    storage.set_phone_verdict("2026-08-01T10:00:00", "correct")
    t2 = storage.get_sleep_truth()
    check("old verdicts survive the first phone write",
          len(t2["verdicts"]) == 1 and len(t2["phone"]) == 1)


def test_corrupt_file_recovers():
    print("a corrupt file does not take the page down")
    path = fresh()
    with open(path, "w") as f:
        f.write("{not json")
    check("reads as empty", storage.get_sleep_truth()["phone"] == {})
    check("and accepts the next write",
          storage.set_phone_verdict("2026-08-28T10:00:00", "correct") is not None)


def main():
    print("Sleep/phone truth store:")
    original = storage.SLEEP_TRUTH_FILE
    try:
        for fn in (test_phone_verdicts, test_sleep_and_phone_share_the_file,
                   test_a_file_written_before_phone_existed, test_corrupt_file_recovers):
            fn()
    finally:
        storage.SLEEP_TRUTH_FILE = original
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {', '.join(FAILURES)}")
        sys.exit(1)
    print("All truth-store tests passed.")


if __name__ == "__main__":
    main()
