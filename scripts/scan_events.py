#!/usr/bin/env python3
"""Scan Somali community websites and rewrite the events section of index.html.

Rather than scraping each site's HTML by hand -- markup that changes without
warning and breaks silently -- this reads the structured event data those pages
already publish, in this order:

  1. JSON-LD  <script type="application/ld+json"> containing schema.org Events.
     Squarespace, WordPress event plugins, Eventbrite and most university
     calendars emit this, so one parser covers many sites.
  2. Microdata  itemscope/itemtype="schema.org/Event" attributes.
  3. iCalendar  any .ics feed linked from the page.

Events are merged across sources, filtered to what is still upcoming,
de-duplicated, and the soonest MAX_EVENTS are written into two marked regions
of index.html: the .event-card anchors and a JSON-LD ItemList.

Sites are listed in event_sources.json; adding one needs no code change. A
source that fails or yields nothing is logged and skipped, and if every source
comes up empty the page is left exactly as it is.

Usage:
    python3 scripts/scan_events.py [--dry-run] [--sources FILE] [--page FILE]
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import zoneinfo

SITE = "https://americansomali.com/"
CENTRAL = zoneinfo.ZoneInfo("America/Chicago")

MAX_EVENTS = 5           # cards shown on the page
HORIZON_DAYS = 400       # ignore anything further out than this
FETCH_TIMEOUT = 30
USER_AGENT = "americansomali-events/1.0 (+https://americansomali.com/)"

# Applied only to sources marked relevance='keywords' -- venues that host all
# kinds of programming, where most events are not for this audience.
KEYWORDS = (
    "somali", "soomaali", "somalia", "buraanbur", "dhaqan", "hiddo",
    "east african", "horn of africa", "oromo", "sagal", "kaah",
)

CARDS_START, CARDS_END = "<!-- events:start -->", "<!-- events:end -->"
SCHEMA_START, SCHEMA_END = "<!-- events-schema:start -->", "<!-- events-schema:end -->"


class EventsError(RuntimeError):
    """Raised when index.html cannot be rewritten."""


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #

def fetch(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
        raw = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
    return raw.decode(charset, errors="replace")


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #

JSONLD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.S | re.I,
)


# Most schema.org Event subtypes end in "Event" (MusicEvent, SocialEvent, ...),
# but several do not and would otherwise be missed.
EVENT_TYPES = frozenset(
    {"Event", "Festival", "Hackathon", "CourseInstance", "EventSeries", "ExhibitionEvent"}
)


def is_event_type(node: dict) -> bool:
    types = node.get("@type", "")
    types = [types] if isinstance(types, str) else (types if isinstance(types, list) else [])
    return any(
        isinstance(t, str) and (t.split("/")[-1] in EVENT_TYPES or t.endswith("Event"))
        for t in types
    )


def walk_jsonld(node, found: list[dict]) -> None:
    """Collect every schema.org Event object anywhere in a JSON-LD document."""
    if isinstance(node, list):
        for child in node:
            walk_jsonld(child, found)
        return
    if not isinstance(node, dict):
        return

    if is_event_type(node) and node.get("startDate"):
        found.append(node)

    for value in node.values():
        walk_jsonld(value, found)


def from_jsonld(page: str) -> list[dict]:
    found: list[dict] = []
    for block in JSONLD_RE.findall(page):
        text = block.strip()
        if not text:
            continue
        try:
            walk_jsonld(json.loads(text), found)
        except json.JSONDecodeError:
            continue  # a malformed block on their side is not our problem
    return found


MICRODATA_ITEM_RE = re.compile(
    r'itemtype=["\'][^"\']*schema\.org/(\w*Event)["\'](.{0,6000}?)(?=itemtype=|\Z)',
    re.S | re.I,
)
MICRODATA_PROP_RE = re.compile(
    r'itemprop=["\'](\w+)["\'][^>]*?(?:content|datetime)=["\']([^"\']+)["\']',
    re.I,
)
TAG_RE = re.compile(r"<[^>]+>")


def from_microdata(page: str) -> list[dict]:
    events = []
    for _type, body in MICRODATA_ITEM_RE.findall(page):
        props = {key.lower(): value for key, value in MICRODATA_PROP_RE.findall(body)}
        if "startdate" not in props:
            continue
        events.append(
            {
                "name": props.get("name", ""),
                "startDate": props["startdate"],
                "endDate": props.get("enddate"),
                "url": props.get("url"),
                "description": props.get("description", ""),
                "location": props.get("location", ""),
            }
        )
    return events


ICS_LINK_RE = re.compile(r'href=["\']([^"\']+\.ics(?:\?[^"\']*)?)["\']', re.I)


def unfold_ics(text: str) -> list[str]:
    """iCalendar wraps long values onto continuation lines starting with space."""
    lines: list[str] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if line[:1] in (" ", "\t") and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return lines


def unescape_ics(value: str) -> str:
    return (
        value.replace("\\n", " ").replace("\\N", " ")
        .replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")
    )


def from_ics(text: str) -> list[dict]:
    """Parse VEVENT blocks. Recurrence is not expanded -- see README note."""
    events, current = [], None
    for line in unfold_ics(text):
        if line.startswith("BEGIN:VEVENT"):
            current = {}
            continue
        if line.startswith("END:VEVENT"):
            if current and current.get("startDate"):
                events.append(current)
            current = None
            continue
        if current is None or ":" not in line:
            continue

        name, _, value = line.partition(":")
        key = name.split(";")[0].upper()
        if key == "DTSTART":
            current["startDate"] = value.strip()
        elif key == "DTEND":
            current["endDate"] = value.strip()
        elif key == "SUMMARY":
            current["name"] = unescape_ics(value).strip()
        elif key == "LOCATION":
            current["location"] = unescape_ics(value).strip()
        elif key == "DESCRIPTION":
            current["description"] = unescape_ics(value).strip()
        elif key == "URL":
            current["url"] = value.strip()
    return events


def extract(page: str, base_url: str) -> list[dict]:
    """Try each strategy in turn; the first that yields events wins."""
    for strategy in (from_jsonld, from_microdata):
        events = strategy(page)
        if events:
            return events

    for href in ICS_LINK_RE.findall(page)[:2]:
        try:
            return from_ics(fetch(urllib.parse.urljoin(base_url, html.unescape(href))))
        except (urllib.error.URLError, OSError, ValueError):
            continue
    return []


# --------------------------------------------------------------------------- #
# Normalising
# --------------------------------------------------------------------------- #

def parse_date(value) -> tuple[dt.datetime, bool] | tuple[None, None]:
    """Return (aware datetime in Central, is_all_day) from ISO or iCalendar text."""
    if not isinstance(value, str) or not value.strip():
        return None, None
    text = value.strip()

    # iCalendar basic format: 20260828T170000Z / 20260828T170000 / 20260828
    compact = re.fullmatch(r"(\d{8})(?:T(\d{6})(Z)?)?", text)
    if compact:
        day, time, zulu = compact.groups()
        stamp = dt.datetime.strptime(day + (time or ""), "%Y%m%d%H%M%S" if time else "%Y%m%d")
        if zulu:
            stamp = stamp.replace(tzinfo=dt.timezone.utc)
        return (
            stamp.astimezone(CENTRAL) if stamp.tzinfo else stamp.replace(tzinfo=CENTRAL),
            time is None,
        )

    try:
        stamp = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            stamp = dt.datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            return None, None
        return stamp.replace(tzinfo=CENTRAL), True

    all_day = len(text) <= 10
    return (stamp.astimezone(CENTRAL) if stamp.tzinfo else stamp.replace(tzinfo=CENTRAL)), all_day


def flatten_location(value) -> str:
    """schema.org location may be a string, a Place, or a list of either."""
    if isinstance(value, str):
        return TAG_RE.sub(" ", value).strip()
    if isinstance(value, list):
        parts = [flatten_location(v) for v in value]
        return next((p for p in parts if p), "")
    if not isinstance(value, dict):
        return ""

    name = str(value.get("name", "")).strip()
    address = value.get("address")
    if isinstance(address, str):
        joined = address.strip()
    elif isinstance(address, dict):
        joined = ", ".join(
            str(address[key]).strip()
            for key in ("streetAddress", "addressLocality", "addressRegion")
            if address.get(key)
        )
    else:
        joined = ""

    if name and joined and name.casefold() not in joined.casefold():
        return f"{name}, {joined}"
    return name or joined


def safe_url(url: str, base: str) -> str:
    """Resolve a scraped link, rejecting anything that is not plain http(s).

    These URLs come from third-party pages and end up as href attributes, so a
    javascript: or data: value must never survive, and a relative link that
    resolves against a non-web base is a dead link rather than a useful one.
    """
    if not url:
        return ""
    resolved = urllib.parse.urljoin(base, url)
    return resolved if urllib.parse.urlparse(resolved).scheme in ("http", "https") else ""


def normalise(raw: dict, source: dict) -> dict | None:
    name = raw.get("name") or raw.get("headline") or ""
    if isinstance(name, dict):
        name = name.get("@value", "")
    name = re.sub(r"\s+", " ", html.unescape(TAG_RE.sub(" ", str(name)))).strip()
    if not name:
        return None

    start, all_day = parse_date(raw.get("startDate"))
    if start is None:
        return None
    end, _ = parse_date(raw.get("endDate"))

    url = raw.get("url") or raw.get("@id") or ""
    if isinstance(url, dict):
        url = url.get("@id", "") or url.get("url", "")
    url = safe_url(str(url).strip(), source["url"])

    description = raw.get("description") or ""
    if isinstance(description, dict):
        description = description.get("@value", "")

    return {
        "name": name,
        "start": start,
        "end": end,
        "all_day": bool(all_day),
        "location": html.unescape(flatten_location(raw.get("location"))),
        "description": html.unescape(TAG_RE.sub(" ", str(description))),
        "url": url or safe_url(source["url"], source["url"]),
        "source": source["name"],
    }


def relevant(event: dict, mode: str) -> bool:
    if mode != "keywords":
        return True
    haystack = f"{event['name']} {event['description']} {event['location']}".casefold()
    return any(word in haystack for word in KEYWORDS)


def gather(sources: list[dict], now: dt.datetime) -> list[dict]:
    horizon = now + dt.timedelta(days=HORIZON_DAYS)
    collected: list[dict] = []

    for source in sources:
        try:
            page = fetch(source["url"])
        except (urllib.error.URLError, OSError, ValueError) as error:
            # One unreachable site must not take the whole run down.
            print(f"  ! {source['name']}: {error}")
            continue

        kept = 0
        for raw in extract(page, source["url"]):
            event = normalise(raw, source)
            if event is None:
                continue
            if not (now <= (event["end"] or event["start"]) and event["start"] <= horizon):
                continue
            if not relevant(event, source.get("relevance", "all")):
                continue
            collected.append(event)
            kept += 1
        print(f"  - {source['name']}: {kept} upcoming")

    collected.sort(key=lambda e: e["start"])

    # Two sources often list the same event, and a weekly class would otherwise
    # fill every slot, so keep the soonest instance of each title.
    seen: set[str] = set()
    unique = []
    for event in collected:
        key = re.sub(r"\W+", "", event["name"].casefold())
        if key in seen:
            continue
        seen.add(key)
        unique.append(event)
    return unique[:MAX_EVENTS]


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def badge_text(event: dict) -> str:
    """A short date label, e.g. 'Fri, Aug 28' or 'Sep 19 - Sep 20'."""
    start = event["start"]
    label = start.strftime("%b %-d") if event["all_day"] else start.strftime("%a, %b %-d")
    end = event["end"]
    if end and end.date() > start.date():
        # iCalendar and schema.org treat an all-day end date as exclusive.
        last = end.date() - dt.timedelta(days=1) if event["all_day"] else end.date()
        if last > start.date():
            label = f"{start.strftime('%b %-d')} &ndash; {last.strftime('%b %-d')}"
    return label


# Trailing lead-ins left behind once a URL is stripped, e.g. "... more at <url>".
DANGLING = re.compile(
    r"(?:\b(?:learn\s+more|read\s+more|more|details|info|information|"
    r"sign\s+up|register|rsvp|tickets?)\b)?[\s:]*\b(?:at|here|on|via)?[\s:,.\-]*$",
    re.IGNORECASE,
)


def blurb(event: dict, limit: int = 320) -> str:
    """Card copy: description without markup, URLs, or dangling lead-ins."""
    text = re.sub(r"https?://\S+", "", event["description"])
    text = re.sub(r"\s+", " ", text).strip()
    text = DANGLING.sub("", text).strip()

    if len(text) > limit:
        cut = text[:limit].rsplit(" ", 1)[0].rstrip(",;:-")
        text = cut + "…"

    where = event["location"]
    if where and where.casefold() not in text.casefold():
        if text and not text.endswith((".", "!", "?", "…")):
            text += "."
        text = f"{text} {where}".strip()
    return text


def render_cards(events: list[dict]) -> str:
    lines = [
        CARDS_START,
        "      <!-- Cards below are regenerated by scripts/scan_events.py from the sites",
        "           in scripts/event_sources.json. Manual edits here will be overwritten;",
        "           they are committed so the page still renders if the job is skipped. -->",
    ]
    for event in events:
        # quote=False keeps apostrophes readable in text nodes; attributes below
        # are escaped with quoting on.
        name = html.escape(event["name"], quote=False)
        text = html.escape(blurb(event), quote=False)
        href = event["url"]
        if href:
            label = html.escape(f"{event['name']} — opens in a new tab", quote=True)
            lines.append(
                f'      <a class="news-card event-card" href="{html.escape(href, quote=True)}"'
                f' target="_blank" rel="noopener noreferrer" aria-label="{label}">'
            )
        else:
            # No usable link, so render a card that does not look clickable.
            lines.append('      <div class="news-card event-card is-static">')

        lines += [
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
        if event["url"]:
            item["url"] = event["url"]
        if event["end"]:
            item["endDate"] = (
                event["end"].date().isoformat() if event["all_day"] else event["end"].isoformat()
            )
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
            "<!-- Regenerated by scripts/scan_events.py alongside the event cards. -->",
            '<script type="application/ld+json">',
            json.dumps(payload, indent=2, ensure_ascii=False),
            "</script>",
            SCHEMA_END,
        ]
    )


def splice(page: str, start: str, end: str, replacement: str) -> str:
    i, j = page.find(start), page.find(end)
    if i == -1 or j == -1:
        raise EventsError(f"missing markers {start} / {end}")
    line_start = page.rfind("\n", 0, i) + 1
    indent = page[line_start:i]
    return page[:line_start] + indent + replacement + page[j + len(end) :]


# --------------------------------------------------------------------------- #

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--page", default="index.html")
    parser.add_argument(
        "--sources", default=os.path.join(os.path.dirname(__file__), "event_sources.json")
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="report what was found without writing"
    )
    args = parser.parse_args()

    with open(args.sources, encoding="utf-8") as handle:
        sources = json.load(handle)["sources"]

    now = dt.datetime.now(tz=CENTRAL)
    print(f"Scanning {len(sources)} sources at {now:%Y-%m-%d %H:%M %Z}")
    events = gather(sources, now)

    if not events:
        # Never blank the section on a bad scan; keep the last good cards.
        print("No upcoming events found - leaving the page unchanged.")
        return 0

    print(f"\nTop {len(events)}:")
    for event in events:
        print(f"  {event['start']:%Y-%m-%d %H:%M}  {event['name']}  ({event['source']})")

    if args.dry_run:
        return 0

    with open(args.page, encoding="utf-8") as handle:
        page = original = handle.read()

    page = splice(page, CARDS_START, CARDS_END, render_cards(events))
    page = splice(page, SCHEMA_START, SCHEMA_END, render_schema(events))

    # Refuse to write a page whose structured data no longer parses.
    for block in JSONLD_RE.findall(page):
        json.loads(block)

    if page == original:
        print("\nAlready up to date.")
        return 0

    with open(args.page, "w", encoding="utf-8") as handle:
        handle.write(page)
    print(f"\nUpdated {args.page}.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (EventsError, OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)
