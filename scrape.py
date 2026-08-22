#!/usr/bin/env python3
"""
FC Crans - Juniors D-9 (1er degre, Groupe 1) results scraper + table builder.

What it does:
1. Fetches the FC Crans "Resultats + classements" page from the ACVF matchcenter.
2. Finds the section for our specific group (identified by SECTION_KEYWORDS).
3. Extracts every match shown there (date, time, home team, away team, score if played).
4. Merges new/updated matches into results.json (a permanent history - the website
   itself only ever shows the *current* matchday, so this file is our real archive).
5. Recomputes a standings table from the full history and writes table.html.

Because the site only shows one matchday at a time, this script is meant to be run
on a schedule (e.g. daily via GitHub Actions) so nothing gets missed between visits.

NOTE ON ROBUSTNESS:
This was built from the page's rendered content rather than a look at its raw HTML
tags, so the exact CSS/structure assumptions below (in particular, how "sections"
are delimited) are a best first attempt. If FC Crans' club id, ACVF site id, or the
group name changes, or if the site's markup differs from what's assumed here, you
may need to adjust CONFIG below or the parsing logic in extract_section().
Run with --debug to print diagnostic info about what was found.
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# CONFIG - edit these if needed
# ---------------------------------------------------------------------------

# The club-wide "Resultats + classements" page. This lists all FC Crans teams'
# current matchday fixtures/results in one page (server-rendered, no JS needed).
RESULTS_URL = "https://matchcenter-acvf.football.ch/default.aspx?v=1058&oid=16&lng=2&a=rr"

# Text that must ALL appear in a section heading to identify our group.
# Adjust if the official group name changes between seasons.
SECTION_KEYWORDS = ["Juniors D-9", "1er degr"]  # "1er degr" matches degré/degre spelling

# Our team's exact display name as it appears on the site.
OUR_TEAM = "FC Crans I"

DATA_DIR = Path(__file__).parent
RESULTS_FILE = DATA_DIR / "results.json"
OUTPUT_HTML = DATA_DIR / "table.html"

DATE_RE = re.compile(r"^(?:Lu|Ma|Me|Je|Ve|Sa|Di)\s+(\d{2}\.\d{2}\.\d{4})$")
MATCH_LINK_RE = re.compile(
    r"^\s*(\d{1,2}:\d{2})?\s*(.+?)\s+-\s+(.+?)(?:\s+(\d+)\s*:\s*(\d+))?\s*$"
)
TG_RE = re.compile(r"[?&]tg=(\d+)")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "fr-CH,fr;q=0.9,en;q=0.8",
    "Referer": "https://matchcenter-acvf.football.ch/",
}


def fetch_page(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or resp.encoding
    return resp.text


def find_section_root(soup: BeautifulSoup):
    """
    Locate the heading element whose text contains all SECTION_KEYWORDS, then
    return the list of sibling elements that make up that section (everything
    up to, but not including, the next heading of the same tag name).
    """
    heading_tags = ["h1", "h2", "h3", "h4", "h5", "strong", "b"]
    candidates = soup.find_all(heading_tags)

    target = None
    for tag in candidates:
        text = tag.get_text(" ", strip=True)
        if all(kw.lower() in text.lower() for kw in SECTION_KEYWORDS):
            target = tag
            break

    if target is None:
        return None, []

    # Collect subsequent siblings/elements until we hit another heading of the
    # same tag name (i.e. the start of the next section).
    section_elements = []
    for el in target.find_all_next():
        if el.name == target.name and el is not target:
            # Reached the next section's heading - stop.
            break
        section_elements.append(el)

    return target, section_elements


def parse_matches(section_elements) -> list:
    """
    Walk through the section, tracking the current date, and pull out each
    match link (identified by an href containing tg=...).
    """
    matches = []
    current_date = None

    for el in section_elements:
        text = el.get_text(" ", strip=True) if hasattr(el, "get_text") else str(el).strip()

        date_match = DATE_RE.match(text) if text else None
        if date_match:
            current_date = date_match.group(1)
            continue

        if getattr(el, "name", None) == "a":
            href = el.get("href", "")
            tg_match = TG_RE.search(href)
            if not tg_match:
                continue
            tg_id = tg_match.group(1)

            link_text = el.get_text(" ", strip=True)
            m = MATCH_LINK_RE.match(link_text)
            if not m:
                continue
            time_str, home, away, score_h, score_a = m.groups()

            played = score_h is not None and score_a is not None

            matches.append(
                {
                    "tg_id": tg_id,
                    "date": current_date,
                    "time": time_str,
                    "home": home.strip(),
                    "away": away.strip(),
                    "home_score": int(score_h) if played else None,
                    "away_score": int(score_a) if played else None,
                    "played": played,
                }
            )

    return matches


def load_history() -> dict:
    if RESULTS_FILE.exists():
        return json.loads(RESULTS_FILE.read_text(encoding="utf-8"))
    return {}


def save_history(history: dict) -> None:
    RESULTS_FILE.write_text(
        json.dumps(history, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


def merge_matches(history: dict, new_matches: list) -> int:
    """Merge newly-scraped matches into history, keyed by tg_id. Returns count of changes."""
    changes = 0
    for match in new_matches:
        key = match["tg_id"]
        existing = history.get(key)
        if existing != match:
            history[key] = match
            changes += 1
    return changes


def compute_table(history: dict) -> list:
    """Build standings from every played match in history."""
    teams = {}

    def get_team(name):
        if name not in teams:
            teams[name] = {
                "team": name,
                "played": 0,
                "won": 0,
                "drawn": 0,
                "lost": 0,
                "gf": 0,
                "ga": 0,
                "pts": 0,
            }
        return teams[name]

    for match in history.values():
        if not match.get("played"):
            continue
        home, away = match["home"], match["away"]
        hs, as_ = match["home_score"], match["away_score"]

        h, a = get_team(home), get_team(away)
        h["played"] += 1
        a["played"] += 1
        h["gf"] += hs
        h["ga"] += as_
        a["gf"] += as_
        a["ga"] += hs

        if hs > as_:
            h["won"] += 1
            a["lost"] += 1
            h["pts"] += 3
        elif hs < as_:
            a["won"] += 1
            h["lost"] += 1
            a["pts"] += 3
        else:
            h["drawn"] += 1
            a["drawn"] += 1
            h["pts"] += 1
            a["pts"] += 1

    table = list(teams.values())
    for row in table:
        row["gd"] = row["gf"] - row["ga"]

    table.sort(key=lambda r: (-r["pts"], -r["gd"], -r["gf"], r["team"]))
    return table


def render_html(table: list, history: dict, our_team: str) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    rows_html = ""
    for i, row in enumerate(table, start=1):
        highlight = ' style="font-weight:bold;background:#fff3cd;"' if row["team"] == our_team else ""
        rows_html += (
            f"<tr{highlight}>"
            f"<td>{i}</td><td>{row['team']}</td><td>{row['played']}</td>"
            f"<td>{row['won']}</td><td>{row['drawn']}</td><td>{row['lost']}</td>"
            f"<td>{row['gf']}</td><td>{row['ga']}</td><td>{row['gd']}</td>"
            f"<td><b>{row['pts']}</b></td></tr>"
        )

    upcoming = [m for m in history.values() if not m.get("played")]
    upcoming.sort(key=lambda m: (m.get("date") or "", m.get("time") or ""))
    upcoming_html = "".join(
        f"<li>{m['date']} {m['time'] or ''} - {m['home']} vs {m['away']}</li>"
        for m in upcoming
    )

    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<title>FC Crans - Juniors D-9 I - Classement</title>
<style>
body {{ font-family: Arial, sans-serif; max-width: 720px; margin: 2rem auto; padding: 0 1rem; color:#222; }}
h1 {{ font-size: 1.4rem; }}
table {{ border-collapse: collapse; width: 100%; margin-top: 1rem; }}
th, td {{ border: 1px solid #ccc; padding: 6px 8px; text-align: center; font-size: 0.9rem; }}
th {{ background: #222; color: #fff; }}
td:nth-child(2) {{ text-align: left; }}
.updated {{ color: #666; font-size: 0.85rem; margin-top: 0.5rem; }}
</style>
</head>
<body>
<h1>FC Crans - Juniors D-9 I (1er degre, Groupe 1)</h1>
<p class="updated">Derniere mise a jour : {now}</p>
<table>
<tr><th>#</th><th>Equipe</th><th>J</th><th>G</th><th>N</th><th>P</th><th>BP</th><th>BC</th><th>Diff</th><th>Pts</th></tr>
{rows_html}
</table>
<h2 style="font-size:1.1rem;margin-top:2rem;">Matchs a venir</h2>
<ul>{upcoming_html or '<li>Aucun match a venir enregistre pour le moment.</li>'}</ul>
</body>
</html>"""


