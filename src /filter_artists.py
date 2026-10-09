#!/usr/bin/env python3
"""
Filtre des artistes Spotify selon deux conditions :

1. Au moins 60 % des titres visibles sont classés francophones.
2. La dernière sortie repérée date de moins de six mois.

La dernière sortie est repérée comme dans le script fourni :
- ouverture de /discography/all ;
- récupération des liens /album/ dans leur ordre d'apparition ;
- ouverture du premier lien repéré ;
- extraction de la date depuis le HTML de la page de sortie.

Sans --apply :
- aucune suppression n'est appliquée aux fichiers artistes JSON ;
- un rapport est écrit dans data/artist_filter_report.json.

Avec --apply :
- les artistes qui échouent à une condition sont retirés ;
- les cas incertains et les erreurs sont conservés pour vérification.
"""

import argparse
import asyncio
import json
import re
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup
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
ACTIVE_MONTHS = 6

PAGE_TIMEOUT_MS = 45_000
ELEMENT_TIMEOUT_MS = 12_000
NETWORK_IDLE_TIMEOUT_MS = 15_000
REQUEST_DELAY_MS = 700

DEFAULT_LIMIT = None

SPOTIFY_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 "
    "(KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


class ExtractionUncertain(Exception):
    """Les données récupérées ne permettent pas de conclure sûrement."""


def load_json(path, default):
    if not path.exists():
        return default

    try:
        with path.open("r", encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Lecture JSON impossible ({path}) : {exc}") from exc


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")

    with temporary_path.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.write("\n")

    temporary_path.replace(path)


def get_artist_files():
    main_file = DATA_DIR / "artistes.json"
    files = [main_file] if main_file.exists() else []

    numbered_files = []
    for path in DATA_DIR.glob("artistes_*.json"):
        match = re.fullmatch(r"artistes_(\d+)\.json", path.name)
        if match:
            numbered_files.append((int(match.group(1)), path))

    files.extend(path for _, path in sorted(numbered_files))
    return files


def load_all_artist_docs():
    documents = {}

    for path in get_artist_files():
        document = load_json(path, {"artists": {}})

        if not isinstance(document, dict):
            raise RuntimeError(
                f"Format JSON invalide dans {path} : la racine n'est pas un objet"
            )

        artists = document.get("artists")
        if not isinstance(artists, dict):
            raise RuntimeError(
                f"Format JSON invalide dans {path} : "
                "'artists' absent ou non objet"
            )

        documents[path] = document

    return documents


def normalize_spotify_url(value):
    if not isinstance(value, str):
        return ""

    value = value.strip()

    markdown_match = re.fullmatch(
        r"\[([^\]]+)\]\((https?://[^)]+)\)",
        value,
    )
    if markdown_match:
        value = markdown_match.group(2).strip()

    if value.startswith("<") and value.endswith(">"):
        value = value[1:-1].strip()

    return value


def artist_url_for(artist_id, artist):
    """
    Utilise le champ url du JSON s'il contient un lien Spotify valide.
    Sinon, construit l'URL avec l'identifiant de l'artiste.
    """
    url = normalize_spotify_url(artist.get("url", ""))

    if url:
        parsed = urlparse(url)
        is_valid = (
            parsed.scheme in {"http", "https"}
            and "open.spotify.com" in parsed.netloc.lower()
            and "/artist/" in parsed.path
        )
        if is_valid:
            return url.rstrip("/")

    if not re.fullmatch(r"[A-Za-z0-9]+", str(artist_id)):
        raise ExtractionUncertain(
            f"Identifiant artiste invalide pour construire l'URL : {artist_id}"
        )

    return f"https://open.spotify.com/artist/{artist_id}"


def build_discography_url(artist_url):
    clean_url = normalize_spotify_url(artist_url).rstrip("/")

    clean_url = re.sub(
        r"/discography/(all|albums|singles|compilations|appears-on)$",
        "",
        clean_url,
        flags=re.IGNORECASE,
    )

    return f"{clean_url}/discography/all"


def six_months_ago(now=None):
    """Retourne la date calendaire située six mois avant la date donnée."""
    now = now or datetime.now(timezone.utc)
    target_year = now.year
    target_month = now.month - ACTIVE_MONTHS

    while target_month <= 0:
        target_month += 12
        target_year -= 1

    if target_month == 12:
        next_month = date(target_year + 1, 1, 1)
    else:
        next_month = date(target_year, target_month + 1, 1)

    last_day = (next_month - timedelta(days=1)).day
    target_day = min(now.day, last_day)

    return date(target_year, target_month, target_day)


MONTHS = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
    "janvier": 1, "janv": 1,
    "février": 2, "fevrier": 2, "févr": 2, "fevr": 2, "fév": 2, "fev": 2,
    "mars": 3,
    "avril": 4, "avr": 4,
    "mai": 5,
    "juin": 6,
    "juillet": 7, "juil": 7,
    "août": 8, "aout": 8,
    "septembre": 9,
    "octobre": 10,
    "novembre": 11,
    "décembre": 12, "decembre": 12,
}


