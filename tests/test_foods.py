"""The persistent food list behind the dashboard's Foods card.

This list is the allergy record — what she has tried and when first — so the things
worth pinning are the ones that would quietly corrupt it: a food entered twice under
different capitalisation becoming two records (and resetting first_tried), or a
first_tried date being overwritten on a later offering.

Run:  venv/bin/python tests/test_foods.py
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import storage  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def fresh():
    """Point storage at an empty foods file for one test."""
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    os.remove(path)
    storage.FOODS_FILE = path
    return path


def test_create_then_increment():
    print("a food is created once, then counted")
    fresh()
    t0 = datetime(2026, 8, 14, 12, 30)
    rec, created = storage.add_food("avocado", now=t0)
    check("first offering creates", created and rec["times_offered"] == 1)
    check("first_tried recorded", rec["first_tried"] == t0.isoformat())

    rec2, created2 = storage.add_food("avocado", now=t0 + timedelta(days=1))
    check("second offering increments", not created2 and rec2["times_offered"] == 2)
    # The whole point of the list: first_tried is the allergy-watch anchor and must
    # survive every later offering.
    check("first_tried is not overwritten", rec2["first_tried"] == t0.isoformat(),
          rec2["first_tried"])
    check("last_offered moves", rec2["last_offered"] == (t0 + timedelta(days=1)).isoformat())
    check("still one record", len(storage.get_foods()) == 1)


def test_name_matching_is_forgiving():
    print("the same food typed differently is the same food")
    fresh()
    storage.add_food("Avocado")
    for variant in ("avocado", "  avocado  ", "AVOCADO", "avocado\t"):
        storage.add_food(variant)
    foods = storage.get_foods()
    check("case and whitespace variants merge", len(foods) == 1, f"{len(foods)} records")
    check("all five counted", foods[0]["times_offered"] == 5, str(foods[0]["times_offered"]))
    # Stored as first typed, not normalised — the card should read the way the
    # parent wrote it.
    check("original capitalisation kept", foods[0]["name"] == "Avocado", foods[0]["name"])
    # Interior whitespace collapses, so these are one food, not two.
    storage.add_food("sweet   potato")
    storage.add_food("sweet potato")
    check("interior whitespace collapses", len(storage.get_foods()) == 2)


def test_bad_names_rejected():
    print("unusable names never reach the list")
    fresh()
    for bad in ("", "   ", "\n\t ", None, 42, "x" * 41):
        rec, created = storage.add_food(bad)
        check(f"rejected {bad!r}"[:44], rec is None and not created)
    check("nothing stored", storage.get_foods() == [])
    ok, _ = storage.add_food("x" * 40)
    check("40 chars is allowed", ok is not None)


def test_reaction_notes():
    print("reactions can be attached, edited and cleared")
    fresh()
    storage.add_food("egg")
    rec = storage.set_food_reaction("EGG", "  mild rash  ")
    check("matched case-insensitively", rec is not None)
    check("note normalised", rec and rec["reaction"] == "mild rash", str(rec))
    check("persisted", storage.get_foods()[0]["reaction"] == "mild rash")
    cleared = storage.set_food_reaction("egg", "")
    check("empty note clears", cleared["reaction"] is None)
    check("unknown food returns None", storage.set_food_reaction("kiwi", "x") is None)


def test_ordering_and_missing_file():
    print("listing survives an absent or corrupt file")
    path = fresh()
    check("no file yet reads as empty", storage.get_foods() == [])
    t0 = datetime(2026, 8, 14, 9, 0)
    storage.add_food("oatmeal", now=t0)
    storage.add_food("banana", now=t0 + timedelta(hours=2))
    check("most recently offered first",
          [f["name"] for f in storage.get_foods()] == ["banana", "oatmeal"])
    # A corrupt file must not take the dashboard down with it.
    with open(path, "w") as f:
        f.write("{not json")
    check("corrupt file reads as empty", storage.get_foods() == [])
    rec, created = storage.add_food("pear")
    check("and recovers on the next write", created and len(storage.get_foods()) == 1)


def main():
    print("Food list:")
    original = storage.FOODS_FILE
    try:
        for fn in (test_create_then_increment, test_name_matching_is_forgiving,
                   test_bad_names_rejected, test_reaction_notes,
                   test_ordering_and_missing_file):
            fn()
    finally:
        storage.FOODS_FILE = original
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {', '.join(FAILURES)}")
        sys.exit(1)
    print("All food-list tests passed.")


if __name__ == "__main__":
    main()
