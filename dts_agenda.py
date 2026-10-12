"""
DTS Ede 4 -> agenda (.ics)

Leest het programma van de teampagina op dtsede.nl en maakt er een
iCalendar-feed van die je als abonnement in Google/Apple/Outlook toevoegt.

Gebruik:
    pip install beautifulsoup4
    python dts_agenda.py            # schrijft dts-ede-4.ics
    python dts_agenda.py --serve    # serveert http://localhost:8000/dts-ede-4.ics
"""

import json
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

# ---------------- instellingen ----------------
TEAM_URL = "https://www.dtsede.nl/dts-ede-4"
TEAM_NAAM = "DTS '35 Ede 4"          # zoals op de site in de wedstrijdnaam staat
ICS_BESTAND = Path("dts-ede-4.ics")
CACHE_BESTAND = Path("wedstrijden.json")  # bewaart ook gespeelde wedstrijden
WEDSTRIJDDUUR = timedelta(hours=2)
HERINNERING_MINUTEN = 90              # None = geen herinnering
VERVERS_INTERVAL = 30 * 60            # sec, alleen voor --serve
POORT = 8000
# ----------------------------------------------

TZ = ZoneInfo("Europe/Amsterdam")
MAANDEN = {"jan": 1, "feb": 2, "mrt": 3, "maa": 3, "apr": 4, "mei": 5, "jun": 6,
           "jul": 7, "aug": 8, "sep": 9, "okt": 10, "nov": 11, "dec": 12}
DATUM_RE = re.compile(r"(\d{1,2})\s+([a-z]{3})[a-z]*\.?\s+(\d{1,2}):(\d{2})", re.I)
UITSLAG_RE = re.compile(r"^\d+\s*-\s*\d+$")