def normalize_date_text(value):
    value = " ".join(str(value or "").strip().split())
    value = value.replace("\u00a0", " ")

    normalized = unicodedata.normalize("NFD", value)
    return "".join(
        char for char in normalized
        if unicodedata.category(char) != "Mn"
    )


def parse_release_date(value):
    """
    Parse une date Spotify complète, mensuelle ou réduite à une année.

    Une précision limitée à l'année ou au mois est convertie au premier
    jour de cette période. C'est volontairement prudent pour les sorties
    proches de la limite des six mois.
    """
    if not value:
        return None

    original = " ".join(str(value).strip().split()).replace("\u00a0", " ")
    normalized = normalize_date_text(original)

    iso_match = re.search(
        r"\b((?:19|20)\d{2})[-/](\d{1,2})(?:[-/](\d{1,2}))?\b",
        normalized,
    )
    if iso_match:
        year = int(iso_match.group(1))
        month = int(iso_match.group(2))
        day = int(iso_match.group(3) or 1)
        try:
            return date(year, month, day)
        except ValueError:
            return None

    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(original, fmt).date()
        except ValueError:
            pass

    # Formats anglais : January 5, 2026 ou January 5th, 2026.
    match = re.search(
        r"\b([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?[,]?\s+"
        r"((?:19|20)\d{2})\b",
        normalized,
        flags=re.IGNORECASE,
    )
    if match:
        month = MONTHS.get(match.group(1).lower())
        if month:
            try:
                return date(
                    int(match.group(3)),
                    month,
                    int(match.group(2)),
                )
            except ValueError:
                return None

    # Formats français : 5 janvier 2026.
    match = re.search(
        r"\b(\d{1,2})(?:er)?\s+([A-Za-z]+)\s+"
        r"((?:19|20)\d{2})\b",
        normalized,
        flags=re.IGNORECASE,
    )
    if match:
        month = MONTHS.get(match.group(2).lower())
        if month:
            try:
                return date(
                    int(match.group(3)),
                    month,
                    int(match.group(1)),
                )
            except ValueError:
                return None

    # Mois + année, par exemple January 2026 ou janvier 2026.
    match = re.search(
        r"\b([A-Za-z]+)\s+((?:19|20)\d{2})\b",
        normalized,
        flags=re.IGNORECASE,
    )
    if match:
        month = MONTHS.get(match.group(1).lower())
        if month:
            try:
                return date(int(match.group(2)), month, 1)
            except ValueError:
                return None

    # Année seule : on l'interprète au 1er janvier.
    year_match = re.search(r"\b((?:19|20)\d{2})\b", normalized)
    if year_match:
        try:
            return date(int(year_match.group(1)), 1, 1)
        except ValueError:
            return None

    return None


