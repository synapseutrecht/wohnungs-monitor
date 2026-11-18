"""Wohnungs-Monitor: prüft die Seiten aus sites.json und meldet Neues per ntfy.

Modus "links": sammelt Inserat-Links, deren URL auf das Regex "pattern" passt,
               öffnet jedes neue Inserat, liest Miete, m² und Zimmer und pusht
               nur passende oder unklare Angebote. Alles landet in funde.md.
Modus "text":  meldet neue Textzeilen (optional nur mit "keywords").
Aufruf mit --dry: zeigt Treffer, ohne zu pushen oder Dateien zu schreiben.
"""
import html as htmlmod
import json, os, re, sys, time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit, unquote

import requests
from bs4 import BeautifulSoup

# ---- Deine Kriterien -------------------------------------------------------
MAX_RENT = 1231        # höchste Kaltmiete in Euro
MIN_BEDROOMS = 1       # mindestens so viele separate Schlafzimmer
CITY = "Utrecht"       # Ortsprüfung für Quellen mit "near"
OTHER_CITIES = re.compile(r"\b(amsterdam|rotterdam|den haag|'s-gravenhage|breda|eindhoven|tilburg|amersfoort|haarlem|leiden|delft"
                          r"|groningen|arnhem|nijmegen|almere|zwolle|den bosch|'s-hertogenbosch|hilversum|bilthoven|laren"
                          r"|harderwijk|nieuwegein|zeist|houten|ijsselstein|maarssen|de bilt|woerden|amstelveen|zoetermeer)\b", re.I)
# ---------------------------------------------------------------------------

SITES = json.loads(Path("sites.json").read_text(encoding="utf-8"))
STATE = Path("state.json")
FUNDE = Path("funde.md")
state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
meta = state.setdefault("_meta", {})
log_rows = state.setdefault("_funde", [])
DRY = "--dry" in sys.argv
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
TOPIC = os.environ.get("NTFY_TOPIC", "")
MAX_PUSH = 5      # höchstens so viele Einzel-Pushes pro Seite und Lauf
MAX_DETAILS = 25  # höchstens so viele Inserate pro Lauf öffnen
NEAR = 1200       # Zeichen-Fenster für den "near"-Filter
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
}
QUOTED = re.compile(r"""["']([^"'<>\s]{2,600})["']""")
LONG_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}-?|-?[0-9a-f]{16,}", re.I)
EURO = re.compile(r"€\s*(\d{1,3}(?:[.,]\d{3})+|\d{3,5})(?:[.,](?:\d{2}|-{1,2}))?(?!\d)")
LABEL = re.compile(
    r"(?P<kale>kale\s+huur\w*|basis\s*huur\w*|basic\s+rent|base\s+rent)"
    r"|(?P<total>totale\s+huur\w*|totaal\w*|total\s+rent|all[\s-]?in\w*|inclusief|incl\.?|including)"
    r"|(?P<neg>servicekosten|service\s*costs?|waarborg|borg|deposit|inkomen|income|salaris|salary|ondernemers"
    r"|voorschot|parkeer\w*|parking|koopsom|vraagprijs|k\.k\.|v\.o\.n)"
    r"|(?P<pos>huurprijs|huur|rent\w*|prijs|price)", re.I)
PER_MONTH = re.compile(r"[\s,.\-]*(p\s*/\s*m|p\.\s*m|/\s*mnd|/\s*maand|/\s*mth|/\s*month|per\s+maand|per\s+month|pm\b)", re.I)
INCL_AFTER = re.compile(r"[\s,.\-]*((p\s*/\s*m|p\.\s*m\.?|/\s*mnd|per\s+maand|per\s+month)\s*)?(incl|inclusief|including|all[\s-]?in)", re.I)
CUT = re.compile(r"vergelijkbare\s+(woningen|objecten|panden)|vergelijkbaar\s+aanbod|soortgelijke\s+woningen|similar\s+(properties|listings|homes)"
                 r"|andere\s+woningen|meer\s+aanbod|bekijk\s+ook|ook\s+interessant|recent\s+bekeken|other\s+(properties|listings)", re.I)
