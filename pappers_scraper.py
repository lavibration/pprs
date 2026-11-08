#!/usr/bin/env python3
"""
Pappers.fr Scraper & Financial Screener v2
- Phase 1 : Sourcing rapide via les résultats de recherche Pappers (Marseille 25km, CA cible 800k€ à 3M€).
- Phase 2 : Extraction de la fiche détaillée (https://www.pappers.fr/entreprise/{siren}),
            parsing du tableau financier sur 3 exercices (N, N-1, N-2) et application des 4 critères de screening.
"""

import os
import sys
import csv
import time
import random
import logging
import re
from typing import List, Dict, Any, Optional, Tuple
import numpy as np
from playwright.sync_api import sync_playwright, Page
from playwright_stealth import Stealth

# Configuration du logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("pappers_scraper")

# Configuration du screening (Marseille 25km, CA 800k€ à 3M€)
CONFIG_SCREENING = {
    "output_file": "entreprises_pappers_screened.csv",
    "base_url": (
        "https://www.pappers.fr/recherche"
        "?geolocalisation=43.2965%2C5.3698%2C25%2Cv"
        "&siege=true"
        "&effectifs_min=6"
        "&effectifs_max=500000"
    ),
    # Découpage par tranches de CA pour contourner la limite de pagination (10 pages max)
    "ranges": [
        (800000, 1500000),
        (1500000, 2200000),
        (2200000, 3000000)
    ]
}

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
]

FIELDNAMES = [
    "Nom de l'entreprise",
    "SIREN",
    "Eligible_Screening",
    "CA_N",
    "Moyenne_CA_3ans",
    "StdDev_CA_pct",
    "EBITDA_N",
    "Marge_EBITDA_pct",
    "Cash_Conversion_pct",
    "Treso_Nette_EUR",
    "Ratio_Treso_EBITDA",
    "Raison_Exclusion"
]

def clean_text(text: Optional[str]) -> str:
    """
    Nettoie une chaîne de caractères en normalisant les espaces et espaces insécables.
    """
    if not text:
        return ""
    cleaned = text.replace("\xa0", " ").replace("\u202f", " ")
    cleaned = " ".join(cleaned.split())
    return cleaned.strip()

def parse_pappers_number(val_str: Optional[str]) -> float:
    """
    Convertit des chaînes financières de Pappers ('9,62 M', '79,1 k', '868', '11,6', '-', 'N/A') en float.
    Prend en compte les suffixes (M, k), les décimales (virgules), les signes négatifs et parenthèses.
    """
    if not val_str:
        return 0.0

    cleaned = val_str.replace("\xa0", "").replace("\u202f", "").replace(" ", "")
    cleaned = cleaned.replace("€", "").replace("%", "").replace(",", ".")

    if cleaned in ["-", "", "N/A", "n/a", "ND", "nd", "NC", "nc", "--"]:
        return 0.0

    is_negative = False
    if cleaned.startswith("(") and cleaned.endswith(")"):
        is_negative = True
        cleaned = cleaned[1:-1]
    elif cleaned.startswith("-"):
        is_negative = True
        cleaned = cleaned[1:]

    multiplier = 1.0
    if "M" in cleaned:
        multiplier = 1_000_000.0
        cleaned = cleaned.replace("M", "")
    elif "k" in cleaned:
        multiplier = 1_000.0
        cleaned = cleaned.replace("k", "")

    try:
        val = float(cleaned) * multiplier
        return -val if is_negative else val
    except ValueError:
        return 0.0

def extract_siren_from_text(text: str) -> Optional[str]:
    """
    Extrait un SIREN valide (9 chiffres) d'un texte ou d'une URL.
    """
    if not text:
        return None
    normalized = re.sub(r"\s+", "", text)
    match = re.search(r"\b(\d{9})\b", normalized)
    if match:
        return match.group(1)
    match_anywhere = re.search(r"(\d{9})", normalized)
    if match_anywhere:
        return match_anywhere.group(1)
    return None

def append_to_csv(data: List[Dict[str, Any]], filepath: str) -> None:
    """
    Ajoute les résultats au fichier CSV avec encodage utf-8-sig et séparateur ';'.
    """
    if not data:
        return
    file_exists = os.path.exists(filepath) and os.path.getsize(filepath) > 0
    try:
        with open(filepath, mode="a", encoding="utf-8-sig", newline="") as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=FIELDNAMES, delimiter=";")
            if not file_exists:
                writer.writeheader()
            for row in data:
                writer.writerow({k: row.get(k, "") for k in FIELDNAMES})
    except Exception as e:
        logger.error(f"Erreur d'écriture CSV : {e}")