def parse_date_from_element(element):
    values = []

    try:
        values.append(element.get_text(" ", strip=True))
    except Exception:
        pass

    try:
        values.append(str(element))
    except Exception:
        pass

    if hasattr(element, "attrs"):
        for attribute_value in element.attrs.values():
            if isinstance(attribute_value, list):
                values.extend(str(item) for item in attribute_value)
            else:
                values.append(str(attribute_value))

    for value in values:
        parsed = parse_release_date(value)
        if parsed:
            return parsed

    return None


def extract_release_date_from_html(html):
    """
    Cherche une date de sortie dans le HTML, d'abord dans les éléments
    de métadonnées/Encore, puis dans les métadonnées HTML et le texte.
    """
    soup = BeautifulSoup(html, "html.parser")

    for element in soup.select("[data-encore-id]"):
        parsed = parse_date_from_element(element)
        if parsed:
            return parsed

    for element in soup.find_all(True):
        attributes_text = " ".join(
            f"{key}={value}" for key, value in element.attrs.items()
        )

        if not re.search(
            r"date|release|album|metadata",
            attributes_text,
            flags=re.IGNORECASE,
        ):
            continue

        parsed = parse_date_from_element(element)
        if parsed:
            return parsed

    metadata_selectors = [
        ("time", "datetime"),
        ("meta[property='music:release_date']", "content"),
        ("meta[name='release_date']", "content"),
        ("meta[property='release_date']", "content"),
        ("meta[itemprop='datePublished']", "content"),
        ("meta[itemprop='releaseDate']", "content"),
    ]

    for selector, attribute in metadata_selectors:
        for element in soup.select(selector):
            parsed = parse_release_date(element.get(attribute, ""))
            if not parsed:
                parsed = parse_release_date(
                    element.get_text(" ", strip=True)
                )
            if parsed:
                return parsed

    script_patterns = [
        r'"release_date"\s*:\s*"([^"]+)"',
        r'"releaseDate"\s*:\s*"([^"]+)"',
        r'"datePublished"\s*:\s*"([^"]+)"',
    ]

    for script in soup.find_all("script"):
        raw = script.string or script.get_text()
        if not raw:
            continue

        for pattern in script_patterns:
            for match in re.finditer(pattern, raw, flags=re.IGNORECASE):
                parsed = parse_release_date(match.group(1))
                if parsed:
                    return parsed

    visible_text = soup.get_text(" ", strip=True)
    return parse_release_date(visible_text)


def clean_track_title(value):
    if not value:
        return None

    title = re.sub(r"\s+", " ", str(value)).strip()
    if not title or len(title) > 180:
        return None

    if title.casefold() in {
        "play", "pause", "like", "dislike", "more", "plus",
        "écouter", "lire", "suivant",
    }:
        return None

    return title


async def extract_artist_titles(page):
    """Récupère les titres présents dans les liens de morceaux."""
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
        return []

    titles = []
    seen = set()

    for item in items:
        href = item.get("href", "")
        title = clean_track_title(item.get("text"))

        if not title or "/track/" not in href:
            continue

        key = title.casefold()
        if key not in seen:
            seen.add(key)
            titles.append(title)

    return titles


async def extract_album_links(page):
    """
    Récupère les liens /album/ dans leur ordre d'apparition, comme
    dans le script de référence.
    """
    try:
        locator = page.locator("a[href*='/album/']")
        hrefs = await locator.evaluate_all(
            """els => els.map(a => a.href || a.getAttribute('href') || '')"""
        )
    except Exception:
        return []

    links = []
    seen = set()

    for href in hrefs:
        href = normalize_spotify_url(href)
        if not href:
            continue

        if href.startswith("/"):
            href = f"https://open.spotify.com{href}"

        match = re.search(
            r"https?://open\.spotify\.com/"
            r"(?:intl-[^/]+/)?album/([A-Za-z0-9]+)",
            href,
            flags=re.IGNORECASE,
        )
        if not match:
            continue

        album_url = f"https://open.spotify.com/album/{match.group(1)}"

        if album_url not in seen:
            seen.add(album_url)
            links.append(album_url)

    return links


