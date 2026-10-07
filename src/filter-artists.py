#!/usr/bin/env python3

"""
Nettoyage manuel des artistes Spotify.

Règles :
- Parcourt tous les fichiers data/artistes*.json
- Ouvre la page publique Spotify de chaque artiste avec Playwright
- Aucune Spotify API
- Aucune authentification
- Si aucune sortie n'est trouvée -> suppression
- Si dernière sortie >= 6 mois -> suppression
- Analyse les titres visibles sur la page d'accueil
- Moins de 5 titres : accepté, on travaille avec ceux disponibles
- Artiste conservé uniquement si > 60 % des titres sont français
- Tous les autres artistes sont supprimés
- Les erreurs de scraping ne provoquent PAS de suppression destructive
"""

import asyncio
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# Réutilisation du fonctionnement du crawler existant
from crawler import (
    BASE,
    LANGUAGE,
    HEADLESS,
    PAGE_WAIT_MS,
    prepare_page,
    wait_for_spotify,
    accept_cookies,
)


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"

ARTISTS_MAX_PER_FILE = 45000

# Strictement plus de 60 %
FRENCH_THRESHOLD = 0.60

# Une sortie doit dater de moins de 6 mois.
ACTIVE_MONTHS = 6

PAGE_TIMEOUT = 45000


# ============================================================
# JSON
# ============================================================

def load_json(path, default):
    if not path.exists():
        return default

    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        print(
            f"[WARNING] Impossible de lire {path}: {exc}",
            flush=True,
        )
        return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)

    tmp = path.with_suffix(path.suffix + ".tmp")

    with tmp.open("w", encoding="utf-8") as f:
        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2,
        )

    tmp.replace(path)


# ============================================================
# FICHIERS ARTISTES
# ============================================================

def get_artist_files():
    files = []

    main = DATA_DIR / "artistes.json"

    if main.exists():
        files.append(main)

    numbered = []

    for path in DATA_DIR.glob("artistes_*.json"):
        match = re.fullmatch(
            r"artistes_(\d+)\.json",
            path.name,
        )

        if match:
            numbered.append(
                (
                    int(match.group(1)),
                    path,
                )
            )

    numbered.sort(key=lambda item: item[0])

    files.extend(path for _, path in numbered)

    return files


def load_all_artists():
    docs = {}

    for path in get_artist_files():
        doc = load_json(
            path,
            {"artists": {}},
        )

        if not isinstance(doc, dict):
            doc = {"artists": {}}

        if not isinstance(doc.get("artists"), dict):
            doc["artists"] = {}

        docs[path] = doc

    return docs


# ============================================================
# DATE
# ============================================================

FRENCH_MONTHS = {
    "janvier": 1,
    "février": 2,
    "fevrier": 2,
    "mars": 3,
    "avril": 4,
    "mai": 5,
    "juin": 6,
    "juillet": 7,
    "août": 8,
    "aout": 8,
    "septembre": 9,
    "octobre": 10,
    "novembre": 11,
    "décembre": 12,
    "decembre": 12,
}


def parse_date(value):
    if not value:
        return None

    value = str(value).strip()

    # ISO :
    # 2026-10-07
    # 2026-10-07T00:00:00Z
    match = re.search(
        r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b",
        value,
    )

    if match:
        try:
            return datetime(
                int(match.group(1)),
                int(match.group(2)),
                int(match.group(3)),
                tzinfo=timezone.utc,
            )
        except ValueError:
            pass

    # Français :
    # 7 octobre 2026
    match = re.search(
        r"\b(\d{1,2})\s+([A-Za-zÀ-ÿ]+)\s+(20\d{2})\b",
        value,
        re.I,
    )

    if match:
        month = FRENCH_MONTHS.get(
            match.group(2).lower()
        )

        if month:
            try:
                return datetime(
                    int(match.group(3)),
                    month,
                    int(match.group(1)),
                    tzinfo=timezone.utc,
                )
            except ValueError:
                pass

    # Anglais au cas où Spotify retourne l'interface en anglais.
    english_months = {
        "january": 1,
        "february": 2,
        "march": 3,
        "april": 4,
        "may": 5,
        "june": 6,
        "july": 7,
        "august": 8,
        "september": 9,
        "october": 10,
        "november": 11,
        "december": 12,
    }

    match = re.search(
        r"\b(\d{1,2})\s+([A-Za-z]+)\s+(20\d{2})\b",
        value,
        re.I,
    )

    if match:
        month = english_months.get(
            match.group(2).lower()
        )

        if month:
            try:
                return datetime(
                    int(match.group(3)),
                    month,
                    int(match.group(1)),
                    tzinfo=timezone.utc,
                )
            except ValueError:
                pass

    # Année seule.
    match = re.search(
        r"\b(20\d{2})\b",
        value,
    )

    if match:
        try:
            return datetime(
                int(match.group(1)),
                12,
                31,
                tzinfo=timezone.utc,
            )
        except ValueError:
            pass

    return None


