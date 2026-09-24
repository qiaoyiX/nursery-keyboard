"""What to put on her, given a temperature.

Pure functions only — no network, no files, no clock. Everything the rules need is a
parameter, so the whole layer is testable offline (`tests/test_clothing.py`) and the
page can re-run it for any hour of the forecast, not just now.

Two conventions drive the numbers:

  * **Feels-like, never the raw temperature.** `weather.feels_like()` gives the NWS
    number (wind chill ≤50°F, heat index ≥80°F), which already folds in the wind when it
    matters. Adding a separate wind penalty on top would double-count it, which is how
    a 55°F breezy day ends up recommending a snowsuit. Wind only earns a *caveat* here,
    plus a windbreak when she has no mid layer at all.
  * **One more layer than a comfortable adult.** The standard infant rule: a baby
    cannot shiver efficiently, cannot move to warm up, and cannot tell you. The bands
    below are therefore shifted one layer warmer than what an adult would wear.

The output is garment *ids*, not sentences. The ids match the `<symbol id="ic-…">`
sprite in `templates/_clothes_icons.html` — the page draws pictures, and the label is
only there for the caption, the aria-label, and the dashboard teaser.
"""

# ── Garments ──────────────────────────────────────────────────────────────────
# id → caption word. The caption is deliberately one or two words: it confirms the
# picture, it does not replace it.
GARMENTS = {
    "bodysuit_short": "Short-sleeve onesie",
    "bodysuit_long":  "Long-sleeve onesie",
    "romper":         "Romper",
    "pants":          "Pants",
    "cardigan":       "Cardigan",
    "fleece":         "Fleece jacket",
    "bunting":        "Bunting",
    "rain_cover":     "Rain cover",
    "sun_hat":        "Sun hat",
    "knit_hat":       "Warm hat",
    "mittens":        "Mittens",
    "socks":          "Socks",
    "booties":        "Booties",
    "blanket":        "Blanket",
    "sleeper_footed": "Footed sleeper",
    "sack_05":        "0.5 TOG sack",
    "sack_10":        "1.0 TOG sack",
    "sack_25":        "2.5 TOG sack",
}

# Dressing order: inner layer first, accessories last. The page renders the tiles in
# this order so the grid reads as "put this on, then this".
LAYER_ORDER = [
    "bodysuit_short", "bodysuit_long", "romper", "sleeper_footed", "pants",
    "cardigan", "fleece", "bunting", "rain_cover",
    "sun_hat", "knit_hat", "mittens", "socks", "booties", "blanket",
    "sack_05", "sack_10", "sack_25",
]

# ── Temperature bands, in feels-like °F ───────────────────────────────────────
# (floor, id, headline, garments). Read top-down; the first floor she clears wins.
BANDS = [
    (85, "hot",      "Just a onesie",            ["bodysuit_short", "sun_hat"]),
    (75, "warm",     "Onesie and light pants",   ["bodysuit_short", "pants", "sun_hat"]),
    (65, "mild",     "Long sleeves and pants",   ["bodysuit_long", "pants", "socks"]),
    (55, "cool",     "Add a cardigan",           ["bodysuit_long", "pants", "cardigan", "socks"]),
    (45, "chilly",   "Fleece and a warm hat",    ["bodysuit_long", "pants", "fleece", "knit_hat", "socks"]),
    (35, "cold",     "Bundle her up",            ["bodysuit_long", "pants", "fleece", "bunting",
                                                  "knit_hat", "mittens", "booties"]),
    (-99, "freezing", "Short trips only",        ["bodysuit_long", "pants", "fleece", "bunting",
                                                  "knit_hat", "mittens", "booties", "blanket"]),
]

# Layers that make a car-seat harness unsafe: a puffy layer compresses in a crash and
# leaves the straps inches loose. Buckle her in first, coat over the top.
BULKY = {"fleece", "bunting"}

# ── WMO weather codes → the condition we draw ─────────────────────────────────
# Open-Meteo speaks WMO 4677. We only need six buckets, because six is all a glance
# can hold.
CONDITIONS = {
    "clear": "Clear",  "cloud": "Cloudy", "fog": "Foggy",
    "rain":  "Rain",   "snow":  "Snow",   "storm": "Storms",
}


def condition_for(code):
    """WMO weather code → one of CONDITIONS' keys."""
    if code is None:
        return "cloud"
    code = int(code)
    if code in (0, 1):
        return "clear"
    if code in (2, 3):
        return "cloud"
    if code in (45, 48):
        return "fog"
    if code in (71, 73, 75, 77, 85, 86):
        return "snow"
    if code in (95, 96, 99):
        return "storm"
    if 51 <= code <= 67 or 80 <= code <= 82:
        return "rain"
    return "cloud"


