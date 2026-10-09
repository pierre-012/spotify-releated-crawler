#!/usr/bin/env python3
"""
Filtre prudent d'artistes Spotify via Playwright.

Par défaut : simulation, aucun JSON artiste n'est modifié.
Pour appliquer les suppressions : python script.py --apply

Règles :
- Parcourt data/artistes.json et data/artistes_<nombre>.json
- Une erreur de chargement/extraction conserve l'artiste
- Une absence de dates ou de titres est "à vérifier", jamais une suppression
- Suppression uniquement si une date de sortie exploitable est trouvée,
  et que la plus récente date de sortie est antérieure à 6 mois
- Langue déterminée par heuristique sur les titres visibles
- Conservation si strictement plus de 60 % des titres sont classés français
"""

import argparse
import asyncio
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.async_api import async_playwright

from crawler import (
    BASE,
    LANGUAGE,
    HEADLESS,
    prepare_page,
    wait_for_spotify,
    accept_cookies,
)


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"

FRENCH_THRESHOLD = 0.60
ACTIVE_DAYS = 182
PAGE_TIMEOUT_MS = 45_000
ELEMENT_TIMEOUT_MS = 12_000
REQUEST_DELAY_MS = 700

# Limite facultative pour tester un petit échantillon.
# Exemple : python script.py --limit 20
DEFAULT_LIMIT = None


class ExtractionUncertain(Exception):
    """La page ne fournit pas assez d'informations fiables."""


def load_json(path, default):
    if not path.exists():
        return default

    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Lecture JSON impossible ({path}): {exc}") from exc


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")

    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    tmp.replace(path)


def get_artist_files():
    main = DATA_DIR / "artistes.json"
    files = [main] if main.exists() else []

    numbered = []
    for path in DATA_DIR.glob("artistes_*.json"):
        match = re.fullmatch(r"artistes_(\d+)\.json", path.name)
        if match:
            numbered.append((int(match.group(1)), path))

    files.extend(path for _, path in sorted(numbered))
    return files


def load_all_artist_docs():
    docs = {}

    for path in get_artist_files():
        doc = load_json(path, {"artists": {}})
        if not isinstance(doc, dict):
            raise RuntimeError(f"Format JSON invalide dans {path}: racine non objet")

        artists = doc.get("artists")
        if not isinstance(artists, dict):
            raise RuntimeError(f"Format JSON invalide dans {path}: 'artists' absent ou non objet")

        docs[path] = doc

    return docs


def active_cutoff(now=None):
    now = now or datetime.now(timezone.utc)
    return now - timedelta(days=ACTIVE_DAYS)


MONTHS = {
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4,
    "mai": 5, "juin": 6, "juillet": 7, "août": 8, "aout": 8,
    "septembre": 9, "octobre": 10, "novembre": 11,
    "décembre": 12, "decembre": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
    "june": 6, "july": 7, "august": 8, "september": 9,
    "october": 10, "november": 11, "december": 12,
}


