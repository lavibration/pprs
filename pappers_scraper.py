#!/usr/bin/env python3
"""
Pappers.fr Scraper
Ce script extrait des données d'entreprises de Pappers.fr à partir d'une URL de recherche filtrée.
Il utilise Playwright avec des techniques de contournement anti-bot et de simulation humaine.
"""

import os
import sys
import csv
import time
import random
import logging
from typing import List, Dict, Any, Optional, Tuple
from playwright.sync_api import sync_playwright, Page, BrowserContext, ElementHandle
from playwright_stealth import Stealth

# Configuration du logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("pappers_scraper")

# URL cible par défaut
TARGET_URL = (
    "https://www.pappers.fr/recherche"
    "?geolocalisation=43.2999009436%2C5.38227869795%2C10%2Cv"
    "&siege=true"
    "&chiffre_affaires_min=1000000"
    "&chiffre_affaires_max=10000000"
)

# Liste de User-Agents réalistes pour simuler différents navigateurs récents
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
]

def clean_text(text: Optional[str]) -> str:
    """
    Nettoie une chaîne de caractères en supprimant les espaces insécables,
    les retours à la ligne, et en normalisant les espaces.
    """
    if not text:
        return ""
    # Remplacer les espaces insécables (\xa0) et autres types d'espaces bizarres par des espaces simples
    cleaned = text.replace("\xa0", " ").replace("\u202f", " ")
    cleaned = " ".join(cleaned.split())
    return cleaned.strip()

def clean_numeric_value(value: Optional[str]) -> str:
    """
    Nettoie et formate les valeurs financières (chiffre d'affaires, marge nette)
    en supprimant '€', '%', et en nettoyant les espaces.
    """
    if not value:
        return ""
    cleaned = clean_text(value)
    # Suppression des symboles courants
    cleaned = cleaned.replace("€", "").replace("%", "")
    # Suppression des espaces résiduels pour faciliter l'analyse numérique ultérieure
    cleaned = cleaned.replace(" ", "")
    return cleaned.strip()

def save_to_csv(data: List[Dict[str, str]], filepath: str = "entreprises_pappers.csv") -> None:
    """
    Sauvegarde la liste d'entreprises dans un fichier CSV avec un encodage utf-8-sig
    pour assurer une ouverture propre sous Microsoft Excel.
    """
    if not data:
        logger.warning("Aucune donnée à sauvegarder dans le fichier CSV.")
        return

    fieldnames = ["Nom de l'entreprise", "SIREN", "Chiffre d'affaires", "Marge nette"]

    try:
        with open(filepath, mode="w", encoding="utf-8-sig", newline="") as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames, delimiter=";")
            writer.writeheader()
            for row in data:
                writer.writerow({
                    "Nom de l'entreprise": row.get("Nom de l'entreprise", ""),
                    "SIREN": row.get("SIREN", ""),
                    "Chiffre d'affaires": row.get("Chiffre d'affaires", ""),
                    "Marge nette": row.get("Marge nette", "")
                })
        logger.info(f"Sauvegarde réussie dans {filepath} ({len(data)} entreprises exportées).")
    except Exception as e:
        logger.error(f"Erreur lors de l'écriture du fichier CSV : {e}")

def extract_siren_from_text(text: str) -> Optional[str]:
    """
    Extrait un SIREN (9 chiffres consécutifs) d'un texte.
    """
    import re
    # Recherche 9 chiffres consécutifs, potentiellement séparés par des espaces
    normalized = re.sub(r"\s+", "", text)
    match = re.search(r"\b(\d{9})\b", normalized)
    if match:
        return match.group(1)
    # Autre tentative de recherche au cas où
    match_anywhere = re.search(r"(\d{9})", normalized)
    if match_anywhere:
        return match_anywhere.group(1)
    return None