def is_wet(code):
    """Does this code mean water falling out of the sky right now?"""
    return condition_for(code) in ("rain", "snow", "storm")


def band_for(feels_like_f):
    """Feels-like °F → (band id, headline, base garment list). Never returns None."""
    if feels_like_f is None:
        feels_like_f = 68  # a neutral indoor-ish guess beats a blank page
    for floor, band_id, headline, garments in BANDS:
        if feels_like_f >= floor:
            return band_id, headline, list(garments)
    return BANDS[-1][1], BANDS[-1][2], list(BANDS[-1][3])


def outfit_for(feels_like_f, weather_code=None, wind_mph=0, uv=0, is_day=True):
    """The going-out answer for one moment.

    Returns {band, headline, garments: [{id, label}], caveats: [{id, icon, text}]}.
    """
    band_id, headline, garments = band_for(feels_like_f)
    caveats = []

    # Sun gear is for the sun. After dark it is just a hat she'll pull off.
    if not is_day and "sun_hat" in garments:
        garments.remove("sun_hat")

    if is_wet(weather_code):
        garments.append("rain_cover")
        wet = "Snow" if condition_for(weather_code) == "snow" else "Rain"
        caveats.append({"id": "wet", "icon": "☔️",
                        "text": f"{wet} — cover the stroller, keep her feet dry."})

    # A windbreak only matters if nothing else is blocking it. Above 65°F she has no
    # mid layer at all, so a gust goes straight through the onesie.
    if (wind_mph or 0) >= 15:
        if not ({"cardigan", "fleece", "bunting"} & set(garments)):
            garments.append("cardigan")
        caveats.append({"id": "wind", "icon": "🌬",
                        "text": f"Windy ({round(wind_mph)} mph) — something windproof on top."})

    if is_day and (uv or 0) >= 6:
        if "sun_hat" not in garments:
            garments.append("sun_hat")
        caveats.append({"id": "uv", "icon": "🕶",
                        "text": "Strong sun — shade the stroller, skip the midday walk."})

    if BULKY & set(garments):
        caveats.append({"id": "carseat", "icon": "🚗",
                        "text": "No puffy coat under the car-seat harness — buckle first, coat over the top."})

    # The safety note is appended last but must be read first: it is the only caveat
    # that is about harm rather than comfort.
    caveats.sort(key=lambda c: 0 if c["id"] == "carseat" else 1)

    garments = order_garments(garments)
    return {
        "band": band_id,
        "headline": headline,
        "feels_like": None if feels_like_f is None else round(feels_like_f),
        "garments": [{"id": g, "label": GARMENTS[g]} for g in garments],
        "caveats": caveats,
    }


def order_garments(ids):
    """De-duplicate and sort into dressing order."""
    seen = []
    for g in LAYER_ORDER:
        if g in ids and g not in seen:
            seen.append(g)
    # Anything unknown keeps its original position at the end rather than vanishing.
    for g in ids:
        if g not in seen:
            seen.append(g)
    return seen


# ── Sleepwear ─────────────────────────────────────────────────────────────────
# Keyed on the *nursery* temperature, not the outdoor one — she sleeps indoors. The
# overnight low only matters because a room drifts toward it by 4am.
SLEEP_BANDS = [
    (75, "0.5", "sack_05", "bodysuit_short"),
    (69, "1.0", "sack_10", "bodysuit_long"),
    (-99, "2.5", "sack_25", "sleeper_footed"),
]

SAFE_SLEEP = "No loose blankets, no hat indoors, nothing over her face."


def sleepwear_for(overnight_low_f, nursery_temp_f=70):
    """Tonight's answer: a sack weight and the layer under it."""
    room = nursery_temp_f if nursery_temp_f is not None else 70
    drift = 0
    if overnight_low_f is not None:
        # An unheated room follows the outside down, and overshoots a hot night up.
        if overnight_low_f < 45:
            drift = -2
        elif overnight_low_f > 75:
            drift = +2
    effective = room + drift

    for floor, tog, sack, under in SLEEP_BANDS:
        if effective >= floor:
            break

    return {
        "tog": tog,
        "room_estimate": round(effective),
        "drift": drift,
        "garments": [{"id": g, "label": GARMENTS[g]} for g in order_garments([under, sack])],
        "headline": f"{tog} TOG sack over a {GARMENTS[under].lower()}",
        "note": SAFE_SLEEP,
    }
