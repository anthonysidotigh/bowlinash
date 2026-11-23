"""Scrape SportsKeep (ladder / fixture / results) and Datascore (player stats + PDF game
reports) into data/data.json and reports/. Run from anywhere: python scraper/scrape.py

Each section is scraped independently: if one fails, the previous data for that section
is kept and the error is recorded, so one broken site never blanks the whole page.
data.json is only rewritten when something actually changed, so the GitHub Action
doesn't make a commit every 5 minutes for nothing.
"""

import base64
import json
import re
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "scraper" / "config.json").read_text())
DATA_FILE = ROOT / "data" / "data.json"
REPORTS_DIR = ROOT / "reports"

# ---------------------------------------------------------------- helpers

# Read every row of the SportsKeep table (they all use <table id="ladder">), keeping link targets.
SK_TABLE_JS = """
() => {
  const t = document.querySelector('table#ladder');
  if (!t) return null;
  const rows = [...t.rows].filter(r => r.cells.length).map(r => [...r.cells].map(c => ({
    text: c.innerText.trim(),
    links: [...c.querySelectorAll('a')].map(a => a.getAttribute('href'))
  })));
  return { headings: [...document.querySelectorAll('h1')].map(h => h.innerText.trim()), rows };
}
"""

# Read the MudBlazor player-rankings table. Two header rows: group names (with colspans)
# then column names, so keys become e.g. "Scoring Runs:Total" / "Going Out:Total".
DS_TABLE_JS = """
() => {
  const t = document.querySelector('table');
  if (!t || t.rows.length < 3) return null;
  const groups = [];
  for (const c of t.rows[0].cells) for (let i = 0; i < (c.colSpan || 1); i++) groups.push(c.innerText.trim());
  const keys = [...t.rows[1].cells].map((c, i) => (groups[i] ? groups[i] + ':' : '') + c.innerText.trim());
  const rows = [...t.rows].slice(2).map(r => [...r.cells].map(c => c.innerText.trim()));
  const info = (document.body.innerText.match(/Showing .*? data from .*?\\./) || [''])[0];
  return { keys, rows, info };
}
"""

# Blazor sends each PDF as a data: URL on an <a download> that it clicks.
# Intercept that so the PDF bytes can be read straight out of the page.
DOWNLOAD_HOOK_JS = """
window.__dl = [];
const _click = HTMLAnchorElement.prototype.click;
HTMLAnchorElement.prototype.click = function () {
  if (this.href && this.href.startsWith('data:')) { window.__dl.push(this.href); return; }
  return _click.call(this);
};
"""


def num(v):
    v = (v or "").strip().rstrip(".")
    try:
        f = float(v)
        return int(f) if f.is_integer() else round(f, 2)
    except ValueError:
        return v


def param(href, name):
    m = re.search(rf"[?&]{name}=(\d+)", href or "")
    return int(m.group(1)) if m else None


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def sk_url(page, **extra):
    c = CFG["sportskeep"]
    q = f"sp={c['sp']}&g={c['g']}&s={c['s']}"
    if extra.get("team"):
        q += f"&t={c['t']}"
    return f"{c['base']}{page}?{q}&embedded="


# ---------------------------------------------------------------- SportsKeep

def scrape_sportskeep(page):
    out = {}

    page.goto(sk_url("stadium-ladder.php"), wait_until="domcontentloaded")
    t = page.evaluate(SK_TABLE_JS)
    ladder = []
    for r in t["rows"][1:]:
        if len(r) < 14:
            continue
        ladder.append({
            "pos": num(r[0]["text"]), "team": r[1]["text"],
            "teamId": param((r[14]["links"] or [None])[0], "t") if len(r) > 14 else None,
            "p": num(r[2]["text"]), "w": num(r[3]["text"]), "l": num(r[4]["text"]),
            "d": num(r[5]["text"]), "b": num(r[6]["text"]), "ff": num(r[7]["text"]),
            "fa": num(r[8]["text"]), "for": num(r[9]["text"]), "against": num(r[10]["text"]),
            "pct": num(r[11]["text"]), "bonus": num(r[12]["text"]), "pts": num(r[13]["text"]),
        })
    out["competition"] = t["headings"][0] if t["headings"] else ""
    out["ladder"] = ladder

    page.goto(sk_url("stadium-team-fixture.php", team=True), wait_until="domcontentloaded")
    t = page.evaluate(SK_TABLE_JS)
    out["fixture"] = [
        {"round": num(r[0]["text"]), "opponent": r[1]["text"], "day": r[2]["text"],
         "date": r[3]["text"], "time": r[4]["text"], "court": r[5]["text"]}
        for r in t["rows"][1:] if len(r) >= 6 and r[1]["text"]
    ]

    page.goto(sk_url("stadium-team-results.php", team=True), wait_until="domcontentloaded")
    t = page.evaluate(SK_TABLE_JS)
    out["results"] = [
        {"round": num(r[0]["text"]), "opponent": r[1]["text"], "date": r[2]["text"],
         "result": r[3]["text"], "score": r[4]["text"], "points": num(r[5]["text"])}
        for r in t["rows"][1:] if len(r) >= 6 and r[1]["text"]
    ]
    return out


# ---------------------------------------------------------------- Datascore stats