def active_cutoff():
    """
    Retourne une date approximative à 6 mois dans le passé.

    On utilise 182 jours afin d'éviter les problèmes de longueur
    variable des mois.
    """
    return datetime.now(timezone.utc) - timedelta(days=182)


# ============================================================
# EXTRACTION DES DATES DE SORTIES
# ============================================================

async def extract_release_dates(page):
    """
    Extrait les dates visibles sur la page artiste.

    Spotify modifie régulièrement son DOM. On utilise donc plusieurs
    sources plutôt qu'un sélecteur unique.
    """

    dates = []

    # --------------------------------------------------------
    # 1. Texte visible
    # --------------------------------------------------------

    try:
        body = await page.locator("body").inner_text(
            timeout=10000
        )

        dates.extend(
            extract_dates_from_text(body)
        )

    except Exception:
        pass

    # --------------------------------------------------------
    # 2. HTML / attributs / JSON embarqué
    # --------------------------------------------------------

    try:
        html = await page.content()

        # Dates ISO présentes dans les données Spotify embarquées.
        for match in re.findall(
            r"\b20\d{2}-\d{2}-\d{2}\b",
            html,
        ):
            parsed = parse_date(match)

            if parsed:
                dates.append(parsed)

    except Exception:
        pass

    return sorted(
        set(dates),
        reverse=True,
    )


def extract_dates_from_text(text):
    dates = []

    if not text:
        return dates

    # ISO
    for value in re.findall(
        r"\b20\d{2}-\d{1,2}-\d{1,2}\b",
        text,
    ):
        parsed = parse_date(value)

        if parsed:
            dates.append(parsed)

    # Français / anglais
    month_pattern = (
        r"(?:"
        + "|".join(
            list(FRENCH_MONTHS.keys())
            + [
                "january",
                "february",
                "march",
                "april",
                "may",
                "june",
                "july",
                "august",
                "september",
                "october",
                "november",
                "december",
            ]
        )
        + r")"
    )

    for value in re.findall(
        rf"\b\d{{1,2}}\s+{month_pattern}\s+20\d{{2}}\b",
        text,
        re.I,
    ):
        parsed = parse_date(value)

        if parsed:
            dates.append(parsed)

    return dates


# ============================================================
# EXTRACTION DES TITRES
# ============================================================

async def extract_home_titles(page):
    """
    Récupère les titres Spotify visibles sur l'accueil du profil.

    On privilégie les liens /track/.
    On ne demande volontairement PAS 5 titres minimum :
    s'il n'y en a que 1, 2, 3 ou 4, ils sont analysés.
    """

    titles = []

    try:
        links = await page.locator(
            'a[href*="/track/"]'
        ).evaluate_all(
            """
            els => els.map(a => ({
                href: a.href || a.getAttribute('href') || '',
                text: (a.innerText || a.textContent || '').trim()
            }))
            """
        )

        seen = set()

        for item in links:
            title = clean_track_title(
                item.get("text", "")
            )

            if not title:
                continue

            key = title.casefold()

            if key in seen:
                continue

            seen.add(key)
            titles.append(title)

    except Exception as exc:
        print(
            f"[WARNING] Extraction titres impossible: {exc}",
            flush=True,
        )

    return titles


def clean_track_title(value):
    if not value:
        return None

    value = re.sub(
        r"\s+",
        " ",
        str(value),
    ).strip()

    if not value:
        return None

    # Évite de considérer toute une carte Spotify comme un titre.
    if len(value) > 180:
        return None

    # Quelques éléments de navigation.
    invalid = {
        "play",
        "pause",
        "like",
        "dislike",
        "more",
        "plus",
    }

    if value.casefold() in invalid:
        return None

    return value