async def wait_for_network_idle(page):
    try:
        await page.wait_for_load_state(
            "networkidle",
            timeout=NETWORK_IDLE_TIMEOUT_MS,
        )
    except Exception:
        # Spotify peut garder des requêtes réseau ouvertes.
        pass


async def get_latest_spotify_release_url(page, artist_id, artist):
    """
    Ouvre la discographie complète et retourne le premier album repéré,
    selon la méthode du script fourni.
    """
    artist_url = artist_url_for(artist_id, artist)
    discography_url = build_discography_url(artist_url)

    response = await page.goto(
        discography_url,
        wait_until="domcontentloaded",
        timeout=PAGE_TIMEOUT_MS,
    )

    if response and response.status >= 400:
        raise ExtractionUncertain(
            f"Discographie inaccessible : HTTP {response.status}"
        )

    try:
        await page.locator("a[href*='/album/']").first.wait_for(
            state="visible",
            timeout=ELEMENT_TIMEOUT_MS,
        )
    except Exception as exc:
        raise ExtractionUncertain(
            f"Aucun lien de sortie visible dans la discographie : {exc}"
        ) from exc

    await wait_for_network_idle(page)

    album_links = await extract_album_links(page)
    if not album_links:
        raise ExtractionUncertain(
            "Aucun lien /album/ exploitable dans la discographie"
        )

    # La méthode demandée traite le premier lien comme dernière sortie.
    return album_links[0]


async def extract_release_date_from_page(page, release_url):
    response = await page.goto(
        release_url,
        wait_until="domcontentloaded",
        timeout=PAGE_TIMEOUT_MS,
    )

    if response and response.status >= 400:
        raise ExtractionUncertain(
            f"Page de sortie inaccessible : HTTP {response.status}"
        )

    await wait_for_spotify(page)
    await wait_for_network_idle(page)

    try:
        await page.locator("body").wait_for(
            state="visible",
            timeout=ELEMENT_TIMEOUT_MS,
        )
        html = await page.content()
    except Exception as exc:
        raise ExtractionUncertain(
            f"Impossible de lire la page de sortie : {exc}"
        ) from exc

    release_date = extract_release_date_from_html(html)
    if release_date is None:
        raise ExtractionUncertain(
            "Date de sortie introuvable dans le HTML de la sortie"
        )

    return release_date


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

    french_count = sum(is_french_title(title) for title in titles)
    return french_count / len(titles), french_count, len(titles)


async def analyse_artist(page, artist_id, artist):
    artist_url = artist_url_for(artist_id, artist)

    response = await page.goto(
        artist_url,
        wait_until="domcontentloaded",
        timeout=PAGE_TIMEOUT_MS,
    )

    if response and response.status >= 400:
        raise ExtractionUncertain(
            f"Page artiste inaccessible : HTTP {response.status}"
        )

    await wait_for_spotify(page)
    await accept_cookies(page)

    try:
        await page.locator("body").wait_for(
            state="visible",
            timeout=ELEMENT_TIMEOUT_MS,
        )
    except Exception as exc:
        raise ExtractionUncertain(
            f"Page artiste non visible : {exc}"
        ) from exc

    await page.wait_for_timeout(1500)

    titles = await extract_artist_titles(page)
    if not titles:
        return {
            "decision": "review",
            "reason": "no_reliable_titles",
            "artist_id": artist_id,
            "name": artist.get("name", artist_id),
            "titles": [],
        }

    ratio, french_count, total_count = french_ratio(titles)
    language_ok = ratio >= FRENCH_THRESHOLD

    result = {
        "artist_id": artist_id,
        "name": artist.get("name", artist_id),
        "french_count": french_count,
        "title_count": total_count,
        "french_ratio": ratio,
        "language_condition": language_ok,
        "titles": titles,
    }

    # La deuxième condition n'est vérifiée que si la première est remplie.
    if not language_ok:
        return {
            **result,
            "decision": "delete",
            "reason": "french_ratio_below_threshold",
        }

    release_url = await get_latest_spotify_release_url(
        page,
        artist_id,
        artist,
    )
    release_date = await extract_release_date_from_page(page, release_url)

    cutoff = six_months_ago()
    date_ok = release_date >= cutoff

    return {
        **result,
        "decision": "keep" if date_ok else "delete",
        "reason": (
            "both_conditions_met"
            if date_ok
            else "latest_release_older_than_six_months"
        ),
        "release_url": release_url,
        "latest_release_date": release_date.isoformat(),
        "date_cutoff": cutoff.isoformat(),
        "date_condition": date_ok,
    }