AREA = re.compile(r"(\d{2,3}(?:[.,]\d{1,2})?)\s*(?:m²|m2|m 2)(?![\w²])", re.I)
BED_PATTERNS = [
    r"waarvan\s+(\d)\s+slaapkamers?",
    r"(?:aantal\s+)?slaapkamers?\s*[:|]?\s*(\d)(?![\d.,]|\s*m\b|\s*m²|\s*m2)",
    r"(?:number\s+of\s+)?bedrooms?\s*[:|]?\s*(\d)(?![\d.,]|\s*m\b|\s*m²|\s*m2)",
    r"\b(\d)\s*(?:slaapkamers?|bedrooms?)\b",
]
ROOM_PATTERNS = [
    r"\b(\d{1,2})\s*-?\s*(?:kamer|room)(?:s|appartement|apartment|woning)?\b",
    r"(?<![a-z])kamers\s*[:|]?\s*(\d{1,2})\b",
    r"(?<![a-z])rooms?\s*[:|]?\s*(\d{1,2})\b",
]
STUDENTS_ONLY = re.compile(r"(alleen|uitsluitend|enkel|only)\s+((geschikt|suitable)\s+)?(voor|for)\s+(\d\s+)?student|students?\s+only", re.I)
PHD_OK = re.compile(r"phd|promovend", re.I)
STOP = {"huur", "huren", "utrecht", "woningaanbod", "huurwoningen", "huurwoning", "object", "woning", "wonen",
        "listings", "vastgoed", "appartement", "apartment", "studio", "kamer", "flat", "galerij", "portiek",
        "hoekwoning", "tussenwoning", "en", "nl", "te", "aan", "in", "de", "het", "van", "oz", "wz", "o", "z", "w"}


def push(title, text, click=None, priority=4):
    if DRY:
        print(f"    PUSH: {title} | {text}")
        return
    msg = {"topic": TOPIC, "title": title, "message": text, "priority": priority, "tags": ["house"]}
    if click:
        msg["click"] = click
    requests.post(NTFY_SERVER, json=msg, timeout=15)


def fetch(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.text


def normalize(url):
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc.lower(), p.path.rstrip("/") or "/", "", ""))


def label(url):
    """Lesbare Adresse aus der URL, z. B. "Vredenburgplein 19"."""
    parts = [unquote(x) for x in urlsplit(url).path.split("/") if x]
    while len(parts) > 1 and LONG_ID.fullmatch(parts[-1]):
        parts.pop()
    tail = parts[-1] if parts else url
    if len(parts) > 1 and len(tail) < 7:
        tail = parts[-2] + " " + tail
    tail = LONG_ID.sub(" ", tail.lower())
    tail = re.sub(r"[-+_]+", " ", tail)
    tail = re.sub(r"^woning \d+ |\bh\d{8,}\b|\b\d{4} ?[a-z]{2}\b|\butrecht\b( \d\b)?|\b\d{4} \d{2}$", " ", tail)
    words = tail.split()
    if len(words) > 1 and words[0].isdigit():
        words = words[1:] + words[:1]
    text = " ".join(w if w in ("van", "de", "der", "den", "het", "en", "aan", "te", "in") else w.capitalize() for w in words)
    return (text[:60] + "…") if len(text) > 60 else (text or url)


def address_key(url):
    """Straße + Hausnummer aus der URL, um dieselbe Wohnung bei mehreren Maklern zu erkennen."""
    parts = [unquote(x).lower() for x in urlsplit(url).path.split("/") if x][-2:]
    slug = LONG_ID.sub(" ", " ".join(parts))
    slug = re.sub(r"woning-\d+-|-h\d{8,}|\b\d{4}\s?[a-z]{2}\b", " ", slug)
    tokens = [t for t in re.findall(r"[a-zäöüé]+|\d+", slug) if t not in STOP]
    words = [t for t in tokens if not t.isdigit() and len(t) >= 5]
    numbers = [t for t in tokens if t.isdigit() and len(t) <= 4]
    if not words or not numbers:
        return None
    return " ".join(sorted(set(tokens)))


def find_links(page_url, raw, pattern, near=None):
    text = raw.replace("\\/", "/")
    rx = re.compile(pattern, re.I)
    found = set()
    for m in QUOTED.finditer(text):
        cand = htmlmod.unescape(m.group(1)).strip()
        if not cand.startswith(("http://", "https://", "/")):
            continue
        url = normalize(urljoin(page_url, cand))
        if not rx.search(url):
            continue
        if near and near.lower() not in text[max(0, m.start() - NEAR):m.end() + NEAR].lower():
            continue
        found.add(url)
    found.discard(normalize(page_url))
    return found


def find_lines(raw, selector=None, keywords=None):
    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    roots = soup.select(selector) if selector else [soup.body or soup]
    lines = set()
    for root in roots:
        for line in root.get_text("\n").split("\n"):
            line = re.sub(r"\s+", " ", line).strip()
            if 3 <= len(line) <= 300:
                lines.add(line)
    if keywords:
        kws = [k.lower() for k in keywords]
        lines = {x for x in lines if any(k in x.lower() for k in kws)}
    return lines