def parse_full_date(value):
    """Ne parse que les dates complètes ; une année seule est ignorée."""
    if not value:
        return None

    value = str(value).strip()

    iso = re.search(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", value)
    if iso:
        try:
            return datetime(
                int(iso.group(1)), int(iso.group(2)), int(iso.group(3)),
                tzinfo=timezone.utc,
            )
        except ValueError:
            return None

    text_date = re.search(
        r"\b(\d{1,2})\s+([A-Za-zÀ-ÿ]+)\s+(20\d{2})\b",
        value,
        re.IGNORECASE,
    )
    if text_date:
        month = MONTHS.get(text_date.group(2).lower())
        if month:
            try:
                return datetime(
                    int(text_date.group(3)), month, int(text_date.group(1)),
                    tzinfo=timezone.utc,
                )
            except ValueError:
                return None

    return None


def find_full_dates(text):
    if not text:
        return []

    candidates = re.findall(
        r"\b20\d{2}-\d{1,2}-\d{1,2}\b"
        r"|\b\d{1,2}\s+[A-Za-zÀ-ÿ]+\s+20\d{2}\b",
        text,
        re.IGNORECASE,
    )

    parsed = {d for item in candidates if (d := parse_full_date(item))}
    return sorted(parsed, reverse=True)


async def extract_release_dates(page):
    """
    Collecte les dates complètes visibles dans les libellés du profil.

    La page entière est inspectée comme solution de repli, mais on ne
    considère jamais une année seule comme une date fiable.
    """
    try:
        body_text = await page.locator("body").inner_text(timeout=ELEMENT_TIMEOUT_MS)
    except Exception as exc:
        raise ExtractionUncertain(f"Texte de page inaccessible: {exc}") from exc

    dates = find_full_dates(body_text)

    # Les dates dans les attributs/JSON embarqués ne sont pas utilisées :
    # elles peuvent correspondre à des métadonnées sans lien avec une sortie.
    return dates


def clean_track_title(value):
    if not value:
        return None

    value = re.sub(r"\s+", " ", str(value)).strip()
    if not value or len(value) > 180:
        return None

    if value.casefold() in {"play", "pause", "like", "dislike", "more", "plus"}:
        return None

    return value


async def extract_home_titles(page):
    try:
        locator = page.locator('a[href*="/track/"]')
        await locator.first.wait_for(
            state="visible",
            timeout=ELEMENT_TIMEOUT_MS,
        )
        items = await locator.evaluate_all(
            """els => els.map(a => ({
                href: a.href || a.getAttribute('href') || '',
                text: (a.innerText || a.textContent || '').trim()
            }))"""
        )
    except Exception:
        # Aucun titre visible ne signifie pas qu'il n'existe aucune sortie.
        return []

    titles = []
    seen = set()

    for item in items:
        title = clean_track_title(item.get("text"))
        if not title:
            continue

        # Écarte les liens ne correspondant pas à une URL de track.
        if "/track/" not in item.get("href", ""):
            continue

        key = title.casefold()
        if key not in seen:
            seen.add(key)
            titles.append(title)

    return titles


FRENCH_WORDS = {
    "à", "au", "aux", "avec", "ce", "ces", "cette", "dans", "de", "des",
    "du", "elle", "en", "es", "et", "être", "il", "ils", "je", "la", "le",
    "les", "leur", "lui", "ma", "mais", "me", "mes", "mon", "ne", "nos",
    "notre", "nous", "on", "ou", "où", "par", "pas", "pour", "quand", "que",
    "quel", "quelle", "qui", "sans", "se", "ses", "son", "sur", "ta", "te",
    "tes", "toi", "ton", "tous", "tout", "tu", "un", "une", "vos", "votre",
    "vous", "y", "amour", "comme", "cœur", "coeur", "vie", "femme", "homme",
    "jour", "nuit", "temps", "rêve", "reve", "monde", "soleil", "pluie",
    "frère", "frere", "sœur", "soeur", "enfant", "maison", "maintenant",
    "toujours", "jamais", "encore", "bien", "plus", "moins", "rien",
    "quelque", "chose",
}

ENGLISH_WORDS = {
    "the", "and", "you", "your", "yours", "love", "with", "without", "for",
    "from", "this", "that", "these", "those", "what", "when", "where", "why",
    "how", "my", "me", "we", "they", "them", "our", "night", "day", "girl",
    "boy", "heart", "home", "life", "world", "dream", "fire", "baby", "money",
    "time", "again", "never", "always", "nothing", "something",
}


def is_french_title(title):
    tokens = re.findall(r"[A-Za-zÀ-ÿŒœ]+", title.casefold())
    if not tokens:
        return False

    french_hits = sum(token in FRENCH_WORDS for token in tokens)
    english_hits = sum(token in ENGLISH_WORDS for token in tokens)

    # Un seul mot partagé ou ambigu ne suffit pas.
    if french_hits > english_hits and french_hits >= 2:
        return True

    accents = "àâæçéèêëîïôœùûüÿ"
    has_french_accent = any(char in title.casefold() for char in accents)
    if has_french_accent and english_hits == 0:
        return True

    if len(tokens) >= 3:
        return (
            french_hits / len(tokens) >= 0.5
            and french_hits > english_hits
        )

    return False


def french_ratio(titles):
    if not titles:
        return 0.0, 0, 0

    count = sum(is_french_title(title) for title in titles)
    return count / len(titles), count, len(titles)


async def analyse_artist(page, artist_id, artist):
    url = f"{BASE}/{LANGUAGE}/artist/{artist_id}"

    response = await page.goto(
        url,
        wait_until="domcontentloaded",
        timeout=PAGE_TIMEOUT_MS,
    )

    if response and response.status >= 400:
        raise ExtractionUncertain(f"Réponse HTTP {response.status}")

    await wait_for_spotify(page)
    await accept_cookies(page)

    # Cible explicite : le script ne conclut pas sur une page vide.
    try:
        await page.locator("body").wait_for(
            state="visible",
            timeout=ELEMENT_TIMEOUT_MS,
        )
    except Exception as exc:
        raise ExtractionUncertain(f"Page non visible: {exc}") from exc

    await page.wait_for_timeout(1500)

    dates = await extract_release_dates(page)
    if not dates:
        return {
            "decision": "review",
            "reason": "no_reliable_release_date",
            "artist_id": artist_id,
            "name": artist.get("name", artist_id),
            "release_dates": [],
            "titles": [],
        }

    latest = max(dates)
    cutoff = active_cutoff()

    if latest < cutoff:
        return {
            "decision": "delete",
            "reason": "inactive",
            "artist_id": artist_id,
            "name": artist.get("name", artist_id),
            "latest_release": latest.isoformat(),
            "release_dates": [d.isoformat() for d in dates[:10]],
            "titles": [],
        }

    titles = await extract_home_titles(page)
    if not titles:
        return {
            "decision": "review",
            "reason": "no_reliable_titles",
            "artist_id": artist_id,
            "name": artist.get("name", artist_id),
            "latest_release": latest.isoformat(),
            "release_dates": [d.isoformat() for d in dates[:10]],
            "titles": [],
        }

    ratio, french_count, total = french_ratio(titles)
    result = {
        "decision": "keep" if ratio > FRENCH_THRESHOLD else "review",
        "reason": "active_and_french" if ratio > FRENCH_THRESHOLD else "language_uncertain",
        "artist_id": artist_id,
        "name": artist.get("name", artist_id),
        "latest_release": latest.isoformat(),
        "french_count": french_count,
        "title_count": total,
        "french_ratio": ratio,
        "titles": titles,
    }
    return result


def print_result(result):
    name = result.get("name", result.get("artist_id"))
    decision = result["decision"].upper()
    reason = result["reason"]

    if "french_ratio" in result:
        print(
            f"[{decision}] {name} | {reason} | "
            f"{result['french_count']}/{result['title_count']} français "
            f"({result['french_ratio']:.1%}) | "
            f"latest={result.get('latest_release')}",
            flush=True,
        )
    else:
        print(f"[{decision}] {name} | {reason}", flush=True)


async def run(args):
    started = time.monotonic()
    docs = load_all_artist_docs()

    total = sum(len(doc["artists"]) for doc in docs.values())
    print(f"Mode : {'APPLICATION' if args.apply else 'SIMULATION'}")
    print(f"Artistes chargés : {total}")

    stats = {
        "total": total,
        "kept": 0,
        "deleted": 0,
        "review": 0,
        "errors": 0,
    }
    results = []
    processed = 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS)
        context = await browser.new_context(
            locale="fr-FR",
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/128.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()
        page.set_default_timeout(ELEMENT_TIMEOUT_MS)

        await prepare_page(page)

        for path, doc in docs.items():
            artists = doc["artists"]
            ids = list(artists.keys())

            for artist_id in ids:
                if args.limit is not None and processed >= args.limit:
                    break

                artist = artists[artist_id]
                processed += 1

                try:
                    result = await analyse_artist(page, artist_id, artist)
                except Exception as exc:
                    result = {
                        "decision": "review",
                        "reason": "scrape_error",
                        "artist_id": artist_id,
                        "name": artist.get("name", artist_id),
                        "error": f"{type(exc).__name__}: {exc}",
                    }

                result["file"] = path.name
                results.append(result)
                print_result(result)

                if result["decision"] == "keep":
                    stats["kept"] += 1
                elif result["decision"] == "delete":
                    stats["deleted"] += 1
                    if args.apply:
                        artists.pop(artist_id, None)
                elif result["reason"] == "scrape_error":
                    stats["errors"] += 1
                else:
                    stats["review"] += 1

                await page.wait_for_timeout(REQUEST_DELAY_MS)

            if args.apply:
                save_json(path, doc)

            if args.limit is not None and processed >= args.limit:
                break

        await browser.close()

    report = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "mode": "apply" if args.apply else "dry_run",
        "rules": {
            "active_days": ACTIVE_DAYS,
            "french_threshold": FRENCH_THRESHOLD,
            "french_operator": ">",
            "missing_or_uncertain_data": "keep_and_review",
        },
        "stats": stats,
        "results": results,
    }

    report_path = DATA_DIR / "artist_filter_report.json"
    save_json(report_path, report)

    print("\n=== FIN ===")
    print(f"Total analysé : {processed}")
    print(f"Conservés : {stats['kept']}")
    print(f"Suppressions prévues/appliquées : {stats['deleted']}")
    print(f"À vérifier : {stats['review']}")
    print(f"Erreurs : {stats['errors']}")
    print(f"Rapport : {report_path}")
    print(f"Durée : {time.monotonic() - started:.0f}s")


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Applique les suppressions. Sans cette option, le script simule.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help="Nombre maximal d'artistes à analyser (pour les tests).",
    )
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(run(parse_args()))