def print_result(result):
    name = result.get("name", result.get("artist_id"))
    decision = result.get("decision", "review").upper()

    if "french_ratio" in result:
        release_date = result.get("latest_release_date", "non vérifiée")
        print(
            f"[{decision}] {name} | "
            f"français={result['french_count']}/{result['title_count']} "
            f"({result['french_ratio']:.1%}) | "
            f"dernière sortie={release_date} | "
            f"{result.get('reason', '')}",
            flush=True,
        )
    else:
        print(
            f"[{decision}] {name} | {result.get('reason', '')}",
            flush=True,
        )


async def run(args):
    started = time.monotonic()
    documents = load_all_artist_docs()

    total_artists = sum(
        len(document["artists"])
        for document in documents.values()
    )

    print(f"Mode : {'APPLICATION' if args.apply else 'SIMULATION'}")
    print(f"Artistes chargés : {total_artists}")

    stats = {
        "total": total_artists,
        "kept": 0,
        "deleted": 0,
        "review": 0,
        "errors": 0,
    }

    results = []
    processed = 0

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=HEADLESS)

        context = await browser.new_context(
            locale="fr-FR",
            user_agent=SPOTIFY_USER_AGENT,
            viewport={"width": 1440, "height": 1000},
        )

        page = await context.new_page()
        page.set_default_timeout(ELEMENT_TIMEOUT_MS)
        page.set_default_navigation_timeout(PAGE_TIMEOUT_MS)

        await prepare_page(page)

        try:
            for path, document in documents.items():
                artists = document["artists"]
                artist_ids = list(artists.keys())

                for artist_id in artist_ids:
                    if args.limit is not None and processed >= args.limit:
                        break

                    artist = artists[artist_id]
                    processed += 1

                    try:
                        result = await analyse_artist(
                            page,
                            artist_id,
                            artist,
                        )
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

                    elif result.get("reason") == "scrape_error":
                        stats["errors"] += 1

                    else:
                        stats["review"] += 1

                    await page.wait_for_timeout(REQUEST_DELAY_MS)

                # En --apply, sauvegarde chaque fichier traité, même si
                # aucune suppression n'a été faite dans ce fichier.
                if args.apply:
                    save_json(path, document)

                if args.limit is not None and processed >= args.limit:
                    break

        finally:
            await browser.close()

    report = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "mode": "apply" if args.apply else "dry_run",
        "rules": {
            "active_months": ACTIVE_MONTHS,
            "french_threshold": FRENCH_THRESHOLD,
            "french_operator": ">=",
            "both_conditions_required": True,
            "uncertain_data": "keep_and_review",
            "latest_release_method": (
                "discography/all puis premier lien /album/"
            ),
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
    parser = argparse.ArgumentParser(
        description=(
            "Filtre les artistes selon la proportion de titres francophones "
            "et la date de leur dernière sortie."
        )
    )

    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Applique les suppressions aux fichiers artistes. "
            "Sans cette option, le script simule."
        ),
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help="Nombre maximal d'artistes à analyser, utile pour tester.",
    )

    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(run(parse_args()))
