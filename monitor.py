"""Wohnungs-Monitor: prüft die Seiten aus sites.json und pusht Neues per ntfy.

Modus "links": sammelt Inserat-Links, deren URL auf das Regex "pattern" passt.
Modus "text":  meldet neue Textzeilen (optional nur mit "keywords").
Aufruf mit --dry: zeigt Treffer, ohne zu pushen oder state.json zu schreiben.
"""
import html as htmlmod
import json, os, re, sys, time
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit, unquote

import requests
from bs4 import BeautifulSoup

SITES = json.loads(Path("sites.json").read_text(encoding="utf-8"))
STATE = Path("state.json")
state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
meta = state.setdefault("_meta", {})
DRY = "--dry" in sys.argv
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
TOPIC = os.environ.get("NTFY_TOPIC", "")
MAX_PUSH = 5      # höchstens so viele Einzel-Pushes pro Seite und Lauf
NEAR = 1200       # Zeichen-Fenster für den "near"-Filter
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
}
QUOTED = re.compile(r"""["']([^"'<>\s]{2,600})["']""")
LONG_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}-?|-?[0-9a-f]{16,}", re.I)


def push(title, text, click=None):
    if DRY:
        print(f"    PUSH: {title} | {text}")
        return
    msg = {"topic": TOPIC, "title": title, "message": text, "priority": 4, "tags": ["house"]}
    if click:
        msg["click"] = click
    requests.post(NTFY_SERVER, json=msg, timeout=15)


def normalize(url):
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc.lower(), p.path.rstrip("/") or "/", "", ""))


def label(url):
    parts = [unquote(x) for x in urlsplit(url).path.split("/") if x]
    tail = parts[-1] if parts else url
    if len(parts) > 1 and len(tail) < 7:
        tail = parts[-2] + " " + tail
    tail = LONG_ID.sub("", tail)
    return re.sub(r"[-+_]+", " ", tail).strip()


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


for site in SITES:
    name = site["name"]
    urls = site.get("urls") or [site["url"]]
    mode = site.get("mode", "links")
    items, errors = set(), []
    for url in urls:
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
        except Exception as e:
            errors.append(f"{url} -> {e}")
            continue
        if mode == "text":
            items |= find_lines(r.text, site.get("selector"), site.get("keywords"))
        else:
            items |= find_links(url, r.text, site["pattern"], site.get("near"))
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
    else:
        for x in new[:MAX_PUSH]:
            if mode == "text":
                push(f"Änderung bei {name}", x, urls[0])
            else:
                push(f"Neu bei {name}", f"{label(x)}\n{x}", x)
        if len(new) > MAX_PUSH:
            push(f"Neu bei {name}", f"+{len(new) - MAX_PUSH} weitere", urls[0])
        print(f"[{name}] {len(items)} Treffer, {len(new)} neu")
    state[name] = sorted(seen | items)

if not DRY:
    STATE.write_text(json.dumps(state, indent=1, ensure_ascii=False, sort_keys=True), encoding="utf-8")
