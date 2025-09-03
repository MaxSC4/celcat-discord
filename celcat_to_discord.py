# celcat_html_to_discord.py — Embeds par cours (demain)
# - Vise "demain" (Europe/Paris), force ?dt=YYYY-MM-DD sur l'URL listWeek
# - Parse la page listWeek (Playwright) en blocs : horaire -> (titre, enseignants, salle, type)
# - Envoie 1 embed Discord par cours : titre=nom du cours, champs Horaires/Enseignants/Salle/Type (+ emojis)
# - Chaque embed a une couleur inspirée du logo (#E6443A) et un lien vers la semaine CELCAT

import os, re, asyncio, datetime as dt, requests
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from dateutil.tz import gettz
from dotenv import load_dotenv
from playwright.async_api import async_playwright

# ------------ Config ------------
load_dotenv()
WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL")
LIST_URL_TEMPLATE = os.getenv("CELCAT_LIST_URL")
TZ_NAME  = os.getenv("TZ_NAME", "Europe/Paris")
TZ = gettz(TZ_NAME)

DEBUG=1

assert WEBHOOK and LIST_URL_TEMPLATE, "Config manquante: DISCORD_WEBHOOK_URL / CELCAT_LIST_URL"

MD_SPECIALS = re.compile(r"([_*~`>])")
def md_escape(s: str) -> str:
    return MD_SPECIALS.sub(r"\\\1", s)

# ------------ Dates / FR ------------
JOURS_FR = ["lundi","mardi","mercredi","jeudi","vendredi","samedi","dimanche"]
MOIS_FR  = ["janvier","février","mars","avril","mai","juin","juillet","août","septembre","octobre","novembre","décembre"]

def now_paris() -> dt.datetime:
    return dt.datetime.now(TZ)

def french_date(d: dt.date, capitalize_first: bool = True) -> str:
    jour = JOURS_FR[d.weekday()]
    mois = MOIS_FR[d.month - 1]
    s = f"{jour} {d.day} {mois} {d.year}"
    return s[:1].upper() + s[1:] if capitalize_first else s

def week_url_for(date_obj: dt.date) -> str:
    parsed = urlparse(LIST_URL_TEMPLATE)
    q = parse_qs(parsed.query, keep_blank_values=True)
    q["dt"] = [date_obj.strftime("%Y-%m-%d")]
    new_query = urlencode(q, doseq=True)
    return urlunparse(parsed._replace(query=new_query))

# ------------ Parsing heuristique ------------
MONTHS = {
    "january":1,"february":2,"march":3,"april":4,"may":5,"june":6,"july":7,"august":8,"september":9,"october":10,"november":11,"december":12,
    "janvier":1,"février":2,"fevrier":2,"mars":3,"avril":4,"mai":5,"juin":6,"juillet":7,"août":8,"aout":8,"septembre":9,"octobre":10,"novembre":11,"décembre":12,"decembre":12
}
DATE_FULL_RE  = re.compile(r"(?i)^\s*(\d{1,2})\s+([A-Za-zéèêëàâîïôöûüç]+)\s+(\d{4})\s*$")
TIME_RANGE_RE = re.compile(r"(?i)\b(\d{1,2}:\d{2})\s*[–-]\s*(\d{1,2}:\d{2})\b")
CUVIER_ROOMS_RE = re.compile(r"(?i)CUVIER[\s\u00A0\-–—-][^,;|\n]+")
WEEKDAY_RE = re.compile(r"(?i)^(monday|tuesday|wednesday|thursday|friday|saturday|sunday|lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)\s*$")
NAME_WORD_RE = re.compile(r"^[A-ZÉÈÀÂÇÎÏÔÛÜ][A-Za-zÉÈÀÂÇÎÏÔÛÜéèàâçïîôöûü'’\-]{2,}$")

def parse_date_full(line: str) -> dt.date | None:
    m = DATE_FULL_RE.match(line.strip()); 
    if not m: return None
    day, month_txt, year = int(m.group(1)), m.group(2).lower(), int(m.group(3))
    month = MONTHS.get(month_txt); 
    if not month: return None
    try: return dt.date(year, month, day)
    except ValueError: return None

def looks_like_names(line: str) -> bool:
    if "," not in line: return False
    tokens = [t.strip() for t in re.split(r"[,\s]+", line) if t.strip()]
    caps_like = sum(1 for t in tokens if NAME_WORD_RE.match(t))
    return caps_like >= 2

def is_people_list(line: str) -> bool:
    return looks_like_names(line)

def is_group_codes(line: str) -> bool:
    return bool(re.search(r"\b(M|L)\d\b|\bUE\b|\bGP\b|\bM1\b|\bM2\b", line))

def is_weekday_header(line: str) -> bool:
    return bool(WEEKDAY_RE.match(line.strip()))