def parse_financial_rows(rows_data: List[Tuple[str, List[str]]]) -> Dict[str, List[float]]:
    """
    Extrait les séries financières (CA, EBITDA, REX, Trésorerie, Dettes)
    à partir des paires (label, valeurs des cellules).
    """
    data_series = {
        "ca": [],
        "ebitda": [],
        "rex": [],
        "tresorerie": [],
        "dettes_fin": []
    }

    for label_raw, cell_texts in rows_data:
        label = clean_text(label_raw).lower()
        # Les 3 premières colonnes d'exercices correspondent aux 3 dernières années (N, N-1, N-2)
        values = [parse_pappers_number(val) for val in cell_texts[:3]]

        if "chiffre d’affaires" in label or "chiffre d'affaires" in label or label == "ca":
            if not data_series["ca"]:
                data_series["ca"] = values
        elif "ebitda - ebe" in label or "ebitda" in label or "ebe" in label:
            if not data_series["ebitda"]:
                data_series["ebitda"] = values
        elif "résultat d’exploitation" in label or "résultat d'exploitation" in label or "rex" in label:
            if not data_series["rex"]:
                data_series["rex"] = values
        elif "trésorerie" in label or "tresorerie" in label:
            if not data_series["tresorerie"]:
                data_series["tresorerie"] = values
        elif "dettes financières" in label or "dettes financieres" in label:
            if not data_series["dettes_fin"]:
                data_series["dettes_fin"] = values

    return data_series

def scrape_financial_table(page: Page, siren: str, company_url: Optional[str] = None) -> Dict[str, List[float]]:
    """
    Se rend sur la page de détail Pappers et extrait les séries sur 3 ans (N, N-1, N-2).
    """
    detail_url = company_url if (company_url and company_url.startswith("http")) else f"https://www.pappers.fr/entreprise/{siren}"
    logger.info(f"Scraping fiche détaillée : {detail_url}")

    data_series = {
        "ca": [],
        "ebitda": [],
        "rex": [],
        "tresorerie": [],
        "dettes_fin": []
    }

    try:
        page.goto(detail_url, wait_until="domcontentloaded", timeout=45000)
        time.sleep(random.uniform(1.5, 3.5))

        # Cookie banner (si nécessaire sur la page fiche)
        try:
            cookie_btn = page.query_selector("button:has-text('Accepter'), button:has-text('Tout accepter'), #cookies-consent-accept, .cc-btn")
            if cookie_btn and cookie_btn.is_visible():
                cookie_btn.click()
                time.sleep(0.5)
        except Exception:
            pass

        # Recherche du tableau financier HTML
        table = page.query_selector("table.w-full.border-collapse, table[class*='border-collapse'], table.tableau-financier")
        if not table:
            logger.warning(f"Tableau financier introuvable pour SIREN {siren}")
            return data_series

        rows = table.query_selector_all("tr")
        rows_data = []
        for row in rows:
            cells = row.query_selector_all("td, th")
            if len(cells) < 2:
                continue

            label = cells[0].inner_text().strip()
            # Dans le tableau Pappers, la colonne 0 est le libellé.
            # Si la colonne 1 est une unité/variante ou le premier chiffre, on prend les cellules de données
            cell_texts = [c.inner_text().strip() for c in cells[1:]]
            rows_data.append((label, cell_texts))

        data_series = parse_financial_rows(rows_data)

    except Exception as e:
        logger.error(f"Erreur lors de l'extraction de la fiche {siren} : {e}")

    return data_series