def extract_financials_from_text(text: str) -> Tuple[str, str]:
    """
    Analyse le texte d'un élément/carte pour en extraire le Chiffre d'Affaires et la Marge Nette.
    """
    import re
    ca = ""
    marge = ""

    # Stratégie 1 : Recherche très précise du Chiffre d'affaires (avec ou sans année)
    # Ex: "Chiffre d'affaires 2022 : 1 183 024 €" ou "Chiffre d'affaires : 1 183 024 €"
    ca_patterns = [
        r"Chiffre\s+d'affaires\s*(?:\d{4})?\s*:\s*([0-9\s \u202f,\.\-]+€)",
        r"(?:Chiffre d'affaires|CA)\s*[:\-]?\s*([0-9\s \u202f,\.\-]+€)",
        r"([0-9\s \u202f,\.\-]+€)\s*(?:\(CA\)|CA|Chiffre d'affaires)"
    ]
    for pattern in ca_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            ca = match.group(1)
            break

    marge_patterns = [
        r"Marge\s+nette\s*:\s*([\-\+]?[0-9\s \u202f,\.\-]+\s*%)",
        r"(?:Marge nette|Marge)\s*[:\-]?\s*([\-\+]?[0-9\s \u202f,\.\-]+\s*%)",
        r"([\-\+]?[0-9\s \u202f,\.\-]+\s*%)\s*(?:Marge nette|Marge)"
    ]
    for pattern in marge_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            marge = match.group(1)
            break

    # Stratégie 2 : Recherche brute si non trouvé (en excluant la ligne du Capital qui est souvent "Capital : XX €")
    if not ca:
        # On essaie d'exclure la ligne du Capital pour éviter de confondre Chiffre d'affaires et Capital Social
        lines = text.split("\n")
        non_capital_lines = [line for line in lines if "capital" not in line.lower()]
        for line in non_capital_lines:
            euro_matches = re.findall(r"([0-9\s \u202f]{3,}(?:,[0-9]+)?\s*€)", line)
            if euro_matches:
                ca = euro_matches[0]
                break

    if not marge:
        # Cherche n'importe quel pourcentage
        pct_matches = re.findall(r"(\-?[0-9]+(?:\s*,\s*[0-9]+)?\s*%)", text)
        if pct_matches:
            marge = pct_matches[0]

    return ca, marge

