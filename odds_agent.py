#!/usr/bin/env python3
"""Odds Lens agent (paper-trading mode).

Compares market odds against a "fair" probability (the sharp book's de-vigged line,
or the consensus of all books if the sharp book is absent), logs signals with a
timestamp, tracks closing-line value (CLV), settles results and reports.
Information and analysis only. Not betting advice.

Usage:
  export ODDS_API_KEY=...            # https://the-odds-api.com
  python odds_agent.py run           # fetch, log signals, notify, settle
  python odds_agent.py report        # hit rate, CLV, flat-stake result
  python odds_agent.py export        # writes signals.json to import into the Odds Lens page
  python odds_agent.py demo          # offline test with sample data
Schedule `run` a few times a day (cron / GitHub Actions / any scheduler).

Optional env: SPORTS=NFL,NBA,MLB,UFC,SOCCER,TENNIS  MIN_EDGE=0.05  MAX_EDGE=0.20
  REGIONS=eu,uk,us  SHARP_BOOK=pinnacle  LANG_UI=es|en  DB_PATH=signals.db
  TELEGRAM_TOKEN=...  TELEGRAM_CHAT=...
"""
import os, sys, json, sqlite3, argparse, urllib.request, urllib.parse, urllib.error
from datetime import datetime, timezone

API = "https://api.the-odds-api.com/v4"
KEY = os.environ.get("ODDS_API_KEY", "")
SHARP = os.environ.get("SHARP_BOOK", "pinnacle")
REGIONS = os.environ.get("REGIONS", "eu,uk,us")
MIN_EDGE = float(os.environ.get("MIN_EDGE", "0.05"))
MAX_EDGE = float(os.environ.get("MAX_EDGE", "0.20"))  # huge "edges" are usually stale lines
LANG = os.environ.get("LANG_UI", "es")
DB = os.environ.get("DB_PATH", "signals.db")
ENABLED = os.environ.get("SPORTS", "NFL,NBA,MLB,UFC,SOCCER,TENNIS").split(",")

# group -> (explicit keys, active-key prefix, max keys from prefix). Each key costs API credits.
GROUPS = {
    "NFL": (["americanfootball_nfl"], None, 0),
    "NBA": (["basketball_nba"], None, 0),
    "MLB": (["baseball_mlb"], None, 0),
    "UFC": (["mma_mixed_martial_arts"], None, 0),
    "SOCCER": (["soccer_epl", "soccer_spain_la_liga", "soccer_uefa_champions_league", "soccer_mexico_ligamx"], None, 0),
    "TENNIS": ([], "tennis_", 3),
}

TXT = {
    "es": "Señal de análisis\n{ev} ({lg})\nSelección: {sel} en {book}\nProbabilidad justa: {p:.1f}%\nCuota: {odds:.2f} (implica {imp:.1f}%)\nVentaja estimada: +{edge:.1f}%\nEs información, no una recomendación. Ninguna probabilidad es garantía. Solo mayores de 18.",
    "en": "Analysis signal\n{ev} ({lg})\nSelection: {sel} at {book}\nFair probability: {p:.1f}%\nOdds: {odds:.2f} (implies {imp:.1f}%)\nEstimated edge: +{edge:.1f}%\nThis is information, not a recommendation. No probability is a guarantee. 18+ only.",
}


def get(path, **params):
    params["apiKey"] = KEY
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            print(f"  credits left: {r.headers.get('x-requests-remaining', '?')}", file=sys.stderr)
            return json.load(r)
    except urllib.error.HTTPError as e:
        print(f"API error {e.code} on {path}", file=sys.stderr)
        return []


def active_keys():
    active = {s["key"] for s in get("/sports/") if s.get("active")}  # this endpoint is free
    keys = []
    for g in ENABLED:
        explicit, prefix, n = GROUPS.get(g.strip(), ([], None, 0))
        keys += [k for k in explicit if k in active]
        if prefix:
            keys += sorted(k for k in active if k.startswith(prefix))[:n]
    return keys


