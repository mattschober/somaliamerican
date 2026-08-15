#!/usr/bin/env python3
"""Regenerate the Upcoming Events section of index.html from a public calendar.

Reads an iCalendar (.ics) feed -- a public Google Calendar works well -- keeps
the next few events, and rewrites two regions of index.html in place:

  <!-- events:start -->        ... the .event-card anchors
  <!-- events-schema:start --> ... the JSON-LD ItemList of schema.org Events

Everything outside those markers is left byte-for-byte alone, so the hand-built
page stays hand-built. The rendered output is committed, which means the page
still shows the last known events if this job is ever skipped or fails.

Usage:
    EVENTS_ICS_URL="https://calendar.google.com/calendar/ical/.../public/basic.ics" \
        python3 scripts/update_events.py

With no EVENTS_ICS_URL set the script exits without touching anything, so the
workflow is harmless until the calendar is configured.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import sys
import urllib.request
import zoneinfo

import icalendar
import recurring_ical_events

PAGE = "index.html"
SITE = "https://americansomali.com/"
CENTRAL = zoneinfo.ZoneInfo("America/Chicago")

# How many events to show, and how far ahead to look for them.
MAX_EVENTS = 5
HORIZON_DAYS = 400

CARDS_START, CARDS_END = "<!-- events:start -->", "<!-- events:end -->"
SCHEMA_START, SCHEMA_END = "<!-- events-schema:start -->", "<!-- events-schema:end -->"


class EventsError(RuntimeError):
    """Raised when the feed cannot be turned into a usable set of events."""


# --------------------------------------------------------------------------- #
# Reading the calendar
# --------------------------------------------------------------------------- #

def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "americansomali-events/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def as_aware(value) -> dt.datetime:
    """Normalise an ics date or datetime to an aware Central-time datetime."""
    if isinstance(value, dt.datetime):
        return value.astimezone(CENTRAL) if value.tzinfo else value.replace(tzinfo=CENTRAL)
    # An all-day event: treat it as starting at midnight local time.
    return dt.datetime(value.year, value.month, value.day, tzinfo=CENTRAL)


def is_all_day(component) -> bool:
    start = component.get("DTSTART")
    return start is not None and not isinstance(start.dt, dt.datetime)


def collect(ics_bytes: bytes, now: dt.datetime) -> list[dict]:
    """Expand recurrences and return the next MAX_EVENTS upcoming events."""
    calendar = icalendar.Calendar.from_ical(ics_bytes)
    window_end = now + dt.timedelta(days=HORIZON_DAYS)

    events = []
    for component in recurring_ical_events.of(calendar).between(now, window_end):
        summary = str(component.get("SUMMARY", "")).strip()
        if not summary:
            continue  # an event with no title has nothing to show

        start = as_aware(component["DTSTART"].dt)
        end = component.get("DTEND")
        events.append(
            {
                "name": summary,
                "start": start,
                "end": as_aware(end.dt) if end is not None else None,
                "all_day": is_all_day(component),
                "location": str(component.get("LOCATION", "")).strip(),
                "description": str(component.get("DESCRIPTION", "")).strip(),
                "url": str(component.get("URL", "")).strip(),
            }
        )

    # between() already filters to the window, but recurrence expansion can
    # return an occurrence that started earlier today; drop anything past.
    events = [e for e in events if (e["end"] or e["start"]) >= now]
    events.sort(key=lambda e: e["start"])

    # A weekly class would otherwise fill every slot with the same title, so
    # keep only the soonest occurrence of each event and let five different
    # things through.
    seen: set[str] = set()
    unique = []
    for event in events:
        key = event["name"].casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(event)
    return unique[:MAX_EVENTS]


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def badge_text(event: dict) -> str:
    """A short date label, e.g. 'Fri, Aug 28' or 'Aug 28 - Sep 2'."""
    start = event["start"]
    label = start.strftime("%a, %b %-d") if not event["all_day"] else start.strftime("%b %-d")
    end = event["end"]
    if end and end.date() > start.date():
        # Non-inclusive DTEND on all-day events: show the last real day.
        last = end.date() - dt.timedelta(days=1) if event["all_day"] else end.date()
        if last > start.date():
            label = f"{start.strftime('%b %-d')} &ndash; {last.strftime('%b %-d')}"
    return label


def link_for(event: dict) -> str:
    """Prefer an explicit URL, else the first link found in the description."""
    if event["url"]:
        return event["url"]
    match = re.search(r"https?://[^\s<>\"]+", event["description"])
    return match.group(0).rstrip(".,);") if match else ""


# Trailing lead-ins left behind once a URL is stripped, e.g. "... more at <url>".
DANGLING = re.compile(
    r"(?:\b(?:learn\s+more|read\s+more|more|details|info|information|"
    r"sign\s+up|register|rsvp|tickets?)\b)?[\s:]*\b(?:at|here|on|via)?[\s:,.\-]*$",
    re.IGNORECASE,
)


def blurb(event: dict) -> str:
    """Card copy: the description with markup, URLs, and dangling lead-ins removed."""
    text = re.sub(r"<[^>]+>", " ", event["description"])
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = DANGLING.sub("", text).strip()

    where = event["location"]
    if where and where.casefold() not in text.casefold():
        if text and not text.endswith((".", "!", "?")):
            text += "."
        text = f"{text} {where}".strip()
    return text


def render_cards(events: list[dict]) -> str:
    lines = [
        CARDS_START,
        "      <!-- Cards below are regenerated by scripts/update_events.py from the",
        "           public Google Calendar feed. Manual edits here will be overwritten;",
        "           they are committed so the page still renders if the job is skipped. -->",
    ]
    for event in events:
        href = link_for(event)
        name = html.escape(event["name"])
        text = html.escape(blurb(event))
        label = f"{name} — opens in a new tab" if href else name
        open_tag = (
            f'      <a class="news-card event-card" href="{html.escape(href)}"'
            f' target="_blank" rel="noopener noreferrer"'
            f' aria-label="{html.escape(label)}">'
            if href
            else '      <div class="news-card event-card is-static">'
        )
        lines += [
            open_tag,
            f'        <span class="badge">{badge_text(event)}</span>',
            f"        <h3>{name}</h3>",
            f"        <p>{text}</p>",
        ]
        if href:
            lines += [
                '        <span class="news-card-link" aria-hidden="true">Visit site &rarr;</span>',
                "      </a>",
            ]
        else:
            lines.append("      </div>")
    lines.append("      " + CARDS_END)
    return "\n".join(lines)


def render_schema(events: list[dict]) -> str:
    items = []
    for position, event in enumerate(events, start=1):
        item = {
            "@type": "Event",
            "name": event["name"],
            "startDate": event["start"].date().isoformat()
            if event["all_day"]
            else event["start"].isoformat(),
            "eventAttendanceMode": "https://schema.org/OfflineEventAttendanceMode",
        }
        if event["end"]:
            item["endDate"] = (
                event["end"].date().isoformat() if event["all_day"] else event["end"].isoformat()
            )
        if link_for(event):
            item["url"] = link_for(event)
        if blurb(event):
            item["description"] = blurb(event)
        if event["location"]:
            item["location"] = {"@type": "Place", "name": event["location"]}
        items.append({"@type": "ListItem", "position": position, "item": item})

    payload = {
        "@context": "https://schema.org",
        "@type": "ItemList",
        "@id": f"{SITE}#events",
        "name": "Somali Community Events in Minnesota",
        "description": "Somali cultural festivals, performances, and community gatherings in Minnesota.",
        "itemListOrder": "https://schema.org/ItemListOrderAscending",
        "numberOfItems": len(items),
        "itemListElement": items,
    }
    return "\n".join(
        [
            SCHEMA_START,
            "<!-- Regenerated by scripts/update_events.py alongside the event cards. -->",
            '<script type="application/ld+json">',
            json.dumps(payload, indent=2, ensure_ascii=False),
            "</script>",
            SCHEMA_END,
        ]
    )


def splice(page: str, start: str, end: str, replacement: str) -> str:
    i, j = page.find(start), page.find(end)
    if i == -1 or j == -1:
        raise EventsError(f"missing markers {start} / {end} in {PAGE}")
    # Keep the indentation that precedes the opening marker.
    line_start = page.rfind("\n", 0, i) + 1
    indent = page[line_start:i]
    return page[:line_start] + indent + replacement + page[j + len(end) :]


# --------------------------------------------------------------------------- #

def main() -> int:
    url = os.environ.get("EVENTS_ICS_URL", "").strip()
    if not url:
        print("EVENTS_ICS_URL is not set - leaving index.html untouched.")
        print("Set it to a public Google Calendar .ics URL to enable updates.")
        return 0

    now = dt.datetime.now(tz=CENTRAL)
    events = collect(fetch(url), now)
    if not events:
        # Never blank the section: an empty calendar keeps the last good cards.
        print("No upcoming events in the feed - keeping the committed cards.")
        return 0

    with open(PAGE, encoding="utf-8") as handle:
        page = original = handle.read()

    page = splice(page, CARDS_START, CARDS_END, render_cards(events))
    page = splice(page, SCHEMA_START, SCHEMA_END, render_schema(events))

    # Fail loudly rather than committing a page whose structured data is broken.
    for block in re.findall(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', page, re.S):
        json.loads(block)

    if page == original:
        print("Already up to date.")
        return 0

    with open(PAGE, "w", encoding="utf-8") as handle:
        handle.write(page)

    print(f"Wrote {len(events)} event(s):")
    for event in events:
        print(f"  {event['start']:%Y-%m-%d %H:%M}  {event['name']}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (EventsError, OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)