def evaluate_financial_criteria(history_data: dict) -> Tuple[bool, dict]:
    """
    Évalue les 4 critères de screening financier sur les 3 derniers exercices :
    1. Taille & Stabilité du CA : 800k€ <= CA_N <= 3M€ et StdDev(CA_3ans) / Mean(CA_3ans) < 15%
    2. Marge EBITDA opérationnelle : EBITDA_N / CA_N >= 12%
    3. Taux de conversion Cash-Flow (Proxy) : REX_N / EBITDA_N >= 75%
    4. Trésorerie Nette / EBITDA : (Trésorerie_N - Dettes_N) / EBITDA_N >= 1.0x
    """
    ca_history = history_data.get("ca", [])
    ebitda_history = history_data.get("ebitda", [])
    rex_history = history_data.get("rex", [])
    treso_history = history_data.get("tresorerie", [])
    dettes_history = history_data.get("dettes_fin", [])

    if len(ca_history) < 3 or ca_history[0] == 0:
        return False, {
            "Eligible_Screening": "NON",
            "CA_N": round(ca_history[0], 2) if ca_history else 0.0,
            "Moyenne_CA_3ans": 0.0,
            "StdDev_CA_pct": 0.0,
            "EBITDA_N": 0.0,
            "Marge_EBITDA_pct": 0.0,
            "Cash_Conversion_pct": 0.0,
            "Treso_Nette_EUR": 0.0,
            "Ratio_Treso_EBITDA": 0.0,
            "Raison_Exclusion": "Historique CA < 3 ans ou CA_N nul"
        }

    ca_n = ca_history[0]
    ebitda_n = ebitda_history[0] if len(ebitda_history) > 0 else 0.0
    rex_n = rex_history[0] if len(rex_history) > 0 else 0.0
    treso_n = treso_history[0] if len(treso_history) > 0 else 0.0
    dettes_n = dettes_history[0] if len(dettes_history) > 0 else 0.0

    # 1. Filtre CA (800k€ - 3M€) & Stabilité 3 ans (Std dev relative < 15%)
    ca_3ans = ca_history[:3]
    ca_mean = float(np.mean(ca_3ans))
    ca_std = float(np.std(ca_3ans))
    std_dev_pct = (ca_std / ca_mean) * 100.0 if ca_mean > 0 else 999.0

    cond_ca_range = (800_000.0 <= ca_n <= 3_000_000.0)
    cond_ca_stability = (std_dev_pct < 15.0)

    # 2. Marge d'EBITDA (>= 12%)
    marge_ebitda = (ebitda_n / ca_n) * 100.0 if ca_n > 0 else 0.0
    cond_ebitda = (marge_ebitda >= 12.0)

    # 3. Cash-Flow Conversion Proxy (REX / EBITDA >= 75%)
    cash_conversion = (rex_n / ebitda_n) * 100.0 if ebitda_n > 0 else 0.0
    cond_cash_conv = (cash_conversion >= 75.0)

    # 4. Trésorerie Nette / EBITDA (>= 1.0x)
    treso_nette = treso_n - dettes_n
    ratio_treso_ebitda = (treso_nette / ebitda_n) if ebitda_n > 0 else -999.0
    cond_treso = (ratio_treso_ebitda >= 1.0)

    is_eligible = bool(cond_ca_range and cond_ca_stability and cond_ebitda and cond_cash_conv and cond_treso)

    reasons = []
    if not cond_ca_range:
        reasons.append(f"CA hors plage ({ca_n:,.0f}€)")
    if not cond_ca_stability:
        reasons.append(f"Instabilité CA (std={std_dev_pct:.1f}%)")
    if not cond_ebitda:
        reasons.append(f"Marge EBITDA faible ({marge_ebitda:.1f}%)")
    if not cond_cash_conv:
        reasons.append(f"Cash Conv. faible ({cash_conversion:.1f}%)")
    if not cond_treso:
        reasons.append(f"Ratio Treso/EBITDA faible ({ratio_treso_ebitda:.2f}x)")

    metrics = {
        "Eligible_Screening": "OUI" if is_eligible else "NON",
        "CA_N": round(ca_n, 2),
        "Moyenne_CA_3ans": round(ca_mean, 2),
        "StdDev_CA_pct": round(std_dev_pct, 2),
        "EBITDA_N": round(ebitda_n, 2),
        "Marge_EBITDA_pct": round(marge_ebitda, 2),
        "Cash_Conversion_pct": round(cash_conversion, 2),
        "Treso_Nette_EUR": round(treso_nette, 2),
        "Ratio_Treso_EBITDA": round(ratio_treso_ebitda, 2),
        "Raison_Exclusion": " / ".join(reasons) if reasons else "Aucune"
    }

    return is_eligible, metrics