def extract_room(lines: list[str]) -> str | None:
    rooms = []
    for s in lines:
        matches = CUVIER_ROOMS_RE.findall(s)
        if not matches and "CUVIER" in s.upper():
            # Fallback si la regex rate : coupe depuis 'CUVIER' jusqu'à la virgule/fin
            idx = s.upper().find("CUVIER")
            tail = s[idx:]
            cut = re.split(r"[,;|\n]", tail)[0]
            matches = [cut]
        for m in matches:
            v = m.strip().rstrip(" ,;|")
            if v and v.upper() not in (x.upper() for x in rooms):
                rooms.append(v)
    return ", ".join(rooms) if rooms else None


def extract_type(chunk: list[str]) -> str | None:
    for s in reversed(chunk):
        if re.match(r"(?i)^(type\s*:\s*)?réunion\b.*", s) or re.match(r"(?i)^type\s*:\s*\S+", s):
            return s
    return None

def extract_teachers(chunk: list[str]) -> str | None:
    for s in chunk:
        if is_people_list(s):
            return s
    return None

def choose_title(chunk: list[str], room: str | None, type_line: str | None) -> str:
    candidate = None
    for s in chunk:
        if is_weekday_header(s):
            continue
        if room and s.find("CUVIER-") != -1:
            continue
        if type_line and s == type_line:
            continue
        if is_people_list(s):
            continue
        if is_group_codes(s):
            candidate = candidate or s
            continue
        if len(s) <= 3:
            continue
        return s  # ligne descriptive
    return candidate or (chunk[0] if chunk else "Événement")

def parse_specific_day(full_text: str, target_date: dt.date):
    lines = [re.sub(r"\s+", " ", L).strip() for L in full_text.splitlines()]
    lines = [L for L in lines if L]

    current_date, events = None, []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]

        d = parse_date_full(line)
        if d: current_date = d; i += 1; continue

        m = TIME_RANGE_RE.search(line)
        if m and current_date is not None:
            start, end = m.group(1), m.group(2)
            # bloc événement

            after_time = line[m.end():].strip()
            room_inline = extract_room([after_time]) if after_time else None

            chunk, j = [], i + 1
            while j < n:
                nxt = lines[j]
                if TIME_RANGE_RE.search(nxt) or parse_date_full(nxt) or is_weekday_header(nxt):
                    break
                chunk.append(nxt); j += 1

            room = room_inline or extract_room(chunk)
            ev_type = extract_type(chunk)
            teachers = extract_teachers(chunk)
            title = choose_title(chunk, room, ev_type)

            events.append({
                "date": current_date, "start": start, "end": end,
                "title": title, "room": room, "teachers": teachers, "type": ev_type
            })
            i = j; continue

        i += 1

    todays = [e for e in events if e["date"] == target_date]
    todays.sort(key=lambda e: e["start"])
    return todays

# ------------ Récupération ------------
async def fetch_week_text(url: str) -> str:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(url, wait_until="domcontentloaded")
        await page.wait_for_load_state("networkidle", timeout=15000)
        text = await page.locator("body").inner_text()
        await browser.close()
        return text

# ------------ Embeds Discord ------------
def build_embeds(events: list[dict], day_label: str, timestamp_iso: str, week_url: str) -> dict:
    if not events:
        return {"content": f"🗓️ **{day_label}** — *Aucun cours prévu pour demain.*\n<{week_url}>"}

    embeds = []
    for e in events[:10]:  # Discord autorise jusqu'à 10 embeds par message
        fields = []
        fields.append({"name": "🕒 Horaires", "value": f"**{e['start']}–{e['end']}**", "inline": True})
        if e.get("teachers"):
            fields.append({"name": "👩‍🏫 Enseignants", "value": md_escape(e["teachers"])[:1024], "inline": False})
        if e.get("room"):
            fields.append({"name": "🏫 Salle", "value": md_escape(e["room"]), "inline": True})
        if e.get("type"):
            fields.append({"name": "🏷️ Type", "value": md_escape(e["type"])[:1024], "inline": True})

        embed = {
            "title": e["title"][:256],     # Titre = nom du cours
            "type": "rich",
            "url": week_url,               # clic → semaine CELCAT
            "timestamp": timestamp_iso,
            "color": int("E6443A", 16),    # Couleur proche du logo
            "fields": fields,
            "footer": {"text": f"Extrait de CELCAT"}
        }
        embeds.append(embed)

    # petit en-tête simple au-dessus des embeds
    content = f"🗓️ **{day_label}** — emploi du temps de demain :"
    return {"content": content, "embeds": embeds}

def post_discord(payload: dict):
    r = requests.post(WEBHOOK, json=payload, timeout=30)
    r.raise_for_status()

# ------------ Main ------------
async def main():
    now = now_paris()
    tomorrow = (now + dt.timedelta(days=1)).date()
    week_url = week_url_for(tomorrow)
    full_text = await fetch_week_text(week_url)
    events = parse_specific_day(full_text, tomorrow)
    day_label = french_date(tomorrow, capitalize_first=True)
    payload = build_embeds(events, day_label, now.isoformat(), week_url)
    post_discord(payload)

if __name__ == "__main__":
    asyncio.run(main())