def ds_rankings(page, filter_type):
    c = CFG["datascore"]
    page.goto(f"{c['base']}/IndoorCricket/PlayerRankings/{c['teamId']}?filterType={filter_type}")
    page.wait_for_function("document.querySelectorAll('table tr').length > 2", timeout=60000)
    page.wait_for_timeout(1000)  # let Blazor finish rendering rows
    t = page.evaluate(DS_TABLE_JS)
    players = []
    for row in t["rows"]:
        d = dict(zip(t["keys"], row))
        players.append({
            "rank": num(d.get("Rank")), "name": d.get("Player Name", "").title(),
            "team": d.get("Team Name", ""),
            "runsScored": num(d.get("Scoring Runs:Total")), "outs": num(d.get("Going Out:Total")),
            "netRuns": num(d.get("Total Runs")), "runsAgainst": num(d.get("Runs Against")),
            "wickets": num(d.get("Wickets Taken")), "ngv": num(d.get("Total Net Game Value")),
            "games": num(d.get("Num Games")), "avgNgv": num(d.get("Average Net Game Value")),
            "fours": num(d.get("Scoring Runs:4")), "fives": num(d.get("Scoring Runs:5")),
            "sevens": num(d.get("Scoring Runs:7")),
        })
    return players, t["info"]


def scrape_datascore_stats(page):
    team, info = ds_rankings(page, "FT_Team")
    grade, grade_info = ds_rankings(page, "FT_Grade")
    our = CFG["teamName"].upper()
    top_n = CFG["datascore"]["gradeTopN"]
    return {
        "teamPlayers": team, "teamInfo": info,
        "gradeTop": [p for i, p in enumerate(grade) if i < top_n or p["team"].upper() == our],
        "gradeCount": len(grade), "gradeInfo": grade_info,
    }


# ---------------------------------------------------------------- Datascore PDFs

def parse_game(label):
    # "18/11/2026 09:16 against SLOGGERS 8.0"
    m = re.match(r"(\d{2})/(\d{2})/(\d{4}) (\d{2}):(\d{2}) against (.+)", label)
    if not m:
        return None
    d, mo, y, hh, mm, opp = m.groups()
    return {"key": f"{y}-{mo}-{d}_{hh}{mm}_{slug(opp)}", "label": label,
            "date": f"{y}-{mo}-{d}", "time": f"{hh}:{mm}", "opponent": opp.title()}


def open_game_list(page):
    page.click("input.mud-select-input")
    page.wait_for_selector(".mud-popover-open .mud-list-item", timeout=30000)
    return page.locator(".mud-popover-open .mud-list-item")


def scrape_reports(page, previous_games):
    c = CFG["datascore"]
    page.goto(f"{c['base']}/IndoorCricket/TeamReports/{c['teamId']}")
    page.wait_for_selector("input.mud-select-input", timeout=60000)
    page.wait_for_timeout(1000)

    labels = [s.strip() for s in open_game_list(page).all_inner_texts()]
    page.keyboard.press("Escape")
    games = [g for g in (parse_game(l) for l in labels) if g][: c["maxGamesWithReports"]]

    prev = {g["key"]: g for g in previous_games}
    for g in games:
        g["reports"] = dict(prev.get(g["key"], {}).get("reports", {}))
        missing = [r for r in c["reports"]
                   if not (ROOT / g["reports"].get(slug(r), "__none__")).exists()]
        if not missing:
            continue  # finished games don't change, so only download each PDF once

        items = open_game_list(page)
        items.filter(has_text=g["label"]).first.click()
        page.wait_for_function(
            "v => document.querySelector('input.mud-select-input').value.trim() === v",
            arg=g["label"], timeout=30000)
        page.wait_for_timeout(800)

        folder = REPORTS_DIR / g["key"]
        folder.mkdir(parents=True, exist_ok=True)
        for report in missing:
            try:
                n = page.evaluate("window.__dl.length")
                page.get_by_role("button", name=report, exact=True).click()
                page.wait_for_function(f"window.__dl.length > {n}", timeout=90000)
                href = page.evaluate("window.__dl[window.__dl.length - 1]")
                pdf = base64.b64decode(href.split(",", 1)[1])
                path = folder / f"{slug(report)}.pdf"
                path.write_bytes(pdf)
                g["reports"][slug(report)] = str(path.relative_to(ROOT))
                print(f"  saved {path.relative_to(ROOT)} ({len(pdf) // 1024} KB)")
            except Exception as e:  # keep going with the other reports
                print(f"  ! {g['label']} / {report}: {e}", file=sys.stderr)
    return games


# ---------------------------------------------------------------- main

def main():
    old = json.loads(DATA_FILE.read_text()) if DATA_FILE.exists() else {}
    new = {k: v for k, v in old.items() if k not in ("updatedAt", "errors")}
    errors = {}

    with sync_playwright() as p:
        # ubuntu-latest GitHub runners already have Google Chrome, so no browser download.
        try:
            browser = p.chromium.launch(channel="chrome")
        except Exception:
            browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        ctx.add_init_script(DOWNLOAD_HOOK_JS)
        page = ctx.new_page()
        page.set_default_timeout(45000)

        steps = [
            ("sportskeep", lambda: new.update(scrape_sportskeep(page))),
            ("stats", lambda: new.update(scrape_datascore_stats(page))),
            ("reports", lambda: new.update(games=scrape_reports(page, old.get("games", [])))),
        ]
        for name, fn in steps:
            try:
                print(f"- {name}")
                fn()
            except Exception as e:
                traceback.print_exc()
                errors[name] = str(e).splitlines()[0][:300]
        browser.close()

    new["teamName"] = CFG["teamName"]
    new["sources"] = {
        "ladder": sk_url("stadium-ladder.php"),
        "datascore": f"{CFG['datascore']['base']}/IndoorCricket/TeamReports/{CFG['datascore']['teamId']}",
    }
    if errors:
        new["errors"] = errors

    comparable_old = {k: v for k, v in old.items() if k != "updatedAt"}
    if new == comparable_old:
        print("No changes.")
        return
    new["updatedAt"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    DATA_FILE.parent.mkdir(exist_ok=True)
    DATA_FILE.write_text(json.dumps(new, indent=1))
    print("data.json updated.")


if __name__ == "__main__":
    main()