def scrape_and_screen(headless: bool = True):
    config = CONFIG_SCREENING
    output_file = config["output_file"]
    base_url = config["base_url"]
    ranges = config["ranges"]

    # Chargement des SIRENs déjà traités pour reprise incrémentale
    processed_sirens = set()
    if os.path.exists(output_file) and os.path.getsize(output_file) > 0:
        try:
            with open(output_file, mode="r", encoding="utf-8-sig") as csvfile:
                reader = csv.DictReader(csvfile, delimiter=";")
                for row in reader:
                    siren = row.get("SIREN", "")
                    if siren:
                        processed_sirens.add(siren)
            logger.info(f"{len(processed_sirens)} SIRENs déjà traités et chargés depuis {output_file}")
        except Exception as e:
            logger.warning(f"Erreur de lecture du CSV existant : {e}")

    with sync_playwright() as p:
        user_agent = random.choice(USER_AGENTS)
        browser = p.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-setuid-sandbox"]
        )
        context = browser.new_context(
            user_agent=user_agent,
            viewport={"width": 1280, "height": 800},
            locale="fr-FR",
            timezone_id="Europe/Paris"
        )
        try:
            Stealth().apply_stealth_sync(context)
        except Exception as e:
            logger.debug(f"Erreur d'initialisation de stealth : {e}")

        page = context.new_page()

        for idx, (min_val, max_val) in enumerate(ranges, 1):
            target_url = f"{base_url}&chiffre_affaires_min={min_val}&chiffre_affaires_max={max_val}"
            logger.info(f"=== Recherche tranche CA {idx}/{len(ranges)} : {min_val:,.0f}€ - {max_val:,.0f}€ ===")

            try:
                page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
                time.sleep(random.uniform(2.0, 3.5))
            except Exception as e:
                logger.error(f"Erreur chargement tranche {min_val}-{max_val} : {e}")
                continue

            try:
                cookie_btn = page.query_selector("button:has-text('Accepter'), button:has-text('Tout accepter'), #cookies-consent-accept, .cc-btn")
                if cookie_btn and cookie_btn.is_visible():
                    cookie_btn.click()
                    time.sleep(1.0)
            except Exception:
                pass

            page_num = 1
            while True:
                logger.info(f"Page {page_num} de la tranche {min_val}-{max_val}")
                card_selectors = [
                    ".resultat-recherche",
                    ".recherche-resultat",
                    "div.border.rounded-lg",
                    "tr.resultat",
                    "[class*='resultat']"
                ]

                cards = []
                for selector in card_selectors:
                    cards = page.query_selector_all(selector)
                    if cards:
                        break

                if not cards:
                    logger.info("Aucun résultat trouvé sur cette page.")
                    break

                sirens_to_process = []
                for card in cards:
                    text = card.inner_text()
                    siren = extract_siren_from_text(text)

                    # Chercher également le lien direct dans la carte
                    direct_url = None
                    try:
                        links = card.query_selector_all("a")
                        for link in links:
                            href = link.get_attribute("href")
                            if href and "/entreprise/" in href:
                                if not siren:
                                    siren = extract_siren_from_text(href)
                                direct_url = f"https://www.pappers.fr{href}" if href.startswith("/") else href
                                break
                    except Exception:
                        pass

                    name_el = card.query_selector(".nom-entreprise, h3, h2, a.title, [class*='titre'], [class*='nom']")
                    name = clean_text(name_el.inner_text()) if name_el else "Inconnu"

                    if siren and siren not in processed_sirens:
                        sirens_to_process.append((name, siren, direct_url))

                logger.info(f"{len(sirens_to_process)} nouvelles entreprises à analyser sur cette page.")

                # PHASE 2 : Visite de la fiche détaillée pour chaque entreprise
                for name, siren, direct_url in sirens_to_process:
                    logger.info(f"Traitement de {name} (SIREN: {siren})...")

                    detail_page = context.new_page()
                    history_data = scrape_financial_table(detail_page, siren, company_url=direct_url)
                    detail_page.close()

                    is_eligible, metrics = evaluate_financial_criteria(history_data)

                    row_data = {
                        "Nom de l'entreprise": name,
                        "SIREN": siren,
                        **metrics
                    }

                    # Sauvegarde incrémentale
                    append_to_csv([row_data], output_file)
                    processed_sirens.add(siren)

                    # Tempo anti-bot inter-fiches (1.5s à 3.5s)
                    sleep_time = random.uniform(1.5, 3.5)
                    logger.info(f"Attente anti-bot de {sleep_time:.2f}s...")
                    time.sleep(sleep_time)

                # Pagination
                next_selectors = [
                    "a.pagination-image-right:not(.disabled)",
                    "a.pagination.pagination-image-right:not(.disabled)",
                    "a.pagination-suivant",
                    "button.pagination-suivant",
                    "a:has-text('Suivant')",
                    "button:has-text('Suivant')"
                ]
                next_btn = None
                for selector in next_selectors:
                    try:
                        el = page.query_selector(selector)
                        if el and el.is_visible() and el.is_enabled():
                            next_btn = el
                            break
                    except Exception:
                        continue

                if next_btn:
                    try:
                        next_btn.scroll_into_view_if_needed()
                        time.sleep(random.uniform(0.5, 1.0))
                        next_btn.click()
                        page_num += 1
                        time.sleep(random.uniform(2.5, 4.0))
                    except Exception as e:
                        logger.error(f"Erreur de pagination : {e}")
                        break
                else:
                    logger.info("Fin des pages pour cette tranche.")
                    break

        browser.close()

if __name__ == "__main__":
    logger.info("Démarrage du script Pappers Screener v2...")
    scrape_and_screen(headless=os.getenv("GITHUB_ACTIONS") == "true")