# ============================================================
# LANGUE FRANÇAISE
# ============================================================

FRENCH_WORDS = {
    "a",
    "à",
    "au",
    "aux",
    "avec",
    "ce",
    "ces",
    "cette",
    "dans",
    "de",
    "des",
    "du",
    "elle",
    "en",
    "es",
    "et",
    "être",
    "il",
    "ils",
    "je",
    "la",
    "le",
    "les",
    "leur",
    "lui",
    "ma",
    "mais",
    "me",
    "mes",
    "mon",
    "ne",
    "nos",
    "notre",
    "nous",
    "on",
    "ou",
    "où",
    "par",
    "pas",
    "pour",
    "quand",
    "que",
    "quel",
    "quelle",
    "qui",
    "sans",
    "se",
    "ses",
    "son",
    "sur",
    "ta",
    "te",
    "tes",
    "toi",
    "ton",
    "tous",
    "tout",
    "tu",
    "un",
    "une",
    "vos",
    "votre",
    "vous",
    "y",
    "amour",
    "avec",
    "comme",
    "cœur",
    "coeur",
    "vie",
    "femme",
    "homme",
    "jour",
    "nuit",
    "temps",
    "rêve",
    "reve",
    "monde",
    "soleil",
    "pluie",
    "frère",
    "frere",
    "sœur",
    "soeur",
    "enfant",
    "maison",
    "maintenant",
    "toujours",
    "jamais",
    "encore",
    "bien",
    "plus",
    "moins",
    "rien",
    "quelque",
    "chose",
}

ENGLISH_WORDS = {
    "the",
    "and",
    "you",
    "your",
    "yours",
    "love",
    "with",
    "without",
    "for",
    "from",
    "this",
    "that",
    "these",
    "those",
    "what",
    "when",
    "where",
    "why",
    "how",
    "my",
    "me",
    "we",
    "they",
    "them",
    "our",
    "night",
    "day",
    "girl",
    "boy",
    "heart",
    "home",
    "life",
    "world",
    "dream",
    "fire",
    "baby",
    "money",
    "time",
    "again",
    "never",
    "always",
    "nothing",
    "something",
}


def tokenize_title(title):
    return re.findall(
        r"[A-Za-zÀ-ÿŒœ]+",
        title.lower(),
    )


def is_french_title(title):
    """
    Classification volontairement conservatrice.

    Pour des titres très courts ou constitués uniquement de noms propres,
    on préfère ne pas les déclarer français sans indice linguistique.

    Les accents français forts sont un indice supplémentaire.
    """

    if not title:
        return False

    tokens = tokenize_title(title)

    if not tokens:
        return False

    normalized = title.lower()

    # Indices très forts.
    french_accents = (
        "à",
        "â",
        "æ",
        "ç",
        "é",
        "è",
        "ê",
        "ë",
        "î",
        "ï",
        "ô",
        "œ",
        "ù",
        "û",
        "ü",
        "ÿ",
    )

    accent_score = sum(
        normalized.count(char)
        for char in french_accents
    )

    french_hits = sum(
        1
        for token in tokens
        if token in FRENCH_WORDS
    )

    english_hits = sum(
        1
        for token in tokens
        if token in ENGLISH_WORDS
    )

    # Présence d'un mot français explicite.
    if french_hits > english_hits and french_hits >= 1:
        return True

    # Accent français + absence de dominance anglaise.
    if accent_score > 0 and english_hits == 0:
        return True

    # Pour un titre composé de plusieurs mots, une majorité française
    # est nécessaire.
    if len(tokens) >= 3:
        french_ratio = french_hits / len(tokens)

        if french_ratio >= 0.5 and french_hits > english_hits:
            return True

    return False


def calculate_french_ratio(titles):
    if not titles:
        return 0.0, 0, 0

    french_count = sum(
        1
        for title in titles
        if is_french_title(title)
    )

    total = len(titles)

    return (
        french_count / total,
        french_count,
        total,
    )


# ============================================================
# ANALYSE D'UN ARTISTE
# ============================================================

