"""
Fire-and-forget Huckleberry sync. Called after each local DB write.
Uses py-huckleberry-api (unofficial Firebase client for Huckleberry's Firebase backend).

Required settings.json keys:
  huckleberry_email        — Huckleberry account email
  huckleberry_password     — Huckleberry account password
  huckleberry_child_index  — 0-based index of child in account (default 0)

Leave email/password empty to disable sync silently.
"""

import asyncio
import logging
import threading
from datetime import datetime

import aiohttp
from huckleberry_api import HuckleberryAPI

from storage import load_settings

_DIAPER_MODE = {"Wet": "pee", "Dirty": "poo"}


def _make_api(settings, session):
    tz = settings.get("huckleberry_timezone", "America/New_York")
    return HuckleberryAPI(
        email=settings["huckleberry_email"].strip(),
        password=settings["huckleberry_password"].strip(),
        websession=session,
        timezone=tz,
    )


def _run_async(coro):
    """Spawn a daemon thread to run one async coroutine, then exit."""
    def _target():
        try:
            asyncio.run(coro)
        except Exception as exc:
            logging.warning("Huckleberry sync failed: %s", exc)
    threading.Thread(target=_target, daemon=True).start()


async def _push_event_async(event_type: str, timestamp: datetime) -> None:
    settings = load_settings()
    email    = settings.get("huckleberry_email", "")
    password = settings.get("huckleberry_password", "")
    if not email or not password:
        return

    async with aiohttp.ClientSession() as session:
        api = _make_api(settings, session)
        await api.authenticate()
        user  = await api.get_user()
        child = user.childList[int(settings.get("huckleberry_child_index", 0))].cid

        if event_type in _DIAPER_MODE:
            await api.log_diaper(child, mode=_DIAPER_MODE[event_type], start_time=timestamp)
            logging.info("Huckleberry: logged diaper %s", event_type)
        elif event_type == "Feed":
            await api.log_bottle(child, start_time=timestamp, amount=0, bottle_type="Breastmilk", units="ml")
            logging.info("Huckleberry: logged feed")
        elif event_type == "Play":
            await api.log_activity(child, mode="tummyTime", start_time=timestamp)
            logging.info("Huckleberry: logged tummy time")


async def _push_sleep_async(start_time: datetime, end_time: datetime) -> None:
    settings = load_settings()
    email    = settings.get("huckleberry_email", "")
    password = settings.get("huckleberry_password", "")
    if not email or not password:
        return

    async with aiohttp.ClientSession() as session:
        api = _make_api(settings, session)
        await api.authenticate()
        user  = await api.get_user()
        child = user.childList[int(settings.get("huckleberry_child_index", 0))].cid
        await api.log_sleep(child, start_time=start_time, end_time=end_time)
        logging.info("Huckleberry: logged sleep %s → %s",
                     start_time.isoformat(), end_time.isoformat())


def _friendly_auth_error(err: aiohttp.ClientResponseError) -> str:
    """Map a Firebase auth error into an actionable message."""
    msg = err.message or ""
    if "EMAIL_NOT_FOUND" in msg:
        return "No Huckleberry account for that email — check huckleberry_email in settings.json."
    if "INVALID_PASSWORD" in msg or "INVALID_LOGIN_CREDENTIALS" in msg:
        return ("Password rejected by Huckleberry. If you normally sign into the app with "
                "Apple or Google, there is no email+password credential — open the Huckleberry "
                "app and set/reset a password first, then use that here.")
    if "MISSING_PASSWORD" in msg or "MISSING_EMAIL" in msg:
        return "Email or password is empty in settings.json."
    if "TOO_MANY_ATTEMPTS" in msg:
        return "Too many failed attempts — Huckleberry temporarily blocked sign-in. Wait and retry."
    return f"Huckleberry rejected sign-in (HTTP {err.status}): {msg}"


async def _test_connection_async() -> dict:
    settings = load_settings()
    email    = settings.get("huckleberry_email", "")
    password = settings.get("huckleberry_password", "")
    if not email or not password:
        raise ValueError("huckleberry_email / huckleberry_password not set in settings.json")
    async with aiohttp.ClientSession() as session:
        api = _make_api(settings, session)
        try:
            await api.authenticate()
        except aiohttp.ClientResponseError as err:
            raise RuntimeError(_friendly_auth_error(err)) from err
        user = await api.get_user()
        return {"ok": True, "child_count": len(user.childList)}


# ── Public API (non-blocking) ─────────────────────────────────────────────────

def push_event(event_type: str, timestamp: datetime) -> None:
    """Push a keypad/button event to Huckleberry in the background."""
    _run_async(_push_event_async(event_type, timestamp))


def push_sleep(start_time: datetime, end_time: datetime) -> None:
    """Push a completed sleep session to Huckleberry in the background."""
    _run_async(_push_sleep_async(start_time, end_time))


def test_connection() -> dict:
    """Blocking connection test. Returns {"ok": True, "children": [...]} or raises."""
    return asyncio.run(_test_connection_async())