def devig(outcomes):
    """Proportional de-vig: remove the bookmaker margin from implied probabilities."""
    imp = {n: 1 / p for n, p in outcomes if p > 1}
    s = sum(imp.values())
    return {n: v / s for n, v in imp.items()} if s else {}


def analyse(ev):
    """Return {selection: (fair_p, best_book, best_odds)} for the h2h market."""
    per_book, best = {}, {}
    for b in ev.get("bookmakers", []):
        m = next((m for m in b["markets"] if m["key"] == "h2h"), None)
        if not m:
            continue
        outs = [(o["name"], o["price"]) for o in m["outcomes"]]
        per_book[b["key"]] = devig(outs)
        for n, p in outs:
            if n not in best or p > best[n][1]:
                best[n] = (b["key"], p)
    if not per_book:
        return {}
    if SHARP in per_book:
        fair = per_book[SHARP]
    else:
        names = set().union(*per_book.values())
        fair = {n: sum(d.get(n, 0) for d in per_book.values()) / len(per_book) for n in names}
    return {n: (fair[n], best[n][0], best[n][1]) for n in fair if n in best}


def db():
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS signals(
        id INTEGER PRIMARY KEY, event_id, sport_key, league, home, away, commence,
        selection, book, odds REAL, fair_p REAL, edge REAL, issued, close_p REAL, result,
        UNIQUE(event_id, selection, book))""")
    return c


def notify(msg):
    tok, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT")
    if tok and chat:
        data = urllib.parse.urlencode({"chat_id": chat, "text": msg}).encode()
        try:
            urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage", data, timeout=20)
            return
        except Exception as e:
            print("Telegram failed:", e, file=sys.stderr)
    print("-" * 40 + "\n" + msg)


def cycle(c, events):
    now = datetime.now(timezone.utc).isoformat()
    new = 0
    for ev in events:
        for sel, (p, book, odds) in analyse(ev).items():
            # keep the latest fair probability until kickoff: the last value is the closing line
            if ev["commence_time"] > now:
                c.execute("UPDATE signals SET close_p=? WHERE event_id=? AND selection=? AND result IS NULL",
                          (p, ev["id"], sel))
            edge = p - 1 / odds
            if MIN_EDGE <= edge <= MAX_EDGE and ev["commence_time"] > now:
                cur = c.execute("INSERT OR IGNORE INTO signals(event_id,sport_key,league,home,away,commence,selection,book,odds,fair_p,edge,issued,close_p)"
                                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                (ev["id"], ev["sport_key"], ev.get("sport_title", ""), ev["home_team"], ev["away_team"],
                                 ev["commence_time"], sel, book, odds, p, edge, now, p))
                if cur.rowcount:
                    new += 1
                    notify(TXT[LANG].format(ev=f"{ev['home_team']} - {ev['away_team']}", lg=ev.get("sport_title", ""),
                                            sel=sel, book=book, p=p * 100, odds=odds, imp=100 / odds, edge=edge * 100))
    c.commit()
    return new


def settle(c, scores_by_key):
    for key, scores in scores_by_key.items():
        for s in scores:
            if not s.get("completed") or not s.get("scores"):
                continue
            pts = {x["name"]: float(x["score"]) for x in s["scores"]}
            top = max(pts.values())
            winners = [n for n, v in pts.items() if v == top]
            win = winners[0] if len(winners) == 1 else "Draw"
            for row in c.execute("SELECT id, selection FROM signals WHERE event_id=? AND result IS NULL", (s["id"],)).fetchall():
                res = "W" if row[1] == win else "L"
                if len(winners) > 1 and not key.startswith("soccer"):
                    res = "V"  # tie in a market without a draw: void
                c.execute("UPDATE signals SET result=? WHERE id=?", (res, row[0]))
    c.commit()


def report(c):
    rows = c.execute("SELECT odds, close_p, result FROM signals WHERE result IN ('W','L')").fetchall()
    total = c.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    if not rows:
        print(f"Signals logged: {total}. None settled yet.")
        return
    n = len(rows)
    wins = sum(r[2] == "W" for r in rows)
    units = sum((r[0] - 1) if r[2] == "W" else -1 for r in rows)
    clvs = [r[0] * r[1] - 1 for r in rows if r[1]]  # odds taken vs. closing fair probability
    print(f"Signals logged: {total} | settled: {n}")
    print(f"Hit rate: {wins / n:.1%} | flat 1u result: {units:+.2f}u (ROI {units / n:+.1%})")
    if clvs:
        print(f"Average CLV: {sum(clvs) / len(clvs):+.2%} (positive and sustained = best evidence of real edge)")
    print("Note: judge by CLV over hundreds of signals, not by short-term results.")


def export(c, path=None, limit=400):
    path = path or os.environ.get("EXPORT_PATH", "signals.json")
    cols = ["event_id", "league", "home", "away", "commence", "selection", "book", "odds", "fair_p", "edge", "issued", "close_p", "result"]
    rows = c.execute(f"SELECT {','.join(cols)} FROM signals ORDER BY issued DESC LIMIT ?", (limit,)).fetchall()
    out = {"generated": datetime.now(timezone.utc).isoformat(), "rows": [dict(zip(cols, r)) for r in rows]}
    with open(path, "w") as f:
        json.dump(out, f, ensure_ascii=False)
    print(f"Wrote {len(rows)} signals to {path}")


def run():
    if not KEY:
        sys.exit("Set ODDS_API_KEY first.")
    c = db()
    keys = active_keys()
    print("Sports keys:", keys, file=sys.stderr)
    events = []
    for k in keys:
        events += get(f"/sports/{k}/odds", regions=REGIONS, markets="h2h", oddsFormat="decimal")
    print(f"New signals: {cycle(c, events)}")
    open_keys = [r[0] for r in c.execute("SELECT DISTINCT sport_key FROM signals WHERE result IS NULL")]
    settle(c, {k: get(f"/sports/{k}/scores", daysFrom=3) for k in open_keys})
    report(c)
    export(c)


def demo():
    """Offline test: sample odds, then a 'closing' snapshot, then fake results."""
    global DB
    DB = ":memory:"
    c = db()
    soon = "2099-01-01T12:00:00Z"
    def ev(i, sharp, other):
        mk = lambda o: [{"key": "h2h", "outcomes": [{"name": n, "price": p} for n, p in o]}]
        return {"id": f"e{i}", "sport_key": "soccer_epl", "sport_title": "Sample League", "home_team": f"Home{i}",
                "away_team": f"Away{i}", "commence_time": soon,
                "bookmakers": [{"key": "pinnacle", "markets": mk(sharp)}, {"key": "otherbook", "markets": mk(other)}]}
    e1 = ev(1, [("Home1", 2.00), ("Draw", 3.50), ("Away1", 4.00)], [("Home1", 2.35), ("Draw", 3.40), ("Away1", 3.90)])
    e2 = ev(2, [("Home2", 1.60), ("Draw", 4.00), ("Away2", 6.00)], [("Home2", 1.55), ("Draw", 3.90), ("Away2", 5.80)])
    print("Cycle 1 (opening odds):")
    cycle(c, [e1, e2])
    e1c = ev(1, [("Home1", 1.90), ("Draw", 3.60), ("Away1", 4.40)], [("Home1", 2.00), ("Draw", 3.40), ("Away1", 4.00)])
    cycle(c, [e1c, e2])  # sharp line moved toward the signal -> positive CLV
    c.execute("UPDATE signals SET result='W'")
    c.commit()
    print("\nReport after (fake) settlement:")
    report(c)
    export(c, "/tmp/signals_demo.json")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["run", "report", "export", "demo"])
    a = ap.parse_args()
    {"run": run, "report": lambda: report(db()), "export": lambda: export(db()), "demo": demo}[a.cmd]()