async def analyse_artist(page, artist_id, artist):
    url = (
        f"{BASE}/{LANGUAGE}"
        f"/artist/{artist_id}"
    )

    print(
        f"[ARTIST] {artist_id} -> {url}",
        flush=True,
    )

    await page.goto(
        url,
        wait_until="domcontentloaded",
        timeout=PAGE_TIMEOUT,
    )

    await wait_for_spotify(page)

    await accept_cookies(page)

    # Petit scroll pour forcer le rendu des sections de l'accueil.
    await page.mouse.wheel(0, 900)
    await page.wait_for_timeout(800)

    # --------------------------------------------------------
    # SORTIES
    # --------------------------------------------------------

    release_dates = await extract_release_dates(page)

    if not release_dates:
        return {
            "decision": "delete",
            "reason": "no_release",
            "artist_id": artist_id,
            "name": artist.get("name", artist_id),
            "titles": [],
            "release_dates": [],
        }

    latest_release = max(release_dates)

    cutoff = active_cutoff()

    if latest_release < cutoff:
        return {
            "decision": "delete",
            "reason": "inactive",
            "artist_id": artist_id,
            "name": artist.get("name", artist_id),
            "latest_release": latest_release.isoformat(),
            "titles": [],
            "release_dates": [
                value.isoformat()
                for value in release_dates[:10]
            ],
        }

    # --------------------------------------------------------
    # TITRES DE L'ACCUEIL
    # --------------------------------------------------------

    titles = await extract_home_titles(page)

    if not titles:
        return {
            "decision": "delete",
            "reason": "no_titles",
            "artist_id": artist_id,
            "name": artist.get("name", artist_id),
            "latest_release": latest_release.isoformat(),
            "titles": [],
        }

    french_ratio, french_count, total_count = (
        calculate_french_ratio(titles)
    )

    print(
        f"[CHECK] "
        f"{artist.get('name', artist_id)} | "
        f"latest={latest_release.date()} | "
        f"French={french_count}/{total_count} "
        f"({french_ratio:.1%})",
        flush=True,
    )

    # IMPORTANT :
    # > 60 %, pas >= 60 %.
    if french_ratio <= FRENCH_THRESHOLD:
        return {
            "decision": "delete",
            "reason": "not_french_enough",
            "artist_id": artist_id,
            "name": artist.get("name", artist_id),
            "latest_release": latest_release.isoformat(),
            "french_count": french_count,
            "title_count": total_count,
            "french_ratio": french_ratio,
            "titles": titles,
        }

    return {
        "decision": "keep",
        "reason": "active_and_french",
        "artist_id": artist_id,
        "name": artist.get("name", artist_id),
        "latest_release": latest_release.isoformat(),
        "french_count": french_count,
        "title_count": total_count,
        "french_ratio": french_ratio,
        "titles": titles,
    }


# ============================================================
# RAPPORT
# ============================================================

def print_result(result):
    name = result.get(
        "name",
        result.get("artist_id"),
    )

    decision = result["decision"]
    reason = result["reason"]

    if decision == "keep":
        print(
            f"[KEEP] {name} | "
            f"{result['french_count']}/{result['title_count']} "
            f"français "
            f"({result['french_ratio']:.1%}) | "
            f"latest={result['latest_release']}",
            flush=True,
        )

    elif reason == "inactive":
        print(
            f"[DELETE] {name} | INACTIF | "
            f"latest={result.get('latest_release')}",
            flush=True,
        )

    elif reason == "no_release":
        print(
            f"[DELETE] {name} | AUCUNE SORTIE",
            flush=True,
        )

    elif reason == "no_titles":
        print(
            f"[DELETE] {name} | AUCUN TITRE",
            flush=True,
        )

    else:
        print(
            f"[DELETE] {name} | {reason} | "
            f"{result.get('french_ratio', 0):.1%}",
            flush=True,
        )


# ============================================================
# MAIN
# ============================================================