def haal_pagina_op(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (team-agenda)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", errors="replace")


def kies_jaar(dag: int, maand: int, uur: int, minuut: int) -> datetime:
    """De site toont geen jaartal: kies het jaar dat het dichtst bij vandaag ligt."""
    nu = datetime.now(TZ)
    kandidaten = []
    for jaar in (nu.year - 1, nu.year, nu.year + 1):
        try:
            kandidaten.append(datetime(jaar, maand, dag, uur, minuut, tzinfo=TZ))
        except ValueError:
            pass
    return min(kandidaten, key=lambda d: abs(d - nu))


def parse_programma(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    wedstrijden = []
    for rij in soup.find_all("tr"):
        cellen = [c.get_text(" ", strip=True) for c in rij.find_all("td")]
        teksten = [t for t in cellen if t]
        if not teksten:
            continue
        # uitslagen-rijen overslaan (die hebben een score zoals "3 - 0")
        if any(UITSLAG_RE.match(t) for t in teksten):
            continue
        datum_m = next((DATUM_RE.search(t) for t in teksten if DATUM_RE.search(t)), None)
        wedstrijd = next((t for t in teksten if " - " in t and TEAM_NAAM in t), None)
        link = rij.find("a", href=re.compile(r"wedstrijdcode=\d+"))
        if not (datum_m and wedstrijd and link):
            continue

        dag, mnd, uur, mi = datum_m.groups()
        maand = MAANDEN.get(mnd.lower()[:3])
        if not maand:
            continue
        start = kies_jaar(int(dag), maand, int(uur), int(mi))
        code = re.search(r"wedstrijdcode=(\d+)", link["href"]).group(1)

        # accommodatie = tekst die geen datum, wedstrijd of link is
        overig = [t for t in teksten
                  if t != wedstrijd and not DATUM_RE.search(t) and "wedstrijdcode" not in t]
        thuis, uit = [s.strip() for s in wedstrijd.split(" - ", 1)]

        wedstrijden.append({
            "code": code,
            "start": start.isoformat(),
            "thuis": thuis,
            "uit": uit,
            "accommodatie": overig[0] if overig else "",
            "url": link["href"],
        })
    return wedstrijden


def update_cache(nieuw: list[dict]) -> list[dict]:
    """Voegt nieuwe data samen met eerder opgeslagen wedstrijden.
    Gespeelde wedstrijden blijven staan; toekomstige die van de site verdwijnen
    (bijv. afgelast/verplaatst zonder nieuwe datum) worden verwijderd."""
    oud = json.loads(CACHE_BESTAND.read_text()) if CACHE_BESTAND.exists() else {}
    nu = datetime.now(TZ)
    nieuwe_codes = {w["code"] for w in nieuw}
    resultaat = {c: w for c, w in oud.items()
                 if c in nieuwe_codes or datetime.fromisoformat(w["start"]) < nu}
    for w in nieuw:
        resultaat[w["code"]] = w
    CACHE_BESTAND.write_text(json.dumps(resultaat, indent=2, ensure_ascii=False))
    return sorted(resultaat.values(), key=lambda w: w["start"])


def esc(tekst: str) -> str:
    return (tekst.replace("\\", "\\\\").replace(";", "\\;")
                 .replace(",", "\\,").replace("\n", "\\n"))


def vouw(regel: str) -> str:
    """iCalendar: regels max 75 bytes, vervolg met spatie."""
    data = regel.encode("utf-8")
    if len(data) <= 75:
        return regel
    delen, huidig = [], b""
    for ch in regel:
        b = ch.encode("utf-8")
        if len(huidig) + len(b) > (75 if not delen else 74):
            delen.append(huidig.decode("utf-8"))
            huidig = b""
        huidig += b
    delen.append(huidig.decode("utf-8"))
    return "\r\n ".join(delen)


def maak_ics(wedstrijden: list[dict]) -> str:
    utc = lambda d: d.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    nu = utc(datetime.now(TZ))
    regels = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//dts-agenda//NL",
        "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
        f"X-WR-CALNAME:{esc(TEAM_NAAM)}", "X-WR-TIMEZONE:Europe/Amsterdam",
        "REFRESH-INTERVAL;VALUE=DURATION:PT1H", "X-PUBLISHED-TTL:PT1H",
    ]
    for w in wedstrijden:
        start = datetime.fromisoformat(w["start"])
        thuiswedstrijd = w["thuis"] == TEAM_NAAM
        titel = f"⚽ {w['thuis']} - {w['uit']}" + (" (thuis)" if thuiswedstrijd else " (uit)")
        regels += [
            "BEGIN:VEVENT",
            f"UID:{w['code']}@dts-agenda",
            f"DTSTAMP:{nu}",
            f"DTSTART:{utc(start)}",
            f"DTEND:{utc(start + WEDSTRIJDDUUR)}",
            f"SUMMARY:{esc(titel)}",
            f"LOCATION:{esc(w['accommodatie'])}",
            f"DESCRIPTION:{esc('Details: ' + w['url'])}",
            f"URL:{w['url']}",
        ]
        if HERINNERING_MINUTEN:
            regels += ["BEGIN:VALARM", "ACTION:DISPLAY",
                       f"DESCRIPTION:{esc(titel)}",
                       f"TRIGGER:-PT{HERINNERING_MINUTEN}M", "END:VALARM"]
        regels.append("END:VEVENT")
    regels.append("END:VCALENDAR")
    return "\r\n".join(vouw(r) for r in regels) + "\r\n"


def ververs() -> str:
    html = haal_pagina_op(TEAM_URL)
    gevonden = parse_programma(html)
    if not gevonden:
        print("Let op: geen wedstrijden gevonden (seizoen voorbij of site gewijzigd?)")
    alles = update_cache(gevonden)
    ics = maak_ics(alles)
    ICS_BESTAND.write_text(ics, encoding="utf-8")
    print(f"{len(gevonden)} gevonden op site, {len(alles)} in feed -> {ICS_BESTAND}")
    return ics


def serve():
    status = {"ics": None, "tijd": 0.0}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.split("?")[0] != f"/{ICS_BESTAND.name}":
                self.send_error(404)
                return
            if time.time() - status["tijd"] > VERVERS_INTERVAL or not status["ics"]:
                try:
                    status["ics"] = ververs()
                    status["tijd"] = time.time()
                except Exception as e:  # site even onbereikbaar: oude versie geven
                    print("Verversen mislukt:", e)
                    if not status["ics"] and ICS_BESTAND.exists():
                        status["ics"] = ICS_BESTAND.read_text(encoding="utf-8")
            if not status["ics"]:
                self.send_error(503)
                return
            body = status["ics"].encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/calendar; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    print(f"Feed op http://localhost:{POORT}/{ICS_BESTAND.name}")
    HTTPServer(("0.0.0.0", POORT), Handler).serve_forever()


if __name__ == "__main__":
    serve() if "--serve" in sys.argv else ververs()
