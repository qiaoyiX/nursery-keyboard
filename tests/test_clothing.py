"""The clothing rules: band edges, modifiers, and the two safety notes.

What is worth pinning here is what would be wrong on a real morning: an edge
temperature landing in the warmer band (she goes out underdressed), a sun hat at
9pm, wind chill counted twice, or the car-seat warning going missing on the one day
she is actually in a bunting.

Run:  venv/bin/python tests/test_clothing.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import clothing  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def ids(outfit):
    return [g["id"] for g in outfit["garments"]]


def caveat_ids(outfit):
    return [c["id"] for c in outfit["caveats"]]


def test_bands():
    print("temperature bands")
    check("90° is a onesie", ids(clothing.outfit_for(90)) == ["bodysuit_short", "sun_hat"])
    check("70° adds long sleeves", "bodysuit_long" in ids(clothing.outfit_for(70)))
    check("60° adds a cardigan", "cardigan" in ids(clothing.outfit_for(60)))
    check("50° adds fleece + hat",
          {"fleece", "knit_hat"} <= set(ids(clothing.outfit_for(50))))
    check("40° adds a bunting", "bunting" in ids(clothing.outfit_for(40)))
    check("20° adds a blanket", "blanket" in ids(clothing.outfit_for(20)))

    # Every boundary belongs to the warmer band; one degree below drops a band.
    for floor, band_id, _, _ in clothing.BANDS[:-1]:
        check(f"{floor}° is {band_id}", clothing.band_for(floor)[0] == band_id)
        check(f"{floor - 1}° is not {band_id}", clothing.band_for(floor - 1)[0] != band_id)


def test_dressing_order():
    print("garments come out in dressing order")
    order = ids(clothing.outfit_for(40))
    check("inner layer first", order[0] == "bodysuit_long", order)
    check("bunting before accessories", order.index("bunting") < order.index("knit_hat"), order)
    check("no duplicates", len(order) == len(set(order)), order)


def test_sun_and_night():
    print("sun gear follows the sun")
    check("sun hat by day", "sun_hat" in ids(clothing.outfit_for(80, 0, 0, 3, True)))
    check("no sun hat after dark", "sun_hat" not in ids(clothing.outfit_for(80, 0, 0, 0, False)))
    hi_uv = clothing.outfit_for(70, 0, 0, 8, True)
    check("high UV adds the hat back", "sun_hat" in ids(hi_uv))
    check("high UV warns about shade", "uv" in caveat_ids(hi_uv))
    check("high UV at night does nothing",
          "uv" not in caveat_ids(clothing.outfit_for(70, 0, 0, 8, False)))


def test_wet():
    print("rain and snow")
    rain = clothing.outfit_for(60, 63)
    check("rain code adds a cover", "rain_cover" in ids(rain))
    check("rain is called out", "wet" in caveat_ids(rain))
    snow = clothing.outfit_for(30, 73)
    check("snow adds a cover too", "rain_cover" in ids(snow))
    wet_chip = next(c for c in snow["caveats"] if c["id"] == "wet")
    check("snow says snow", wet_chip["text"].startswith("Snow"))
    check("clear sky stays dry", "rain_cover" not in ids(clothing.outfit_for(60, 0)))
    check("thunder counts as wet", clothing.is_wet(95))
    check("fog does not", not clothing.is_wet(45))


def test_wind_is_not_double_counted():
    print("wind")
    windy = clothing.outfit_for(72, 0, 22, 0, True)
    check("a windbreak appears when she has no mid layer", "cardigan" in ids(windy))
    check("wind is called out", "wind" in caveat_ids(windy))
    # She is already in a fleece at 50°; the wind chill is already inside the
    # feels-like number, so it must not push her up another layer as well.
    cold_windy = clothing.outfit_for(50, 0, 22, 0, True)
    check("no extra layer on top of a fleece",
          ids(cold_windy) == ids(clothing.outfit_for(50, 0, 0, 0, True)),
          ids(cold_windy))
    check("calm days say nothing about wind",
          "wind" not in caveat_ids(clothing.outfit_for(72, 0, 4, 0, True)))


def test_car_seat_warning():
    print("car-seat safety note")
    check("bunting warns", "carseat" in caveat_ids(clothing.outfit_for(38)))
    check("fleece warns", "carseat" in caveat_ids(clothing.outfit_for(50)))
    check("cardigan does not", "carseat" not in caveat_ids(clothing.outfit_for(60)))


def test_safety_caveat_comes_first():
    """It is the only caveat about harm rather than comfort, so it cannot sit last."""
    print("the car-seat warning is read first")
    # Freezing rain: wet, wind and carseat all fire at once.
    out = clothing.outfit_for(30, 63, 22, 0, True)
    ids = caveat_ids(out)
    check("all three fire", set(ids) == {"wet", "wind", "carseat"}, ids)
    check("safety is first", ids[0] == "carseat", ids)
    check("the others keep their order", ids[1:] == ["wet", "wind"], ids)


def test_missing_data():
    print("missing readings never blank the page")
    out = clothing.outfit_for(None, None, None, None, True)
    check("a garment is still recommended", len(out["garments"]) > 0)
    check("band is real", out["band"] in {b[1] for b in clothing.BANDS})
    check("every id has a caption", all(g["id"] in clothing.GARMENTS for g in out["garments"]))


def test_sleepwear():
    print("sleepwear")
    warm = clothing.sleepwear_for(70, 78)
    check("a warm nursery is 0.5 TOG", warm["tog"] == "0.5", warm)
    check("0.5 TOG over short sleeves",
          [g["id"] for g in warm["garments"]] == ["bodysuit_short", "sack_05"])
    check("70° nursery is 1.0 TOG", clothing.sleepwear_for(60, 70)["tog"] == "1.0")
    check("a cool nursery is 2.5 TOG", clothing.sleepwear_for(50, 66)["tog"] == "2.5")

    # A freezing night pulls a 69° room under the 1.0 TOG line by morning.
    drifted = clothing.sleepwear_for(30, 69)
    check("a freezing night drifts the room down", drifted["tog"] == "2.5", drifted)
    check("drift is reported", drifted["drift"] == -2 and drifted["room_estimate"] == 67)
    check("safe-sleep note is always there", clothing.SAFE_SLEEP in drifted["note"])
    check("no overnight low is still answerable",
          clothing.sleepwear_for(None, 70)["tog"] == "1.0")


def test_conditions():
    print("weather codes")
    for code, want in [(0, "clear"), (1, "clear"), (2, "cloud"), (3, "cloud"), (45, "fog"),
                       (53, "rain"), (65, "rain"), (81, "rain"), (71, "snow"), (86, "snow"),
                       (95, "storm"), (None, "cloud")]:
        check(f"code {code} is {want}", clothing.condition_for(code) == want,
              clothing.condition_for(code))
    check("every condition has a label",
          all(c in clothing.CONDITIONS for c in
              {clothing.condition_for(c) for c in range(0, 100)}))


def main():
    print("Clothing rules:")
    for fn in (test_bands, test_dressing_order, test_sun_and_night, test_wet,
               test_wind_is_not_double_counted, test_car_seat_warning,
               test_safety_caveat_comes_first,
               test_missing_data, test_sleepwear, test_conditions):
        fn()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {', '.join(FAILURES)}")
        sys.exit(1)
    print("All clothing-rule tests passed.")


if __name__ == "__main__":
    main()
