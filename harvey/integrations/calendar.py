"""Calendar over Microsoft Graph: read busy time, find free slots, create events.

Degrades gracefully like the placeholder it replaces: with no Microsoft
sign-in (or no calendar permission) every read returns [] and every write
returns False/None, so callers can invoke it unconditionally.

Harvey's persona never proposes specific times in a message (the booking link
does that), so nothing in the run loop calls this yet. It exists so booked
meetings and availability checks have a real backend.

Creating an event with attendees makes Exchange email the invitation to them,
so ``create_event`` is an outward-facing action: call it only when a meeting
has actually been agreed.
"""

import logging
from datetime import datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

from harvey import owner
from harvey.integrations.ms_auth import MicrosoftAuthError
from harvey.integrations.ms_graph import GraphClient, GraphError

logger = logging.getLogger("harvey.calendar")

BUSY_STATES = {"busy", "oof", "tentative", "workingelsewhere"}
UTC = timezone.utc


def _parse_graph_dt(value: dict) -> datetime:
    """Graph returns {'dateTime': '2026-10-22T14:00:00.0000000', 'timeZone': 'UTC'}."""
    raw = (value.get("dateTime") or "").split(".")[0]
    return datetime.fromisoformat(raw).replace(tzinfo=UTC)


def compute_free_slots(busy: list[tuple[datetime, datetime]], start: datetime, end: datetime, *,
                       duration_min: int = 30, tz: str = "America/New_York",
                       day_start: dtime = dtime(9, 0), day_end: dtime = dtime(17, 0),
                       step_min: int = 30, weekdays: tuple[int, ...] = (0, 1, 2, 3, 4)) -> list[dict]:
    """Free `duration_min` windows inside working hours (local `tz`), none overlapping `busy`.

    Pure: all datetimes are timezone-aware. Slots start on `step_min` boundaries."""
    zone = ZoneInfo(tz)
    duration = timedelta(minutes=duration_min)
    merged: list[list[datetime]] = []
    for b_start, b_end in sorted(busy):
        if merged and b_start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b_end)
        else:
            merged.append([b_start, b_end])

    slots: list[dict] = []
    day = start.astimezone(zone).date()
    last_day = end.astimezone(zone).date()
    while day <= last_day:
        if day.weekday() in weekdays:
            cursor = datetime.combine(day, day_start, zone)
            close = datetime.combine(day, day_end, zone)
            while cursor + duration <= close:
                slot_end = cursor + duration
                if (cursor >= start and slot_end <= end
                        and not any(cursor < b_end and slot_end > b_start for b_start, b_end in merged)):
                    slots.append({"start": cursor.astimezone(UTC).isoformat(),
                                  "end": slot_end.astimezone(UTC).isoformat()})
                cursor += timedelta(minutes=step_min)
        day += timedelta(days=1)
    return slots


class CalendarClient:
    def __init__(self, graph: GraphClient | None = None, *, tz: str = "America/New_York"):
        self.graph = graph
        self.tz = tz

    def is_available(self) -> bool:
        auth = getattr(self.graph, "auth", None)
        return bool(auth and auth.is_authorized() and auth.has_feature("calendar"))

    async def list_events(self, start: datetime, end: datetime, limit: int = 100) -> list[dict]:
        """Events overlapping [start, end), cancelled ones dropped. [] when unavailable."""
        if not self.is_available():
            return []
        try:
            rows = await self.graph.get_all(
                "/me/calendarView",
                params={"startDateTime": start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S"),
                        "endDateTime": end.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S"),
                        "$select": "id,subject,start,end,showAs,isCancelled,isAllDay,location,webLink",
                        "$orderby": "start/dateTime", "$top": str(min(limit, 100))},
                headers={"Prefer": 'outlook.timezone="UTC"'}, limit=limit)
        except (GraphError, MicrosoftAuthError) as e:
            logger.error(f"Calendar read failed: {e}")
            return []
        return [r for r in rows if not r.get("isCancelled")]

    async def busy_blocks(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime]] | None:
        """Busy intervals, or None when the calendar could not be read (unknown is not free)."""
        if not self.is_available():
            return None
        try:
            rows = await self.graph.get_all(
                "/me/calendarView",
                params={"startDateTime": start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S"),
                        "endDateTime": end.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S"),
                        "$select": "start,end,showAs,isCancelled", "$top": "100"},
                headers={"Prefer": 'outlook.timezone="UTC"'}, limit=500)
        except (GraphError, MicrosoftAuthError) as e:
            logger.error(f"Calendar read failed: {e}")
            return None
        return [(_parse_graph_dt(r["start"]), _parse_graph_dt(r["end"]))
                for r in rows
                if not r.get("isCancelled") and (r.get("showAs") or "busy").lower() in BUSY_STATES]

    async def find_free_slots(self, start: datetime, end: datetime, **kwargs) -> list[dict]:
        """Free windows in working hours. [] when the calendar can't be read: never guess free."""
        busy = await self.busy_blocks(start, end)
        if busy is None:
            return []
        kwargs.setdefault("tz", self.tz)
        return compute_free_slots(busy, start, end, **kwargs)

    async def create_event(self, subject: str, start: datetime, end: datetime, *,
                           attendees: list[str] | None = None, body: str = "",
                           location: str = "") -> dict | None:
        """Create an event on the primary calendar. Attendees receive an invitation."""
        if not self.is_available():
            return None
        payload = {
            "subject": subject,
            "start": {"dateTime": start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "UTC"},
            "end": {"dateTime": end.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "UTC"},
            "body": {"contentType": "text", "content": body},
            "attendees": [{"emailAddress": {"address": a}, "type": "required"} for a in attendees or []],
        }
        if location:
            payload["location"] = {"displayName": location}
        try:
            return await self.graph.request("POST", "/me/events", json=payload)
        except (GraphError, MicrosoftAuthError) as e:
            logger.error(f"Calendar event not created: {e}")
            return None

    # Kept from the placeholder so existing callers don't break.
    async def get_available_slots(self, date_range: str) -> list[dict]:
        """`date_range` is 'YYYY-MM-DD' or 'YYYY-MM-DD/YYYY-MM-DD' (inclusive, local time)."""
        try:
            first, _, last = date_range.partition("/")
            zone = ZoneInfo(self.tz)
            start = datetime.combine(datetime.fromisoformat(first).date(), dtime(0, 0), zone)
            end = datetime.combine(datetime.fromisoformat(last or first).date(), dtime(23, 59), zone)
        except ValueError:
            logger.warning(f"get_available_slots: unreadable date range {date_range!r}")
            return []
        return await self.find_free_slots(start, end)

    async def book_meeting(self, prospect_email: str, slot: dict) -> bool:
        if not prospect_email or not isinstance(slot, dict) or not slot.get("start") or not slot.get("end"):
            logger.warning("book_meeting called with invalid arguments.")
            return False
        try:
            start, end = datetime.fromisoformat(slot["start"]), datetime.fromisoformat(slot["end"])
        except (TypeError, ValueError):
            logger.warning("book_meeting called with an unreadable slot.")
            return False
        event = await self.create_event(
            f"Conversation with {owner.NAME}", start, end, attendees=[prospect_email])
        return event is not None