def to_euro(num):
    digits = re.sub(r"[.,]", "", num)
    return int(digits) if digits.isdigit() else None


def extract(raw, url):
    """Liest Miete, Fläche und Zimmer aus einer Inseratsseite (Heuristik)."""
    soup = BeautifulSoup(raw, "html.parser")
    heading = " ".join(x.get_text(" ", strip=True) for x in soup.select("title, h1"))
    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content"):
        heading += " " + og["content"]
    for tag in soup(["script", "style", "noscript", "template", "svg"]):
        tag.decompose()
    flat = re.sub(r"\s+", " ", soup.get_text(" "))
    for cut in CUT.finditer(flat):
        if cut.start() > min(300, len(flat) // 4):
            flat = flat[:cut.start()]
            break
    info = {"rent": None, "m2": None, "rooms": None, "bedrooms": None,
            "students_only": False, "heading": heading}

    best = None
    for m in EURO.finditer(flat):
        value = to_euro(m.group(1))
        if not value or not 300 <= value <= 7000:
            continue
        labels = list(LABEL.finditer(flat[max(0, m.start() - 40):m.start()]))
        kind = labels[-1].lastgroup if labels else None
        score = {"kale": 4, "pos": 1, "total": -1, "neg": -5}.get(kind, 0)
        after = flat[m.end():m.end() + 40]
        if PER_MONTH.match(after):
            score += 2
        if INCL_AFTER.match(after):
            score -= 2
        if score >= 2 and (best is None or score > best[0]):
            best = (score, value)
    info["rent"] = best[1] if best else None

    areas = []
    for m in AREA.finditer(flat):
        value = float(m.group(1).replace(",", "."))
        if 10 <= value <= 400:
            ctx = flat[max(0, m.start() - 40):m.start()].lower()
            areas.append((0 if re.search(r"oppervlakte|living area|usable area|wonen|surface", ctx) else 1, value))
    if areas:
        info["m2"] = round(sorted(areas, key=lambda a: a[0])[0][1])

    low = flat.lower()
    for p in BED_PATTERNS:
        m = re.search(p, low)
        if m:
            info["bedrooms"] = int(m.group(1))
            break
    for p in ROOM_PATTERNS:
        m = re.search(p, low)
        if m and 1 <= int(m.group(1)) <= 12:
            info["rooms"] = int(m.group(1))
            break
    if info["rooms"] is None and "studio" in url.lower():
        info["rooms"] = 1
    if STUDENTS_ONLY.search(flat) and not PHD_OK.search(flat):
        info["students_only"] = True
    return info


def judge(info, check_city):
    """Gibt (Urteil, Gründe) zurück: "passt", "unklar" oder "nein"."""
    reasons = []
    head = info["heading"].lower()
    if check_city and CITY.lower() not in head and OTHER_CITIES.search(head):
        reasons.append(f"nicht {CITY}")
    if info["rent"] and info["rent"] > MAX_RENT:
        reasons.append("zu teuer")
    beds = info["bedrooms"]
    if (beds is not None and beds < MIN_BEDROOMS) or (info["rooms"] == 1 and (beds is None or beds <= 1)):
        reasons.append("kein Schlafzimmer")
    if info["students_only"]:
        reasons.append("nur Studierende")
    if reasons:
        return "nein", reasons
    if info["rent"] is None or (info["rooms"] is None and info["bedrooms"] is None):
        return "unklar", ["Angaben fehlen"]
    return "passt", []


def facts(info):
    rent = f"{info['rent']:,} €".replace(",", ".") if info["rent"] else "Miete ?"
    m2 = f"{info['m2']} m²" if info["m2"] else "? m²"
    if info["bedrooms"] is not None:
        rooms = f"{info['bedrooms']} SZ"
    elif info["rooms"] is not None:
        rooms = f"{info['rooms']} Zi."
    else:
        rooms = "Zi. ?"
    return f"{rent} · {m2} · {rooms}"


def now():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/Amsterdam"))
    except Exception:
        return datetime.now()


def write_funde():
    lines = [
        "# Funde des Wohnungs-Monitors",
        "",
        f"Automatisch erzeugt. Kriterien: Kaltmiete bis {MAX_RENT} €, mindestens {MIN_BEDROOMS} Schlafzimmer. "
        "„passt“ und „unklar“ kamen als Push, „nein“ nicht. SZ = Schlafzimmer, Zi. = Zimmer.",
        "",
        "| Gefunden | Urteil | Miete · Fläche · Zimmer | Wohnung | Quelle |",
        "| --- | --- | --- | --- | --- |",
    ]
    for r in log_rows:
        lines.append(f"| {r['when']} | {r['verdict']} | {r['facts']} | [{r['label']}]({r['url']}) | {r['source']} |")
    FUNDE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def log(source, url, lbl, verdict, fact_text):
    log_rows.insert(0, {"when": now().strftime("%d.%m. %H:%M"), "source": source, "url": url,
                        "label": lbl.replace("|", "/"), "verdict": verdict, "facts": fact_text})
    del log_rows[300:]


# Adressen der Inserate der letzten Wochen, damit Doppel-Inserate nicht doppelt melden.
# Nach 30 Tagen ohne Inserat zählt dieselbe Adresse wieder als neu (Neuvermietung).
TODAY = now().date()
addr = state.get("_addr")
if not isinstance(addr, dict):
    addr = {}
    for k, v in state.items():
        if not k.startswith("_") and isinstance(v, list):
            addr.update({key: TODAY.isoformat() for x in v if x.startswith("http") for key in [address_key(x)] if key})


def recent(key):
    return key in addr and (TODAY - datetime.fromisoformat(addr[key]).date()).days <= 30


details_left = MAX_DETAILS

for site in SITES:
    name = site["name"]
    urls = site.get("urls") or [site["url"]]
    mode = site.get("mode", "links")
    items, errors = set(), []
    for url in urls:
        try:
            raw = fetch(url)
        except Exception as e:
            errors.append(f"{url} -> {e}")
            continue
        if mode == "text":
            items |= find_lines(raw, site.get("selector"), site.get("keywords"))
        else:
            items |= find_links(url, raw, site["pattern"], site.get("near"))
        time.sleep(1)

    if DRY:
        print(f"[{name}] {len(items)} Treffer" + (f", Fehler: {errors[0][:150]}" if errors else ""))
        for x in sorted(items)[:5]:
            print("    ", x)
        continue

    m = meta.setdefault(name, {"fails": 0, "empty": 0})
    if errors and not items:
        m["fails"] += 1
        print(f"[{name}] FEHLER {m['fails']}x: {errors[0][:200]}")
        if m["fails"] == 3:
            push(f"Monitor-Fehler: {name}", errors[0][:200], urls[0])
        continue
    m["fails"] = 0
    if not items:
        m["empty"] += 1
        print(f"[{name}] 0 Treffer – Muster prüfen oder Seite lädt per JavaScript")
        if m["empty"] == 6 and state.get(name):
            push(f"Monitor prüfen: {name}", "Seit 6 Läufen 0 Treffer – Layout geändert?", urls[0])
        continue
    m["empty"] = 0

    first_run = name not in state
    seen = set(state.get(name, []))
    new = sorted(items - seen)
    if first_run:
        print(f"[{name}] Erstlauf, {len(items)} gemerkt (kein Push), z. B.:")
        for x in sorted(items)[:2]:
            print("    ", x)
    elif mode == "text":
        for x in new[:MAX_PUSH]:
            push(f"Änderung bei {name}", x, urls[0])
            log(name, urls[0], x[:80], "Projekt", "–")
        print(f"[{name}] {len(items)} Treffer, {len(new)} neu")
    else:
        pushed = 0
        for x in new:
            key = address_key(x)
            if key and recent(key):
                print(f"    Doppel-Inserat übersprungen: {x}")
                continue
            if key:
                addr[key] = TODAY.isoformat()
            lbl = label(x)
            info = {"rent": None, "m2": None, "rooms": None, "bedrooms": None, "students_only": False, "heading": ""}
            if details_left > 0:
                details_left -= 1
                try:
                    info = extract(fetch(x), x)
                except Exception as e:
                    print(f"    Inserat nicht lesbar: {x} ({e})")
                time.sleep(1)
            verdict, reasons = judge(info, bool(site.get("near")))
            shown = verdict if verdict != "nein" else "nein: " + ", ".join(reasons)
            log(name, x, lbl, shown, facts(info))
            print(f"    {shown} | {facts(info)} | {x}")
            if verdict != "nein" and pushed < MAX_PUSH:
                pushed += 1
                head = "Passt" if verdict == "passt" else "Prüfen"
                push(f"{head}: {facts(info)}", f"{lbl} ({name})\n{x}", x, 4 if verdict == "passt" else 3)
        print(f"[{name}] {len(items)} Treffer, {len(new)} neu")
    if mode != "text":
        addr.update({key: TODAY.isoformat() for key in map(address_key, items) if key})
    state[name] = sorted(seen | items)

if not DRY:
    state["_addr"] = {k: v for k, v in sorted(addr.items()) if (TODAY - datetime.fromisoformat(v).date()).days <= 60}
    STATE.write_text(json.dumps(state, indent=1, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    write_funde()