def parse_company_card(card: ElementHandle, page: Page) -> Dict[str, str]:
    """
    Analyse une carte de résultat d'entreprise pour en extraire le Nom, le SIREN,
    le Chiffre d'Affaires, et la Marge Nette.
    """
    # 1. Extraction du Nom de l'entreprise
    # Pappers affiche typiquement le nom de l'entreprise dans un en-tête bleu ou un lien de titre.
    # Nous essayons plusieurs sélecteurs flexibles
    name = ""
    name_selectors = [
        ".nom-entreprise",
        "h3",
        "h2",
        "a.title",
        "a.resultat-titre",
        ".recherche-resultat-nom",
        "div.flex.justify-between.items-center h3",
        "[class*='titre']",
        "[class*='nom']"
    ]
    for selector in name_selectors:
        try:
            el = card.query_selector(selector)
            if el:
                name = el.inner_text()
                if name:
                    break
        except Exception:
            continue

    if not name:
        # Fallback : premier lien ou première ligne textuelle significative
        try:
            name = card.inner_text().split("\n")[0]
        except Exception:
            name = "Inconnu"

    name = clean_text(name)
    logger.info(f"Analyse de l'entreprise : '{name}'")

    # 2. Extraction du SIREN
    # On regarde d'abord si un lien vers l'entreprise contient un SIREN à 9 chiffres
    siren = ""
    try:
        links = card.query_selector_all("a")
        for link in links:
            href = link.get_attribute("href")
            if href:
                # Exemple de lien: /entreprise/nom-entreprise-123456789
                siren_opt = extract_siren_from_text(href)
                if siren_opt:
                    siren = siren_opt
                    break
    except Exception as e:
        logger.debug(f"Erreur d'extraction du SIREN à partir des liens : {e}")

    # Si le SIREN n'est pas dans l'URL, on le cherche dans le texte de la carte
    card_text = ""
    try:
        card_text = card.inner_text()
    except Exception:
        pass

    if not siren and card_text:
        siren_opt = extract_siren_from_text(card_text)
        if siren_opt:
            siren = siren_opt

    # Fallback ultime spécifié dans les consignes : cliquer sur la carte / le bouton flèche à droite
    # pour ouvrir le détail ou le volet de l'entreprise.
    if not siren:
        logger.info(f"SIREN non trouvé directement pour '{name}'. Tentative d'ouverture du détail...")
        original_url = page.url
        try:
            # On cherche un bouton avec une flèche à droite ou le lien de la carte
            arrow_btn = card.query_selector("button, a.arrow, [class*='arrow'], .resultat-detail-link")
            if arrow_btn:
                arrow_btn.click()
            else:
                card.click()

            # Attendre un court instant que le détail s'affiche
            time.sleep(random.uniform(2.0, 3.0))

            # Essayer d'extraire le SIREN depuis l'URL de la nouvelle page de détail
            current_url = page.url
            siren_opt = extract_siren_from_text(current_url)
            if siren_opt:
                siren = siren_opt
                logger.info(f"SIREN trouvé dans l'URL de détail : {siren}")
            else:
                # Essayer d'extraire le SIREN du DOM global
                body_text = page.inner_text("body")
                siren_opt = extract_siren_from_text(body_text)
                if siren_opt:
                    siren = siren_opt
                    logger.info(f"SIREN trouvé dans la page de détail : {siren}")

            # Si on a navigué vers une nouvelle page, on retourne à la page de recherche précédente
            if page.url != original_url:
                logger.info("Retour à la liste des résultats...")
                page.go_back()
                # Attendre le rechargement de la page de recherche
                page.wait_for_load_state("domcontentloaded", timeout=30000)
                time.sleep(random.uniform(1.5, 2.5))
            else:
                # Si c'était un volet latéral ou une modale, fermer avec Escape
                page.keyboard.press("Escape")
                time.sleep(0.5)
        except Exception as e:
            logger.warning(f"Impossible de cliquer sur la carte pour '{name}' : {e}")
            # Sécurité : s'assurer qu'on retourne bien sur l'URL d'origine si on s'est perdu
            if page.url != original_url:
                try:
                    page.go_back()
                    page.wait_for_load_state("domcontentloaded", timeout=30000)
                except Exception:
                    pass

    # 3. Extraction du Chiffre d'Affaires et de la Marge Nette
    ca_raw = ""
    marge_raw = ""

    # Essayons de chercher des éléments contenant ces étiquettes de façon plus précise d'abord
    try:
        # Pappers affiche souvent les données financières dans un tableau ou sous forme de paires étiquette-valeur.
        # Par exemple : un div contenant "Chiffre d'affaires" et à côté un span/div avec la valeur.
        ca_el = card.query_selector("td:right-of(:text('Chiffre d\'affaires')), div:right-of(:text('Chiffre d\'affaires'))")
        if ca_el:
            ca_raw = ca_el.inner_text()
        marge_el = card.query_selector("td:right-of(:text('Marge nette')), div:right-of(:text('Marge nette'))")
        if marge_el:
            marge_raw = marge_el.inner_text()
    except Exception:
        pass

    # Si non trouvé de manière ciblée, on extrait globalement du texte de la carte
    if (not ca_raw or not marge_raw) and card_text:
        ca_extracted, marge_extracted = extract_financials_from_text(card_text)
        if not ca_raw:
            ca_raw = ca_extracted
        if not marge_raw:
            marge_raw = marge_extracted

    # Nettoyage des valeurs
    ca_clean = clean_numeric_value(ca_raw)
    marge_clean = clean_numeric_value(marge_raw)

    return {
        "Nom de l'entreprise": name,
        "SIREN": siren if siren else "Non trouvé",
        "Chiffre d'affaires": ca_clean if ca_clean else "Non disponible",
        "Marge nette": marge_clean if marge_clean else "Non disponible"
    }