def main():
    debug = "--debug" in sys.argv

    html = fetch_page(RESULTS_URL)
    soup = BeautifulSoup(html, "html.parser")

    heading, section_elements = find_section_root(soup)
    if heading is None:
        print(
            "ERROR: could not find a heading matching "
            f"{SECTION_KEYWORDS} on the page. The site's wording may have changed - "
            "open the page in a browser and update SECTION_KEYWORDS in scrape.py.",
            file=sys.stderr,
        )
        sys.exit(1)

    if debug:
        print(f"Found section heading: {heading.get_text(' ', strip=True)!r}")

    new_matches = parse_matches(section_elements)
    if debug:
        print(f"Parsed {len(new_matches)} match(es) from current page view:")
        for m in new_matches:
            print(" ", m)

    if not new_matches:
        print(
            "WARNING: no matches parsed. The section was found but no match links "
            "matched the expected pattern - the HTML structure may differ from what "
            "this script assumes. Run with --debug and inspect the page source.",
            file=sys.stderr,
        )

    history = load_history()
    changes = merge_matches(history, new_matches)
    save_history(history)

    table = compute_table(history)
    OUTPUT_HTML.write_text(render_html(table, history, OUR_TEAM), encoding="utf-8")

    print(f"Done. {changes} new/updated match record(s). Table written to {OUTPUT_HTML}.")


if __name__ == "__main__":
    main()