async def main():
    started = time.monotonic()

    print("=" * 70)
    print("SPOTIFY ARTIST FILTER")
    print("=" * 70)
    print("Mode : manuel")
    print("API Spotify : NON")
    print("Source : Spotify Web public + Playwright")
    print("Actif : dernière sortie < 6 mois")
    print("Francophone : > 60 % des titres")
    print("=" * 70)

    docs = load_all_artists()

    if not docs:
        print("[INFO] Aucun fichier artistes trouvé.")
        return

    total = sum(
        len(doc["artists"])
        for doc in docs.values()
    )

    print(
        f"[INFO] {len(docs)} fichier(s) | "
        f"{total} artiste(s)",
        flush=True,
    )

    stats = {
        "total": total,
        "kept": 0,
        "deleted": 0,
        "no_release": 0,
        "inactive": 0,
        "not_french_enough": 0,
        "no_titles": 0,
        "errors": 0,
    }

    errors = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=HEADLESS,
        )

        context = await browser.new_context(
            locale="fr-FR",
            user_agent=(
                "Mozilla/5.0 "
                "(X11; Linux x86_64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/128.0.0.0 "
                "Safari/537.36"
            ),
        )

        page = await context.new_page()

        await prepare_page(page)

        for path, doc in docs.items():
            artists = doc["artists"]

            print()
            print(
                f"=================================================="
            )
            print(
                f"[FILE] {path.name} | "
                f"{len(artists)} artistes"
            )
            print(
                f"=================================================="
            )

            to_delete = []

            # Copie de la liste pour pouvoir supprimer proprement
            # pendant que le dictionnaire original reste stable.
            for artist_id, artist in list(
                artists.items()
            ):
                try:
                    result = await analyse_artist(
                        page,
                        artist_id,
                        artist,
                    )

                    print_result(result)

                    reason = result["reason"]

                    if result["decision"] == "keep":
                        stats["kept"] += 1
                        continue

                    to_delete.append(artist_id)

                    stats["deleted"] += 1

                    if reason in stats:
                        stats[reason] += 1

                except (
                    PlaywrightTimeoutError,
                    Exception,
                ) as exc:

                    stats["errors"] += 1

                    errors.append(
                        {
                            "artist_id": artist_id,
                            "name": artist.get(
                                "name",
                                artist_id,
                            ),
                            "file": path.name,
                            "error": (
                                f"{type(exc).__name__}: "
                                f"{exc}"
                            ),
                        }
                    )

                    # IMPORTANT :
                    # Une erreur technique ne supprime pas
                    # l'artiste.
                    print(
                        f"[ERROR] "
                        f"{artist.get('name', artist_id)} | "
                        f"{type(exc).__name__}: {exc} | "
                        f"ARTISTE CONSERVÉ",
                        flush=True,
                    )

                await page.wait_for_timeout(700)

            # ------------------------------------------------
            # SUPPRESSION APRÈS ANALYSE DU FICHIER
            # ------------------------------------------------

            for artist_id in to_delete:
                artists.pop(
                    artist_id,
                    None,
                )

            # Sauvegarde immédiate du fichier.
            save_json(
                path,
                doc,
            )

            print(
                f"[FILE DONE] {path.name} | "
                f"supprimés={len(to_delete)} | "
                f"restants={len(artists)}",
                flush=True,
            )

        await browser.close()

    # ========================================================
    # RAPPORT
    # ========================================================

    report = {
        "run_at": datetime.now(
            timezone.utc
        ).isoformat(),

        "rules": {
            "active_months": ACTIVE_MONTHS,
            "french_threshold": FRENCH_THRESHOLD,
            "french_operator": ">",
        },

        "stats": stats,

        "errors": errors,
    }

    report_path = (
        DATA_DIR / "artist_filter_report.json"
    )

    save_json(
        report_path,
        report,
    )

    elapsed = time.monotonic() - started

    print()
    print("=" * 70)
    print("FILTER FINISHED")
    print("=" * 70)
    print(f"Total          : {stats['total']}")
    print(f"Conservés      : {stats['kept']}")
    print(f"Supprimés      : {stats['deleted']}")
    print(f"  Sans sortie  : {stats['no_release']}")
    print(f"  Inactifs     : {stats['inactive']}")
    print(
        f"  Non FR       : "
        f"{stats['not_french_enough']}"
    )
    print(f"  Sans titres  : {stats['no_titles']}")
    print(f"Erreurs        : {stats['errors']}")
    print(f"Durée          : {elapsed:.0f}s")
    print(f"Rapport        : {report_path}")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