def scrape_pappers(url: str = TARGET_URL, output_file: str = "entreprises_pappers.csv", headless: bool = True) -> List[Dict[str, str]]:
    """
    Fonction principale de scraping de Pappers.fr.
    Parcourt toutes les pages de résultats, extrait les données d'entreprises
    et les exporte dans un fichier CSV.
    """
    all_companies = []
    scraped_sirens = set()

    with sync_playwright() as p:
        user_agent = random.choice(USER_AGENTS)
        logger.info(f"Démarrage du navigateur avec l'User-Agent : {user_agent}")

        # Lancement du navigateur avec une configuration réaliste
        browser = p.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox"
            ]
        )

        context = browser.new_context(
            user_agent=user_agent,
            viewport={"width": 1280, "height": 800},
            locale="fr-FR",
            timezone_id="Europe/Paris"
        )

        # Activation de stealth pour masquer Playwright des scripts anti-bot
        try:
            stealth = Stealth()
            stealth.apply_stealth_sync(context)
        except Exception as e:
            logger.debug(f"Erreur d'initialisation de stealth : {e}")

        page = context.new_page()

        # Navigation vers l'URL
        logger.info(f"Navigation vers l'URL cible : {url}")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            logger.error(f"Erreur lors du chargement initial de la page : {e}")
            browser.close()
            return []

        # Simulation d'un délai humain après chargement
        time.sleep(random.uniform(2.0, 4.0))

        # Gestion des cookies si un bouton d'acceptation est visible
        try:
            # Plusieurs sélecteurs possibles pour les popups de consentement de cookies/pappers
            cookie_btn = page.query_selector(
                "button:has-text('Accepter'), button:has-text('Tout accepter'), #cookies-consent-accept, .cc-btn"
            )
            if cookie_btn and cookie_btn.is_visible():
                cookie_btn.click()
                logger.info("Cookies acceptés.")
                time.sleep(random.uniform(1.0, 2.0))
        except Exception as e:
            logger.debug(f"Pas de bannière de cookies détectée ou erreur de clic : {e}")

        page_number = 1
        while True:
            logger.info(f"--- Scraping de la page {page_number} ---")

            # Faire défiler la page pour simuler une lecture et s'assurer du chargement des images/éléments
            try:
                for _ in range(3):
                    page.evaluate("window.scrollBy(0, window.innerHeight / 2)")
                    time.sleep(random.uniform(0.3, 0.7))
            except Exception as e:
                logger.debug(f"Erreur d'évaluation du scroll : {e}")

            # Sélectionner toutes les cartes ou lignes de résultats
            # Sélecteurs flexibles basés sur l'architecture standard de Pappers
            card_selectors = [
                ".resultat-recherche",
                ".recherche-resultat",
                "div.border.rounded-lg",
                "tr.resultat",
                "[class*='resultat']",
                "a[href*='/entreprise/']"
            ]

            cards = []
            for selector in card_selectors:
                try:
                    cards = page.query_selector_all(selector)
                    if cards:
                        logger.info(f"Trouvé {len(cards)} éléments avec le sélecteur '{selector}'")
                        break
                except Exception:
                    continue

            if not cards:
                # Si aucun sélecteur n'a retourné de cartes, on essaie une recherche d'éléments génériques
                # qui ressemblent à des cartes de résultats (contenant par exemple "SIREN" ou des montants en €)
                logger.warning("Aucun élément de résultat spécifique trouvé. Tentative d'analyse générale...")
                # On essaie d'attendre un peu ou de voir si la page indique aucun résultat
                body_text = page.inner_text("body")
                if "aucun résultat" in body_text.lower():
                    logger.info("La recherche n'a retourné aucun résultat.")
                    break
                else:
                    # On s'arrête proprement s'il n'y a pas d'éléments
                    logger.warning("Aucun résultat exploitable trouvé sur cette page.")
                    break

            # Analyse de chaque carte d'entreprise sur la page en cours
            for card in cards:
                try:
                    company_data = parse_company_card(card, page)
                    if company_data:
                        # Filtrer les faux positifs (comme le résumé du nombre de résultats)
                        name_lower = company_data["Nom de l'entreprise"].lower()
                        if "résultats" in name_lower or "recherche" in name_lower or not company_data["Nom de l'entreprise"]:
                            logger.info(f"Ignorer l'élément faux positif : '{company_data['Nom de l\'entreprise']}'")
                            continue

                        # Déduplication basée sur le SIREN ou le Nom de l'entreprise
                        siren_val = company_data.get("SIREN", "")
                        name_val = company_data.get("Nom de l'entreprise", "")
                        dedup_key = siren_val if (siren_val and siren_val != "Non trouvé") else name_val

                        if dedup_key in scraped_sirens:
                            logger.info(f"Ignorer l'entreprise déjà scrapée (clé : '{dedup_key}')")
                            continue

                        scraped_sirens.add(dedup_key)
                        all_companies.append(company_data)
                except Exception as e:
                    logger.error(f"Erreur d'analyse d'une carte d'entreprise : {e}")

                # Délai aléatoire court entre l'analyse de chaque entreprise pour simuler un humain
                time.sleep(random.uniform(0.5, 1.5))

            # --- Gestion de la pagination ---
            logger.info("Recherche du bouton de pagination suivante...")
            next_btn = None

            # Sélecteurs possibles pour la flèche droite ou bouton page suivante
            next_selectors = [
                "a.pagination-image-right:not(.disabled)",
                "a.pagination.pagination-image-right:not(.disabled)",
                "a.pagination-suivant",
                "button.pagination-suivant",
                "a:has-text('Suivant')",
                "button:has-text('Suivant')",
                ".pagination a:last-child",
                "ul.pagination li:last-child a",
                "svg[class*='right']",
                "a[aria-label='Next']",
                "button[aria-label='Next']",
                # Recherche par texte indicatif de pagination (ex: "1 / 20" ou flèche droite)
                "xpath=//a[contains(., '›') or contains(., 'Suivant') or contains(., 'Next')]",
                "xpath=//button[contains(., '›') or contains(., 'Suivant') or contains(., 'Next')]"
            ]

            for selector in next_selectors:
                try:
                    el = page.query_selector(selector)
                    if el and el.is_visible() and el.is_enabled():
                        # S'assurer que ce n'est pas un bouton de retour
                        btn_text = el.inner_text().strip()
                        if btn_text and any(prev in btn_text.lower() for prev in ["précédent", "previous", "‹"]):
                            continue
                        next_btn = el
                        logger.info(f"Bouton page suivante trouvé avec le sélecteur '{selector}'")
                        break
                except Exception:
                    continue

            if next_btn:
                try:
                    # Récupérer le nom de la première entreprise sur la page actuelle pour détecter la mise à jour
                    first_company_before = ""
                    if cards:
                        try:
                            # Rechercher la première entreprise réelle (pas un faux positif) par sélecteur léger de titre
                            for first_card in cards:
                                el = first_card.query_selector(".nom-entreprise, h3, h2, a.title, a.resultat-titre, .recherche-resultat-nom, [class*='titre']")
                                if el:
                                    name_val = el.inner_text().strip()
                                    if name_val and "résultats" not in name_val.lower() and "recherche" not in name_val.lower():
                                        first_company_before = name_val
                                        break
                        except Exception:
                            pass

                    # Défilement vers le bouton de pagination pour pouvoir cliquer dessus de façon réaliste
                    next_btn.scroll_into_view_if_needed()
                    time.sleep(random.uniform(0.5, 1.0))
                    next_btn.click()
                    page_number += 1

                    # Attendre la mise à jour des résultats
                    if first_company_before:
                        logger.info(f"En attente de la mise à jour des résultats (changement de '{first_company_before}')...")
                        start_time = time.time()
                        updated = False
                        while time.time() - start_time < 15:
                            try:
                                # Ré-obtenir les cartes de résultats
                                for sel in card_selectors:
                                    new_cards = page.query_selector_all(sel)
                                    if new_cards:
                                        # Trouver la première entreprise réelle de la nouvelle sélection
                                        new_first_name = ""
                                        for nc in new_cards:
                                            el = nc.query_selector(".nom-entreprise, h3, h2, a.title, a.resultat-titre, .recherche-resultat-nom, [class*='titre']")
                                            if el:
                                                name_val = el.inner_text().strip()
                                                if name_val and "résultats" not in name_val.lower() and "recherche" not in name_val.lower():
                                                    new_first_name = name_val
                                                    break
                                        if new_first_name and new_first_name != first_company_before:
                                            logger.info(f"Résultats de la page {page_number} chargés avec succès ! Nouvelle première entreprise : '{new_first_name}'")
                                            updated = True
                                            break
                                if updated:
                                    break
                            except Exception:
                                pass
                            time.sleep(0.5)
                    else:
                        page.wait_for_load_state("domcontentloaded", timeout=30000)
                        time.sleep(random.uniform(3.0, 5.0))
                except Exception as e:
                    logger.error(f"Erreur lors du clic sur le bouton de page suivante : {e}")
                    break
            else:
                logger.info("Pas d'autre page disponible ou bouton de page suivante non détecté.")
                break

        # Fermeture du navigateur
        browser.close()

    # Sauvegarde finale des données récupérées
    if all_companies:
        save_to_csv(all_companies, output_file)
    else:
        logger.warning("Aucune entreprise n'a pu être scrapée.")

    return all_companies

if __name__ == "__main__":
    logger.info("Lancement du scraper Pappers.fr...")
    # Mode headed par défaut comme spécifié dans les consignes techniques pour contourner
    # de manière robuste les protections anti-bot (Cloudflare/recaptcha).
    # Pour exécuter en mode invisible (headless), passer headless=True.

    # Boucle de tentative robuste (retry loop) pour contourner d'éventuels blocages temporaires
    max_attempts = 10
    results = []
    for attempt in range(1, max_attempts + 1):
        logger.info(f"Tentative de scraping {attempt}/{max_attempts}...")
        results = scrape_pappers(headless=False)
        if results:
            logger.info(f"Scraping réussi à la tentative {attempt} !")
            break
        logger.warning(f"La tentative {attempt} n'a retourné aucun résultat. Nouvelle tentative dans quelques secondes...")
        time.sleep(random.uniform(3.0, 6.0))

    logger.info(f"Travail terminé. {len(results)} entreprises traitées.")
