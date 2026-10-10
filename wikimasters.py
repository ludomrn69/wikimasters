#!/usr/bin/env python3
"""Bot WikiMasters : trie les cartes de votre collection selon config.yaml (et vos réglages de perso.yaml).

    ./wm login        ajoute un compte (cookie du navigateur, voir README)
    ./wm tout         analyser, puis vendre, puis defausser
    ./wm analyser     lit les prix moyens et pose les étiquettes de prix (defausse, +5, +10…)
    ./wm vendre       met aux enchères les cartes étiquetées à vendre (places libres)
    ./wm defausser    défausse les cartes « defausse » (et les « inconnu » des raretés choisies)
    ./wm boosters     ouvre les boosters disponibles
    ./wm acheter      mise à la dernière seconde sur les enchères du marché qui correspondent à buy.keywords
    ./wm comptes      liste les comptes enregistrés

Sans --execute, chaque commande est une simulation : elle affiche ce qu'elle ferait sans rien modifier.
Sous Windows : wm à la place de ./wm. Sans le lanceur : python wikimasters.py …
"""

import argparse
import base64
import binascii
import csv
import datetime
import difflib
import html
import json
import math
import os
import pathlib
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from numbers import Real
from urllib.parse import unquote, urlparse

import requests
import yaml

# Durées d'enchère proposées par le site (en minutes).
KNOWN_DURATIONS = {10, 30, 60, 180, 360, 720}
# Le site n'accepte pas plus de 5 enchères en cours en même temps (lu aussi sur /api/marketplace/mine).
SITE_MAX_AUCTIONS = 5
PAGE_SIZE = 50
# Défausses par lots : prix relus pour 10 cartes, ces 10 cartes défaussées, et ainsi de suite.
DISCARD_BATCH = 10
MAX_PAGES = 500
# Le jeton d'accès est renouvelé quand il lui reste moins que cette marge (secondes).
REFRESH_MARGIN = 300
# Taille maximale d'un morceau de cookie, comme le fait la bibliothèque Supabase du site.
COOKIE_CHUNK_SIZE = 3180
# Nouvelles tentatives pour une lecture (GET) en cas de coupure réseau, 429 ou 5xx : ~3 min de coupure tolérée.
GET_RETRY_DELAYS = (2, 10, 30, 60, 90)
# Nouvelles tentatives pour une écriture (étiquette, défausse, vente) : ~4 min de panne du serveur tolérée.
WRITE_RETRY_DELAYS = (10, 30, 60, 120)
# Réponses qui garantissent que le serveur n'a rien fait : même une défausse ou une vente peut être renvoyée.
# (429 trop de requêtes, 503 indisponible, 521/522/523/525 Cloudflare n'a pas pu joindre le serveur.)
NOT_PROCESSED = {429, 503, 521, 522, 523, 525}
# Après une erreur du serveur, toutes les pauses sont doublées (jusqu'à ×8), puis reviennent peu à peu à la normale.
MAX_SLOWDOWN = 8
# Vérification anti-robot demandée par le site : ce type d'action est mis en pause 1 h, puis 2 h, 4 h, 8 h
# si le site la redemande. Le reste du passage continue.
ANTIBOT_PAUSES = (3600, 7200, 14400, 28800)
ANTIBOT_CODE = "human_verification_required"
ANTIBOT_HINT = ("Ouvrez wiki-masters.com dans votre navigateur avec ce compte et faites l'action à la main "
                "(passez la vérification si elle s'affiche), puis relancez le script.")
ANTIBOT_GROUPS = {"auction": "mises en vente", "discard": "défausses", "add_tag": "étiquettes",
                  "remove_tag": "étiquettes", "pack": "boosters", "bid": "achats"}
# Nouvelles tentatives du renouvellement de session (réseau, 429, 5xx). Rapides : si la première demande était
# arrivée, Supabase accepte encore l'ancien jeton pendant quelques secondes, au-delà il révoquerait la session.
REFRESH_RETRY_DELAYS = (1, 3)
MAYBE_DONE = "L'action a peut-être été effectuée : vérifiez sur le site."
COMMANDS = ("tout", "analyser", "vendre", "defausser", "boosters", "acheter")
# Achats : après la mise de buy.snipe_seconds, dernière vérification de l'enchère quand il reste ce nombre de
# secondes (pour répondre à une surenchère de dernière minute).
LAST_CHECK_SECONDS = 6
# Une mise tardive fait prolonger l'enchère par le site (+60 s, vu le 10/10/2026) : l'enchère suivie est revue
# ce nombre de secondes après sa fin prévue ; prolongée, tout recommence (mise, vérification) avant la nouvelle fin.
AFTER_END_SECONDS = 3
# Aucune autre requête ne part quand une vérification d'enchère tombe dans les N secondes : le site met parfois
# plusieurs secondes à répondre, et la mise arriverait trop tard.
CHECK_MARGIN = 10
# Résultat d'une enchère où le script a misé : lu une minute après sa fin, puis toutes les 5 minutes.
RESULT_DELAY, RESULT_RETRY = 60, 300
# Pages du marché lues au plus par mot-clé et par relecture (triées par fin la plus proche).
MAX_SCAN_PAGES = 5
# Réglages personnels (et jeton Telegram) par-dessus config.yaml, à côté de lui et jamais envoyés sur GitHub.
PERSO_FILE = "perso.yaml"
# Un dossier par compte : session, suivi des ventes, verrou.
ACCOUNTS_DIR = "comptes"
# Commande affichée dans les messages : le lanceur (./wm ou wm) la précise.
CMD = os.environ.get("WM_CMD") or "python wikimasters.py"


def cmd(args):
    return f"{CMD} {args}"


class ApiError(Exception):
    """Erreur d'API. fatal=True arrête le passage, sinon seule l'action en cours est abandonnée."""

    def __init__(self, message, fatal=False, status=None, code=None):
        super().__init__(message)
        self.fatal = fatal
        self.status = status
        self.code = code  # code d'erreur renvoyé par le site ou Supabase (ex. "23505")

    @property
    def antibot(self):
        return self.code == ANTIBOT_CODE


@dataclass
class Card:
    """Un exemplaire possédé (user card) et les infos de sa carte."""

    copy_id: str
    card_id: str
    name: str
    rarity: str = ""
    tags: list[str] = field(default_factory=list)
    tag_ids: dict = field(default_factory=dict)  # norm(nom de l'étiquette) -> id
    initial_tags: frozenset = frozenset()  # étiquettes (norm) présentes sur le site au début du passage
    starred: bool = False
    is_shiny: bool = False
    in_trade: bool = False
    count: int = 1

    def has_any_tag(self, names, initial_only=False):
        mine = self.initial_tags if initial_only else {norm(t) for t in self.tags}
        return bool(set(mine) & {norm(n) for n in names})


def norm(text):
    """Forme canonique d'un nom d'étiquette : majuscules, accents, espaces et « # » initial ignorés."""
    text = unicodedata.normalize("NFKD", str(text))
    text = "".join(c for c in text if not unicodedata.combining(c)).casefold()
    return " ".join(text.split()).lstrip("#").strip()


def display_tag(name):
    """Nom d'étiquette tel qu'il sera créé sur le site : sans le « # » que le site affiche lui-même."""
    return " ".join(str(name).split()).lstrip("#").strip()


def label(card, value):
    shiny = " shiny" if card.is_shiny else ""
    shown = "?" if value is None or value is FAILED else f"{value:g}"
    return f"{card.name} [{card.rarity}{shiny}] valeur={shown}"


def read_yaml(path, missing_ok=False):
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        if missing_ok:
            return None
        sys.exit(f"Impossible de lire {path} (fichier introuvable).")
    except OSError as e:
        sys.exit(f"Impossible de lire {path} ({type(e).__name__}).")
    except yaml.YAMLError as e:
        where = getattr(e, "problem_mark", None)
        line = f" ligne {where.line + 1}" if where else ""
        sys.exit(f"{path} est mal écrit{line} (indentation, guillemets, deux-points ?).")


def merge(base, extra):
    """Réglages de perso.yaml par-dessus ceux de config.yaml : une section est complétée, une valeur remplacée."""
    out = dict(base)
    for key, value in extra.items():
        out[key] = merge(out[key], value) if isinstance(out.get(key), dict) and isinstance(value, dict) else value
    return out


def load_config(path):
    """config.yaml (réglages communs, sur GitHub) complété par perso.yaml (les vôtres, jamais sur GitHub)."""
    cfg = read_yaml(path)
    if not isinstance(cfg, dict):
        sys.exit(f"{path} est vide ou mal écrit : il doit contenir les sections site, protection, price…")
    telegram = cfg.get("telegram")
    if isinstance(telegram, dict) and {"bot_token", "chat_id"} & set(telegram):
        sys.exit(f"{path} contient telegram.bot_token ou telegram.chat_id : déplacez-les dans {PERSO_FILE} "
                 "(jamais envoyé sur GitHub), voir le README.")
    perso_path = config_path(path, PERSO_FILE)
    perso = read_yaml(perso_path, missing_ok=True)
    if perso is None:
        return cfg
    if not isinstance(perso, dict):
        sys.exit(f"{perso_path} est mal écrit : il doit contenir des sections comme dans config.yaml "
                 "(voir perso.exemple.yaml).")
    merged = merge(cfg, perso)
    # Les listes de protection de perso.yaml s'ajoutent à celles de config.yaml : oublier d'y recopier « garder »
    # ne doit jamais retirer une protection (une défausse est définitive).
    base, extra = cfg.get("protection"), perso.get("protection")
    if isinstance(base, dict) and isinstance(extra, dict):
        for key in ("tags", "rarities", "name_contains"):
            if isinstance(base.get(key), list) and isinstance(extra.get(key), list):
                have = {norm(x) for x in base[key]}
                merged["protection"][key] = base[key] + [x for x in extra[key] if norm(x) not in have]
    return merged


def config_path(cfg_file, value):
    """Chemin relatif au dossier du fichier de config (pas au dossier courant)."""
    path = pathlib.Path(value).expanduser()
    return path if path.is_absolute() else pathlib.Path(cfg_file).resolve().parent / path


# --- Validation de la configuration -----------------------------------------


def _is_number(x):
    return isinstance(x, Real) and not isinstance(x, bool)


def _is_int(x):
    return isinstance(x, int) and not isinstance(x, bool)


CONDITION_KEYS = {"name_contains", "rarity_in", "shiny", "min_value", "max_value"}


def check_config(cfg):
    """Refuse toute config ambiguë : une règle mal écrite ne doit jamais désactiver une protection."""
    errors = []
    err = errors.append

    def unknown_keys(sec, where, allowed, hint=""):
        # Une clé mal orthographiée serait ignorée sans bruit, et la protection qu'elle devait poser avec.
        for key in sorted(set(sec) - set(allowed), key=str):
            close = difflib.get_close_matches(str(key), allowed, n=1)
            err(f"{where}{'.' if where else ''}{key} : clé inconnue"
                + (f" (vouliez-vous dire « {close[0]} » ?)" if close else hint))

    def section(name, keys):
        value = cfg.get(name) if isinstance(cfg, dict) else None
        if not isinstance(value, dict):
            err(f"{name} : section manquante ou mal écrite")
            return {}
        unknown_keys(value, name, keys)
        return value

    def number(sec, path, key, minimum=None, integer=False, allow_none=False, strict=False):
        v = sec.get(key)
        if v is None and allow_none:
            return
        ok = _is_int(v) if integer else _is_number(v)
        if ok and minimum is not None:
            ok = v > minimum if strict else v >= minimum
        if not ok:
            kind = "un entier" if integer else "un nombre"
            bound = "" if minimum is None else f" {'>' if strict else '>='} {minimum}"
            err(f"{path}.{key} doit être {kind}{bound}{' (ou null)' if allow_none else ''}")

    def boolean(sec, path, key):
        if not isinstance(sec.get(key), bool):
            err(f"{path}.{key} doit valoir true ou false")

    def str_list(sec, path, key, allow_empty=True):
        v = sec.get(key)
        if not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v) or (not v and not allow_empty):
            err(f"{path}.{key} doit être une liste de textes{'' if allow_empty else ' non vide'}, ex. [\"a\", \"b\"]")
            return []
        return v

    def choice(sec, path, key, options):
        if sec.get(key) not in options:
            err(f"{path}.{key} doit valoir {' ou '.join(map(str, options))}")

    def conditions(cond, where):
        if not isinstance(cond, dict):
            err(f"{where} doit être un dictionnaire")
            return
        unknown_keys(cond, where, CONDITION_KEYS)
        if "rarity_in" in cond:
            str_list(cond, where, "rarity_in", allow_empty=False)
        if "name_contains" in cond and not isinstance(cond["name_contains"], str):
            err(f"{where}.name_contains doit être un texte")
        if "shiny" in cond and not isinstance(cond["shiny"], bool):
            err(f"{where}.shiny doit valoir true ou false")
        for key in ("min_value", "max_value"):
            if key in cond and not _is_number(cond[key]):
                err(f"{where}.{key} doit être un nombre")

    if isinstance(cfg, dict):
        unknown_keys(cfg, "", ("site", "protection", "price", "price_tags", "auto_tags", "discard", "sell", "packs",
                               "buy", "safety", "journal", "display", "telegram"))

    site_keys = ("base_url", "supabase_url", "supabase_anon_key")
    site = section("site", site_keys)
    for key in site_keys:
        if not isinstance(site.get(key), str) or not site.get(key):
            err(f"site.{key} doit être un texte")

    prot = section("protection", ("tags", "starred", "shiny", "rarities", "name_contains"))
    prot_tags = str_list(prot, "protection", "tags")
    boolean(prot, "protection", "starred")
    boolean(prot, "protection", "shiny")
    str_list(prot, "protection", "rarities")
    str_list(prot, "protection", "name_contains")

    price = section("price", ("cache_hours", "cache_file"))
    number(price, "price", "cache_hours", 0)
    if not isinstance(price.get("cache_file"), str) or not price.get("cache_file"):
        err("price.cache_file doit être un nom de fichier")

    pt = section("price_tags", ("bands", "unknown_tag", "remove_outdated", "tag_protected"))
    boolean(pt, "price_tags", "remove_outdated")
    boolean(pt, "price_tags", "tag_protected")
    if pt.get("unknown_tag") is not None and not isinstance(pt.get("unknown_tag"), str):
        err("price_tags.unknown_tag doit être un texte (ou null)")
    bands = pt.get("bands")
    if not isinstance(bands, list) or not bands:
        err("price_tags.bands doit être une liste de tranches de prix")
        bands = []
    previous_below = None
    for i, band in enumerate(bands):
        where = f"price_tags.bands[{i}]"
        if not isinstance(band, dict) or not isinstance(band.get("tag"), str) or not band["tag"].strip():
            err(f"{where}.tag doit être un texte non vide")
            continue
        unknown_keys(band, where, ("tag", "from", "below", "color"))
        if band.get("color") is not None and not isinstance(band.get("color"), str):
            err(f"{where}.color doit être un texte, ex. \"#22c55e\"")
        if any(_is_number(band.get(k)) and band[k] < 0 for k in ("from", "below")):
            err(f"{where} : les prix ne peuvent pas être négatifs")
        lo, hi = band.get("from"), band.get("below")
        for key, v in (("from", lo), ("below", hi)):
            if v is not None and not _is_number(v):
                err(f"{where}.{key} doit être un nombre")
        if _is_number(lo) and _is_number(hi) and lo >= hi:
            err(f"{where} : from ({lo}) doit être inférieur à below ({hi})")
        if i > 0 and lo is None:
            err(f"{where}.from manquant (seule la première tranche peut ne pas avoir de minimum)")
        if i < len(bands) - 1 and hi is None:
            err(f"{where}.below manquant (seule la dernière tranche peut ne pas avoir de maximum)")
        if previous_below is not None and _is_number(lo) and lo != previous_below:
            err(f"{where}.from doit valoir {previous_below} (fin de la tranche précédente) : ni trou ni chevauchement")
        previous_below = hi
    band_tags = [b["tag"] for b in bands if isinstance(b, dict) and isinstance(b.get("tag"), str)]
    if pt.get("unknown_tag"):
        band_tags.append(pt["unknown_tag"])
    if len({norm(t) for t in band_tags}) != len(band_tags):
        err("price_tags : chaque tranche (et unknown_tag) doit avoir une étiquette différente")

    auto_tags = cfg.get("auto_tags", [])
    if not isinstance(auto_tags, list):
        err("auto_tags doit être une liste (ou [])")
        auto_tags = []
    for i, rule in enumerate(auto_tags):
        where = f"auto_tags[{i}]"
        if not isinstance(rule, dict) or not isinstance(rule.get("tag"), str) or not rule["tag"].strip():
            err(f"{where}.tag doit être un texte non vide")
            continue
        unknown_keys(rule, where, ("tag", "when", "color"), " (les conditions vont sous « when: »)")
        if not isinstance(rule.get("when"), dict) or not rule.get("when"):
            err(f"{where}.when doit contenir au moins une condition (sinon l'étiquette irait sur toutes les cartes)")
        else:
            conditions(rule["when"], f"{where}.when")
        if norm(rule["tag"]) in {norm(t) for t in band_tags}:
            err(f"{where}.tag « {rule['tag']} » est déjà une étiquette de prix : elle serait retirée puis remise à chaque analyse")
        if rule.get("color") is not None and not isinstance(rule.get("color"), str):
            err(f"{where}.color doit être un texte, ex. \"#22c55e\"")

    disc = section("discard", ("tags", "max_value", "unknown_rarities", "max_per_run", "fresh_price",
                               "require_existing_tag"))
    disc_tags = str_list(disc, "discard", "tags", allow_empty=False)
    number(disc, "discard", "max_value", 0, allow_none=True)
    unknown_rarities = str_list(disc, "discard", "unknown_rarities")
    if unknown_rarities and not pt.get("unknown_tag"):
        err("discard.unknown_rarities demande price_tags.unknown_tag (ex. \"inconnu\") : c'est cette étiquette "
            "qui repère les cartes jamais vendues")
    protected_rarities = {norm(r) for r in prot.get("rarities") or [] if isinstance(r, str)}
    for rarity in unknown_rarities:
        if norm(rarity) in protected_rarities:
            err(f"discard.unknown_rarities contient {rarity}, qui est protégée (protection.rarities) : "
                "retirez-la de l'une des deux listes")
    number(disc, "discard", "max_per_run", 0, integer=True)
    boolean(disc, "discard", "fresh_price")
    boolean(disc, "discard", "require_existing_tag")

    sell = section("sell", ("tags", "order", "price_factor", "rounding", "min_start_price", "max_start_price",
                            "duration_minutes", "max_auctions", "min_value", "fresh_price", "require_existing_tag",
                            "relist"))
    sell_tags = str_list(sell, "sell", "tags", allow_empty=False)
    number(sell, "sell", "price_factor", 0, strict=True)
    choice(sell, "sell", "rounding", ["floor", "round", "ceil"])
    number(sell, "sell", "min_start_price", 1, integer=True)
    number(sell, "sell", "max_start_price", 1, integer=True, allow_none=True)
    if _is_int(sell.get("min_start_price")) and _is_int(sell.get("max_start_price")) \
            and sell["max_start_price"] < sell["min_start_price"]:
        err("sell.max_start_price doit être >= sell.min_start_price")
    boolean(sell, "sell", "fresh_price")
    boolean(sell, "sell", "require_existing_tag")
    choice(sell, "sell", "duration_minutes", sorted(KNOWN_DURATIONS))
    number(sell, "sell", "max_auctions", 0, integer=True)
    if _is_int(sell.get("max_auctions")) and sell["max_auctions"] > SITE_MAX_AUCTIONS:
        err(f"sell.max_auctions ne peut pas dépasser {SITE_MAX_AUCTIONS} (limite du site)")
    number(sell, "sell", "min_value", 0, allow_none=True)
    choice(sell, "sell", "order", ["random", "value", "tags"])
    relist = sell.get("relist")
    if not isinstance(relist, dict):
        err("sell.relist : section manquante ou mal écrite")
    else:
        unknown_keys(relist, "sell.relist", ("enabled", "factor", "min_start_price", "max_attempts", "first"))
        boolean(relist, "sell.relist", "enabled")
        boolean(relist, "sell.relist", "first")
        number(relist, "sell.relist", "factor", 0, strict=True)
        if _is_number(relist.get("factor")) and relist["factor"] > 1:
            err("sell.relist.factor doit être <= 1 (une relance ne se fait pas plus cher)")
        number(relist, "sell.relist", "min_start_price", 1, integer=True)
        number(relist, "sell.relist", "max_attempts", 1, integer=True)

    packs = section("packs", ("in_tout", "max_per_run", "delay_seconds"))
    boolean(packs, "packs", "in_tout")
    number(packs, "packs", "max_per_run", 0, integer=True)
    number(packs, "packs", "delay_seconds", 0)

    buy = section("buy", ("keywords", "tag", "price_factor", "max_price", "buy_unknown", "skip_owned",
                          "snipe_seconds", "scan_minutes"))
    keywords = str_list(buy, "buy", "keywords")
    number(buy, "buy", "price_factor", 0, strict=True, allow_none=True)
    number(buy, "buy", "max_price", 1, integer=True, allow_none=True)
    boolean(buy, "buy", "buy_unknown")
    boolean(buy, "buy", "skip_owned")
    number(buy, "buy", "snipe_seconds", 10)
    number(buy, "buy", "scan_minutes", 1)
    if buy.get("tag") is not None and (not isinstance(buy["tag"], str) or not buy["tag"].strip()):
        err("buy.tag doit être un texte (ou null)")
    elif keywords and norm(buy.get("tag") or "") not in {norm(t) for t in prot_tags}:
        # Sans elle, « analyser » mettrait en vente ou défausserait les cartes que le script vient d'acheter.
        err("buy.tag doit être une étiquette de protection.tags (ex. « garder ») : elle protège les cartes achetées")

    journal = section("journal", ("enabled", "file", "delimiter"))
    boolean(journal, "journal", "enabled")
    if not isinstance(journal.get("file"), str) or not journal.get("file"):
        err("journal.file doit être un nom de fichier")
    if journal.get("delimiter") not in (";", ",", "\t"):
        err("journal.delimiter doit valoir \";\" (Excel français), \",\" ou une tabulation")

    # Les garde-fous de prix doivent être cohérents avec les tranches, sinon le script tournerait en rond.
    for band in bands if isinstance(bands, list) else []:
        if not isinstance(band, dict) or not isinstance(band.get("tag"), str):
            continue
        if norm(band["tag"]) in {norm(t) for t in disc_tags} and _is_number(disc.get("max_value")) \
                and (band.get("below") is None or band["below"] > disc["max_value"]):
            err(f"la tranche « {band['tag']} » (à défausser) dépasse discard.max_value ({disc['max_value']}) : "
                "ses cartes seraient étiquetées mais jamais défaussées")
        if norm(band["tag"]) in {norm(t) for t in sell_tags} and _is_number(sell.get("min_value")) \
                and (band.get("from") or 0) < sell["min_value"]:
            err(f"la tranche « {band['tag']} » (à vendre) commence sous sell.min_value ({sell['min_value']}) : "
                "ses cartes seraient étiquetées mais jamais vendues")
    for i, rule in enumerate(auto_tags):
        if isinstance(rule, dict) and isinstance(rule.get("tag"), str) and norm(rule["tag"]) in {norm(t) for t in disc_tags}:
            err(f"auto_tags[{i}] : une étiquette de défausse ne peut pas être posée par auto_tags "
                "(utilisez les tranches de price_tags)")
    if pt.get("unknown_tag") and norm(pt["unknown_tag"]) in {norm(t) for t in disc_tags}:
        err("price_tags.unknown_tag ne peut pas être une étiquette de défausse (cartes au prix inconnu)")

    # Une même étiquette ne peut pas vouloir dire deux choses contradictoires.
    for (name_a, a), (name_b, b) in (
        (("protection.tags", prot_tags), ("discard.tags", disc_tags)),
        (("protection.tags", prot_tags), ("sell.tags", sell_tags)),
        (("discard.tags", disc_tags), ("sell.tags", sell_tags)),
    ):
        common = {norm(t) for t in a} & {norm(t) for t in b}
        if common:
            err(f"{name_a} et {name_b} ont des étiquettes en commun : {sorted(common)}")

    safety = section("safety", ("max_actions_per_run", "delay_seconds", "auction_delay_seconds", "read_delay_seconds",
                                "max_consecutive_errors", "max_consecutive_read_failures"))
    number(safety, "safety", "max_actions_per_run", 0, integer=True)
    number(safety, "safety", "delay_seconds", 0)
    number(safety, "safety", "auction_delay_seconds", 0)
    number(safety, "safety", "read_delay_seconds", 0)
    number(safety, "safety", "max_consecutive_errors", 1, integer=True)
    number(safety, "safety", "max_consecutive_read_failures", 1, integer=True)

    display = cfg.get("display", {})
    if isinstance(display, dict):
        unknown_keys(display, "display", ("verbose", "waiting_shown"))
    if not isinstance(display, dict) or not isinstance(display.get("verbose", False), bool):
        err("display.verbose doit valoir true ou false")
    elif not _is_int(display.get("waiting_shown", 5)) or display.get("waiting_shown", 5) < 0:
        err("display.waiting_shown doit être un entier >= 0")

    telegram = cfg.get("telegram", {})
    if not isinstance(telegram, dict):
        err("telegram : section mal écrite")
    else:
        unknown_keys(telegram, "telegram", ("notify_on_dry_run", "bot_token", "chat_id"))
        if "notify_on_dry_run" in telegram:
            boolean(telegram, "telegram", "notify_on_dry_run")
        if telegram.get("bot_token") is not None and not isinstance(telegram["bot_token"], str):
            err("telegram.bot_token doit être un texte entre guillemets")
        if telegram.get("chat_id") is not None and not isinstance(telegram["chat_id"], (str, int)):
            err("telegram.chat_id doit être un nombre ou un texte")

    if errors:
        sys.exit("Configuration invalide :\n  - " + "\n  - ".join(errors))


# --- Session ----------------------------------------------------------------


def parse_cookie_header(header):
    cookies = {}
    for part in header.split(";"):
        name, sep, value = part.strip().partition("=")
        if sep:
            cookies[name.strip()] = value.strip()
    return cookies


def normalize_cookie_header(raw):
    """Accepte un collage avec préfixe « Cookie: », retours à la ligne ou espaces parasites."""
    raw = raw.replace("\r", "").replace("\n", "").strip()
    if raw.lower().startswith("cookie:"):
        raw = raw[len("cookie:"):].strip()
    return "; ".join(f"{k}={v}" for k, v in parse_cookie_header(raw).items())


def supabase_session(cookies, project_ref):
    """Décode la session Supabase du cookie sb-<ref>-auth-token (éventuellement découpé en .0, .1…).

    Lève ValueError avec un message clair si le cookie est absent ou illisible.
    """
    base = f"sb-{project_ref}-auth-token"
    if cookies.get(base):
        raw = cookies[base]
    else:
        chunks = {}
        for name, value in cookies.items():
            suffix = name[len(base) + 1:]
            if name.startswith(base + ".") and suffix.isdigit() and value:
                chunks[int(suffix)] = value
        if not chunks:
            raise ValueError(f"cookie {base} introuvable")
        if sorted(chunks) != list(range(len(chunks))):
            raise ValueError(f"cookie {base} incomplet (morceaux présents : {sorted(chunks)})")
        raw = "".join(chunks[i] for i in range(len(chunks)))
    raw = unquote(raw).strip('"')
    try:
        if raw.startswith("base64-"):
            payload = raw[len("base64-"):]
            raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode()
        session = json.loads(raw)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError(f"cookie {base} illisible (copie tronquée ?)") from None
    if not isinstance(session, dict) or not isinstance(session.get("access_token"), str):
        raise ValueError(f"cookie {base} sans jeton d'accès")
    if not isinstance(session.get("refresh_token"), str):
        raise ValueError(f"cookie {base} sans jeton de renouvellement")
    return session


def session_cookies(session, project_ref):
    """Encode la session comme le fait le site : "base64-" + base64url(JSON), découpé si trop long."""
    base = f"sb-{project_ref}-auth-token"
    value = "base64-" + base64.urlsafe_b64encode(json.dumps(session).encode()).decode().rstrip("=")
    if len(value) <= COOKIE_CHUNK_SIZE:
        return {base: value}
    chunks = [value[i:i + COOKIE_CHUNK_SIZE] for i in range(0, len(value), COOKIE_CHUNK_SIZE)]
    return {f"{base}.{i}": chunk for i, chunk in enumerate(chunks)}


def jwt_claims(token):
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (AttributeError, IndexError, binascii.Error, UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("jeton d'accès illisible") from None
    if not isinstance(claims, dict) or not isinstance(claims.get("sub"), str):
        raise ValueError("jeton d'accès sans identifiant de compte")
    return claims


class SessionSaveError(Exception):
    pass


class SessionStore:
    """Fichier local (session.json) qui contient la session du script. À ne jamais partager."""

    def __init__(self, path):
        self.path = pathlib.Path(path)

    def load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError):
            sys.exit(f"{self.path} est illisible : relancez « {cmd('login')} ».")
        return data.get("session") if isinstance(data, dict) else None

    def save(self, session):
        """Écriture atomique, droits 600 dès la création, données forcées sur le disque.

        Un jeton de renouvellement perdu oblige à refaire login : on ne laisse aucune demi-écriture.
        """
        try:
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".session.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump({"session": session}, f)
                    f.flush()
                    os.fsync(f.fileno())
                for attempt in range(5):
                    try:
                        os.replace(tmp, self.path)
                        break
                    except PermissionError:  # Windows : fichier verrouillé un instant (antivirus, OneDrive…)
                        if attempt == 4:
                            raise
                        time.sleep(0.3)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        except OSError as e:
            raise SessionSaveError(f"impossible d'écrire {self.path} ({type(e).__name__})") from None


def clean_secret(text):
    """Retire les séquences de collage du terminal et les caractères de contrôle (un cookie est en ASCII)."""
    out = []
    for ch in re.sub(r"\x1b\[20[01]~", "", text):
        if ch in "\x7f\x08":
            if out:
                out.pop()
        elif ch in "\r\n" or ch >= " ":
            out.append(ch)
    return "".join(out)


def read_secret(prompt):
    """Lit un collage sans l'afficher, même très long et même sur plusieurs lignes.

    getpass/input passent par le mode canonique du terminal, limité à 1024 caractères sur macOS :
    un cookie de ~5000 caractères y serait tronqué. On lit donc en mode non canonique, jusqu'à ce que
    le collage soit terminé, puis on vide l'entrée pour qu'aucun morceau ne parte vers le shell.
    """
    if not sys.stdin.isatty():
        sys.stderr.write(prompt)
        sys.stderr.flush()
        return clean_secret(sys.stdin.readline())
    try:
        import select
        import termios
    except ImportError:  # Windows : getpass lit caractère par caractère, sans limite.
        import getpass

        return clean_secret(getpass.getpass(prompt))
    print(prompt, end="", flush=True)
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    new = termios.tcgetattr(fd)
    new[3] &= ~(termios.ICANON | termios.ECHO)
    new[6][termios.VMIN], new[6][termios.VTIME] = 1, 0
    data = ""
    try:
        termios.tcsetattr(fd, termios.TCSANOW, new)
        while True:
            chunk = os.read(fd, 65536).decode(errors="ignore")
            if not chunk or (chunk == "\x04" and not data):
                break
            data += chunk
            if "\n" in data or "\r" in data:
                # Entrée reçue : on laisse 0,3 s au reste d'un collage multi-lignes pour arriver.
                while select.select([fd], [], [], 0.3)[0]:
                    more = os.read(fd, 65536).decode(errors="ignore")
                    if not more:
                        break
                    data += more
                break
    finally:
        termios.tcflush(fd, termios.TCIFLUSH)
        termios.tcsetattr(fd, termios.TCSAFLUSH, old)
        print()
    return clean_secret(data)


# --- Lecture des données ----------------------------------------------------


def parse_collection_page(data, pending=None):
    """Convertit une page de /api/my-collection. pending : ids en échange (par défaut ceux de la page)."""
    if pending is None:
        pending = {str(i) for i in data.get("pendingTradeCardIds") or []}
    copies = []
    for item in data.get("collection", []):
        card = item.get("card") or {}
        copy_id, card_id = str(item["id"]), str(item.get("card_id") or card.get("id"))
        tags = item.get("tags") or []
        copies.append(
            Card(
                copy_id=copy_id,
                card_id=card_id,
                name=card.get("wikipedia_title", ""),
                rarity=card.get("rarity", ""),
                tags=[t["name"] if isinstance(t, dict) else str(t) for t in tags],
                tag_ids={norm(t["name"]): t["id"] for t in tags if isinstance(t, dict) and "id" in t},
                initial_tags=frozenset(norm(t["name"] if isinstance(t, dict) else t) for t in tags),
                starred=bool(item.get("starred")),
                is_shiny=bool(item.get("is_shiny")),
                # On ne sait pas si ces ids sont ceux des exemplaires ou des cartes : on teste les deux.
                in_trade=copy_id in pending or card_id in pending,
                count=int(item.get("count") or 1),
            )
        )
    return copies


def value_from_sales(data, card):
    """Prix moyen des ventes passées pour la rareté de la carte, ou None si aucune vente."""
    entry = (data or {}).get("summary", {}).get(card.rarity) if isinstance(data, dict) else None
    average = entry.get("average") if isinstance(entry, dict) else None
    if _is_number(average) and math.isfinite(average) and average >= 0:
        return float(average)
    return None


class PriceCache:
    """Prix déjà lus, gardés quelques heures pour qu'une relance ne relise pas toute la collection."""

    MISSING = object()

    def __init__(self, path, hours):
        self.path, self.ttl = pathlib.Path(path), hours * 3600
        self.not_before = 0  # --fresh : les prix lus avant ce moment sont ignorés (relus sur le site)
        self.data = {}
        if self.ttl > 0:
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                self.data = {}

    def _key(self, card):
        return f"{card.card_id}:{card.rarity}"

    def _alive(self, entry):
        return isinstance(entry, dict) and _is_number(entry.get("at")) and time.time() - entry["at"] <= self.ttl

    def get(self, card, since=0):
        """Prix gardé, ou MISSING. since : ignorer les prix lus avant ce moment (relus sur le site)."""
        entry = self.data.get(self._key(card))
        if self.ttl <= 0 or not self._alive(entry) or entry["at"] < max(since, self.not_before):
            return self.MISSING
        return entry.get("value")

    def set(self, card, value):
        if self.ttl > 0:
            self.data[self._key(card)] = {"value": value, "at": time.time()}

    def save(self):
        """Fusionne avec le fichier (un autre compte a pu l'enrichir entre-temps) et oublie les prix périmés."""
        if self.ttl <= 0:
            return
        try:
            on_disk = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            on_disk = {}
        merged = {}
        for source in (on_disk if isinstance(on_disk, dict) else {}, self.data):
            for key, entry in source.items():
                if self._alive(entry) and entry["at"] >= merged.get(key, {}).get("at", 0):
                    merged[key] = entry
        self.data = merged
        try:
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".prix.", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(merged, f)
            os.replace(tmp, self.path)
        except OSError as e:  # le cache n'est qu'une optimisation : jamais bloquant
            print(f"Attention : cache des prix non enregistré ({type(e).__name__}).")


# --- Règles -----------------------------------------------------------------


def matches(card, value, cond):
    if "name_contains" in cond and norm(cond["name_contains"]) not in norm(card.name):
        return False
    if "rarity_in" in cond and norm(card.rarity) not in {norm(r) for r in cond["rarity_in"]}:
        return False
    if "shiny" in cond and card.is_shiny != cond["shiny"]:
        return False
    if "min_value" in cond and (value is None or value < cond["min_value"]):
        return False
    if "max_value" in cond and (value is None or value > cond["max_value"]):
        return False
    return True


def protection_reason(card, prot):
    """Pourquoi cet exemplaire ne doit jamais être défaussé ni vendu (None s'il n'est pas protégé)."""
    if card.in_trade:
        return "échange en cours"
    tags = sorted({norm(t) for t in card.tags} & {norm(t) for t in prot["tags"]})
    if tags:
        return f"étiquette {tags[0]}"
    if card.starred and prot["starred"]:
        return "favori"
    if card.is_shiny and prot["shiny"]:
        return "shiny"
    if norm(card.rarity) in {norm(r) for r in prot["rarities"]}:
        return f"rareté {card.rarity}"
    for part in prot["name_contains"]:
        if norm(part) in norm(card.name):
            return f"nom contient « {part} »"
    return None


def band_for(value, bands):
    for band in bands:
        lo, hi = band.get("from"), band.get("below")
        if (lo is None or value >= lo) and (hi is None or value < hi):
            return band
    return None


def tag_changes(card, value, cfg):
    """Étiquettes à ajouter et à retirer pour la commande analyser : (ajouts, retraits)."""
    pt = cfg["price_tags"]
    price_tag_names = [b["tag"] for b in pt["bands"]] + ([pt["unknown_tag"]] if pt.get("unknown_tag") else [])
    if value is None:
        target = pt.get("unknown_tag") or None
    else:
        band = band_for(value, pt["bands"])
        target = band["tag"] if band else None
    have = {norm(t): t for t in card.tags}
    add, remove = [], []
    if target and norm(target) not in have:
        add.append(target)
    # Les anciennes étiquettes de prix ne sont retirées que si la nouvelle tranche est connue.
    if pt["remove_outdated"] and target:
        remove = [have[norm(n)] for n in price_tag_names if norm(n) in have and norm(n) != norm(target)]
    auto = []
    for rule in cfg.get("auto_tags", []):
        if norm(rule["tag"]) not in have and norm(rule["tag"]) not in {norm(t) for t in add + auto} \
                and matches(card, value, rule.get("when", {})):
            auto.append(rule["tag"])
    protecting = {norm(t) for t in cfg["protection"]["tags"]}
    if any(norm(t) in protecting for t in auto):
        # La carte devient protégée : pas d'étiquette de vente ni de défausse, et la protection est posée en premier.
        return [t for t in auto if norm(t) in protecting] + [t for t in auto if norm(t) not in protecting], []
    return add + auto, remove


ROUNDING = {
    "floor": math.floor,
    "round": lambda x: math.floor(x + 0.5),  # au plus proche, 2,5 → 3 (et non l'arrondi « bancaire » de Python)
    "ceil": math.ceil,
}


def start_price(value, sell):
    """Prix de départ = valeur × facteur, arrondi selon la config, borné par min/max."""
    raw = round(value * sell["price_factor"], 6)  # évite 28,999999… → 28 avec floor
    price = ROUNDING[sell["rounding"]](raw)
    price = max(int(price), sell["min_start_price"])
    if sell.get("max_start_price"):
        price = min(price, sell["max_start_price"])
    return price


def parse_time(text):
    """Horodatage du site (ex. 2026-10-10T08:04:51.55455+00:00) en secondes depuis 1970, ou None s'il est illisible."""
    m = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:?\d\d)?", str(text or "").strip())
    if not m:
        return None
    moment, fraction, zone = m.groups()
    zone = "+00:00" if zone in (None, "Z") else zone[:3] + ":" + zone[-2:]
    try:
        stamp = datetime.datetime.fromisoformat(moment + zone).timestamp()
    except ValueError:
        return None
    return stamp + (float("0." + fraction) if fraction else 0.0)


def next_bid(auction):
    """Mise minimale acceptée : la mise de départ s'il n'y a pas encore de mise, sinon la mise actuelle + 10 %
    (arrondi à l'inférieur) + 1, comme le propose le site (relevé dans un HAR : 5 → 6, 27 → 30, 100 → 111)."""
    current = auction.get("current_bid")
    if current is None:
        start = auction.get("effective_bid", auction.get("base_amount"))
        return max(1, math.ceil(start)) if _is_number(start) else None
    if not _is_number(current):
        return None
    current = int(current)
    return current + current // 10 + 1


def buy_ceiling(value, buy):
    """Mise maximale pour une carte, ou None sans limite : prix moyen × buy.price_factor (arrondi à l'inférieur, si
    price_factor n'est pas null et le prix connu), et buy.max_price."""
    limits = [buy["max_price"]] if buy.get("max_price") else []
    if buy.get("price_factor") is not None and value is not None:
        limits.append(math.floor(round(value * buy["price_factor"], 6)))
    return min(limits) if limits else None


def show_ceiling(ceiling):
    return "aucun" if ceiling is None else ceiling


# --- Client HTTP ------------------------------------------------------------


def _json_code(resp):
    """Code d'erreur PostgreSQL renvoyé par Supabase (ex. "42501"), ou None."""
    try:
        body = resp.json()
    except ValueError:
        return None
    return body.get("code") if isinstance(body, dict) else None


class WikiMastersClient:
    def __init__(self, site_cfg, store, session=None, page_delay=0):
        self.base_url = site_cfg["base_url"].rstrip("/")
        self.supabase_url = site_cfg["supabase_url"].rstrip("/")
        self.anon_key = site_cfg["supabase_anon_key"]
        self.ref = urlparse(self.supabase_url).netloc.split(".", 1)[0]
        self.store = store
        self.page_delay = page_delay
        self.http = requests.Session()
        self.http.headers["User-Agent"] = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
        self.http.headers["Accept"] = "application/json"
        self.session_lost = None  # raison si la session ne peut plus être utilisée
        self.slowdown = 1.0  # multiplie toutes les pauses après une erreur du serveur
        self.waiter = None  # remplace time.sleep pendant les pauses : les achats misent pendant ce temps (Sniper)
        self.last_uncertain = False  # la dernière écriture a dû être renvoyée après une tentative peut-être effectuée
        self._tag_ids = None
        self._last_refresh = None

        session = session or store.load()
        if not session:
            sys.exit(f"Aucune session enregistrée : lancez d'abord « {cmd('login')} » (voir README).")
        try:
            self._use_session(session, save=False)
        except (ValueError, KeyError, TypeError):
            sys.exit(f"{store.path} est invalide : relancez « {cmd('login')} ».")

    def _use_session(self, session, save=True, fresh=False):
        """Adopte une session. Elle est d'abord écrite sur le disque, puis seulement utilisée."""
        if not isinstance(session, dict) or not isinstance(session.get("refresh_token"), str):
            raise ValueError("session sans jeton de renouvellement")
        claims = jwt_claims(session["access_token"])
        deadlines = [float(claims["exp"])] if _is_number(claims.get("exp")) else []
        if _is_number(session.get("expires_at")):
            deadlines.append(float(session["expires_at"]))
        if fresh and _is_number(session.get("expires_in")):
            # Durée relative : insensible à un décalage d'horloge entre ce PC et le serveur.
            deadlines.append(time.time() + session["expires_in"])
        if not deadlines:
            raise ValueError("session sans date d'expiration")
        if save:
            self.store.save(session)  # SessionSaveError remonte : rien n'est adopté si l'écriture échoue
        self.session = session
        self.access_token = session["access_token"]
        self.user_id = claims["sub"]
        self.username = account_name(claims)
        self.expires_at = min(deadlines)
        self.cookies = session_cookies(session, self.ref)

    def time_left(self):
        return self.expires_at - time.time()

    def refresh(self):
        """Renouvelle la session comme le fait le navigateur (jeton de renouvellement Supabase)."""
        url = f"{self.supabase_url}/auth/v1/token"
        self._last_refresh = time.time()
        for attempt in range(len(REFRESH_RETRY_DELAYS) + 1):
            if attempt:
                time.sleep(REFRESH_RETRY_DELAYS[attempt - 1])
            try:
                resp = self.http.request(
                    "POST",
                    url,
                    params={"grant_type": "refresh_token"},
                    json={"refresh_token": self.session["refresh_token"]},
                    headers={"apikey": self.anon_key},
                    timeout=20,
                    allow_redirects=False,
                )
            except requests.RequestException as e:
                problem = f"réseau : {type(e).__name__}"
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                problem = f"erreur {resp.status_code}"
                continue
            break
        else:
            # Coupure passagère : tant que le jeton actuel est valable, on continue et on réessaie dans une minute.
            if self.time_left() > 30:
                print(f"  Attention : renouvellement de session reporté ({problem}), nouvel essai dans 1 min.")
                return
            raise ApiError(f"renouvellement de session impossible ({problem}) : vérifiez la connexion internet "
                           "(et la mise en veille), puis relancez.", fatal=True)
        if resp.status_code in (400, 401, 403):
            # Définitif : --loop s'arrête au lieu de réessayer à chaque cycle.
            self.session_lost = ("session révoquée ou expirée (déconnexion, ou session partagée avec un navigateur) : "
                                 f"relancez « {cmd('login')} ».")
            raise ApiError(self.session_lost, fatal=True)
        if not resp.ok:
            raise ApiError(f"renouvellement de session impossible (erreur {resp.status_code})", fatal=True)
        try:
            self._use_session(resp.json(), fresh=True)
        except SessionSaveError as e:
            raise ApiError(f"{e} : la nouvelle session est perdue, relancez « {cmd('login')} ».",
                           fatal=True) from None
        except (ValueError, KeyError, TypeError):
            raise ApiError("réponse de renouvellement de session inattendue", fatal=True) from None

    def ensure_fresh(self):
        if self.session_lost:
            raise ApiError(self.session_lost, fatal=True)
        recently = self._last_refresh is not None and time.time() - self._last_refresh < 60
        if self.time_left() < REFRESH_MARGIN and not recently:
            self.refresh()

    def _adopt_server_cookies(self, resp):
        """Si le site renouvelle lui-même la session (Set-Cookie), on reprend la nouvelle."""
        prefix = f"sb-{self.ref}-auth-token"
        updates = {c.name: c.value for c in resp.cookies if c.name == prefix or c.name.startswith(prefix + ".")}
        if not updates:
            return
        # Le site renvoie toujours la session complète : elle remplace entièrement l'ancienne.
        # (Les suppressions de morceaux en trop, Max-Age=0, n'arrivent pas jusqu'à resp.cookies.)
        merged = {k: v for k, v in updates.items() if v}
        try:
            session = supabase_session(merged, self.ref)
        except ValueError:
            self.session_lost = f"le site a renouvelé la session de façon illisible : relancez « {cmd('login')} »."
            return
        try:
            self._use_session(session)
        except SessionSaveError as e:
            self.session_lost = f"{e} : la nouvelle session est perdue, relancez « {cmd('login')} »."
        except (ValueError, KeyError, TypeError):
            self.session_lost = f"le site a renvoyé une session invalide : relancez « {cmd('login')} »."

    def pause(self, seconds):
        """Pause entre deux requêtes, allongée tant que le serveur montre des signes de surcharge."""
        (self.waiter or time.sleep)(seconds * self.slowdown)

    def _request(self, method, url, headers_fn, retry=None, **kwargs):
        """Envoie la requête ; en cas de panne du serveur (429, 5xx, réseau), attend puis la renvoie.

        Une lecture est toujours renvoyée. Une écriture ne l'est que si retry le permet :
          "idempotent" : la refaire ne change rien (étiquette déjà posée ou déjà retirée) ;
          "safe"       : seulement si le serveur n'a certainement rien fait (NOT_PROCESSED, connexion impossible).
        """
        what = f"{method} {urlparse(url).path}"
        if method == "GET":
            retry, delays = "idempotent", GET_RETRY_DELAYS
        else:
            delays = WRITE_RETRY_DELAYS if retry else ()
        uncertain = False  # une tentative d'écriture a peut-être été effectuée par le serveur
        for attempt in range(len(delays) + 1):
            self.ensure_fresh()
            wait = delays[attempt] if attempt < len(delays) else 0
            try:
                resp = self.http.request(
                    method, url, timeout=20, allow_redirects=False, headers=headers_fn(), **kwargs
                )
            except requests.RequestException as e:
                sent = not isinstance(e, requests.ConnectTimeout)  # délai de connexion dépassé : rien n'est parti
                uncertain = uncertain or (sent and method != "GET")
                # Jamais str(e) : le message peut contenir les en-têtes, donc le cookie.
                problem = f"erreur réseau ({type(e).__name__})"
                error = ApiError(f"{problem} sur {what}.{self._maybe_done(uncertain)}", fatal=True)
                retryable = retry == "idempotent" or not sent
            else:
                self._adopt_server_cookies(resp)
                if not (resp.status_code == 429 or resp.status_code >= 500):
                    self.slowdown = max(1.0, self.slowdown * 0.9)
                    self.last_uncertain = uncertain
                    return self._parse(resp, what, self._maybe_done(uncertain))
                problem = f"erreur {resp.status_code}"
                processed = resp.status_code not in NOT_PROCESSED
                uncertain = uncertain or (processed and method != "GET")
                error = self._status_error(resp, what, self._maybe_done(uncertain))
                retryable = retry == "idempotent" or not processed
                wait = max(wait, min(self._retry_after(resp), 300))
            self.slowdown = min(MAX_SLOWDOWN, self.slowdown * 2)
            if not retryable or attempt == len(delays):
                break
            print(f"     {problem} sur {what} : nouvel essai dans {wait:g} s…")
            time.sleep(wait)
        raise error

    @staticmethod
    def _maybe_done(uncertain):
        return f" {MAYBE_DONE}" if uncertain else ""

    @staticmethod
    def _retry_after(resp):
        """Délai demandé par le serveur (en-tête Retry-After, en secondes), 0 sinon."""
        try:
            return max(0.0, float(resp.headers.get("Retry-After") or 0))
        except ValueError:
            return 0.0

    def _status_error(self, resp, what, maybe_done):
        is_json = resp.headers.get("content-type", "").startswith("application/json")
        detail = f" : {resp.text[:200]}" if is_json and resp.text else ""
        if resp.status_code == 429:
            setting = "safety.read_delay_seconds" if what.startswith("GET") else "safety.delay_seconds"
            return ApiError(f"trop de requêtes (429) : augmentez {setting} et réessayez plus tard.{maybe_done}",
                            True, 429)
        return ApiError(f"erreur {resp.status_code} sur {what}{detail}{maybe_done}", status=resp.status_code,
                        code=_json_code(resp) if is_json else None)

    def _parse(self, resp, what, maybe_done):
        is_json = resp.headers.get("content-type", "").startswith("application/json")
        detail = f" : {resp.text[:200]}" if is_json and resp.text else ""
        code = _json_code(resp) if is_json else None
        if 300 <= resp.status_code < 400:
            raise ApiError(f"redirection {resp.status_code} sur {what} (session refusée ?)", fatal=True)
        if resp.status_code == 403 and code == ANTIBOT_CODE:
            # Le site veut qu'un humain passe une vérification : surtout ne pas insister. Une lecture refusée arrête
            # le passage ; une action refusée met ce type d'action en pause (voir Runner).
            hint = f" {ANTIBOT_HINT}" if what.startswith("GET") else ""
            raise ApiError(f"le site demande une vérification anti-robot (« Vérification anti-bot requise ») sur "
                           f"{what}.{hint}", fatal=what.startswith("GET"), status=403, code=code)
        if resp.status_code == 403 and code == "42501":
            # Session valide mais règle d'accès Supabase refusée : en pratique, l'exemplaire a quitté la collection
            # pendant le passage. Seule cette carte est abandonnée ; si tout est refusé, max_consecutive_errors arrête.
            raise ApiError(
                f"refusé par le site sur {what} (règle d'accès, code 42501) : la carte n'est probablement plus "
                "dans votre collection (vendue, échangée ou défaussée depuis le début du passage).",
                status=resp.status_code,
            )
        if resp.status_code in (401, 403):
            raise ApiError(
                f"accès refusé ({resp.status_code}) sur {what}{detail}. "
                f"Session refusée ou blocage Cloudflare : relancez « {cmd('login')} ».",
                fatal=True,
                status=resp.status_code,
            )
        if not resp.ok:
            raise ApiError(f"erreur {resp.status_code} sur {what}{detail}", status=resp.status_code, code=code)
        if not resp.content:
            return None
        if not is_json:
            raise ApiError(f"réponse non JSON sur {what} (page Cloudflare ou de connexion ?).{maybe_done}", fatal=True)
        return resp.json()

    def _api(self, method, path, **kwargs):
        def headers():
            cookie = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
            return {"Cookie": cookie, "Origin": self.base_url}

        return self._request(method, self.base_url + path, headers, **kwargs)

    def _rest(self, method, path, extra_headers=None, **kwargs):
        def headers():
            h = {"apikey": self.anon_key, "Authorization": f"Bearer {self.access_token}"}
            h.update(extra_headers or {})
            return h

        return self._request(method, f"{self.supabase_url}/rest/v1/{path}", headers, **kwargs)

    def list_cards(self):
        """Toute la collection. Retourne (exemplaires, avertissement ou None)."""
        pages, seen, warning = [], set(), None
        for page in range(MAX_PAGES):
            data = self._api("GET", "/api/my-collection", params={"sort": "rarity", "page": page, "stats": 0})
            items = data.get("collection", [])
            new = []
            for item in items:
                copy_id = str(item["id"])
                if copy_id not in seen:
                    seen.add(copy_id)
                    new.append(item)
            if items and not new:
                warning = f"la pagination n'avance pas : seuls les {len(seen)} premiers exemplaires sont traités."
                break
            pages.append(dict(data, collection=new))
            if len(items) < PAGE_SIZE:
                break
            self.pause(self.page_delay)
        else:
            warning = f"plus de {MAX_PAGES} pages : seuls les {len(seen)} premiers exemplaires sont traités."

        # Les échanges en cours peuvent n'être listés que sur certaines pages : on réunit tout.
        pending = {str(i) for p in pages for i in p.get("pendingTradeCardIds") or []}
        cards = [c for p in pages for c in parse_collection_page(p, pending)]
        return cards, warning

    def card_value(self, card):
        try:
            data = self._api("GET", f"/api/marketplace/cards/{card.card_id}/sales", params={"scope": "summary"})
        except ApiError as e:
            if e.status == 404:  # « Carte introuvable » : traitée comme jamais vendue.
                return None
            raise
        return value_from_sales(data, card)

    def auction_slots(self):
        data = self._api("GET", "/api/marketplace/mine")
        return data["maxConcurrentAuctions"] - data["sellingCount"]

    def discard(self, card):
        return self._api("POST", f"/api/user-cards/{card.copy_id}/discard", retry="safe")

    def get_auction(self, auction_id):
        return self._api("GET", f"/api/marketplace/{auction_id}")

    def create_auction(self, card, base_amount, duration_minutes):
        # Le champ s'appelle "card_id" mais attend bien l'id de l'EXEMPLAIRE (vérifié dans le HAR).
        body = {"card_id": card.copy_id, "base_amount": base_amount, "duration_minutes": duration_minutes}
        return self._api("POST", "/api/marketplace", json=body, retry="safe")

    def packs_left(self):
        """Boosters disponibles. Même appel que la page des boosters à chaque visite : le site y compte la recharge."""
        profile = self._rest("POST", "rpc/sync_profile_packs", json={"user_id": self.user_id}, retry="idempotent")
        left = profile.get("packs_remaining") if isinstance(profile, dict) else None
        if not _is_int(left):
            raise ApiError("nombre de boosters disponibles illisible")
        return left

    def open_pack(self):
        return self._api("POST", "/api/packs/open", retry="safe")

    def search_auctions(self, query, page):
        """Une page d'enchères du marché correspondant à query, de la plus proche de sa fin à la plus lointaine."""
        return self._api("GET", "/api/marketplace",
                         params={"page": page, "limit": PAGE_SIZE, "sort": "ending_soon", "q": query})

    def bid(self, auction_id, amount):
        return self._api("POST", f"/api/marketplace/{auction_id}/bid", json={"amount": amount}, retry="safe")

    def known_tags(self):
        """Étiquettes du compte : norm(nom) -> id (lues une fois)."""
        if self._tag_ids is None:
            tags = self._rest("GET", "tags", params={"select": "*", "user_id": f"eq.{self.user_id}"})
            self._tag_ids = {norm(t["name"]): t["id"] for t in tags or []
                             if isinstance(t, dict) and isinstance(t.get("name"), str) and t.get("id")}
        return self._tag_ids

    def tag_id(self, name, color):
        if norm(name) not in self.known_tags():
            created = self._rest(
                "POST",
                "tags",
                json={"user_id": self.user_id, "name": display_tag(name), "color": color},
                extra_headers={"Prefer": "return=representation"},
                retry="safe",  # renvoyée sans précaution, elle pourrait créer l'étiquette en double
            )
            if not (isinstance(created, list) and created and isinstance(created[0], dict) and created[0].get("id")):
                self._tag_ids = None  # relire les étiquettes au prochain usage
                raise ApiError(f"étiquette « {display_tag(name)} » créée mais le site n'a pas renvoyé son identifiant")
            self._tag_ids[norm(name)] = created[0]["id"]
        return self._tag_ids[norm(name)]

    def add_tag(self, card, name, color):
        tag_id = self.tag_id(name, color)
        try:
            self._rest("POST", "user_card_tags", json={"user_card_id": card.copy_id, "tag_id": tag_id},
                       retry="idempotent")
        except ApiError as e:
            # Doublon (code PostgreSQL 23505) : l'étiquette est déjà sur la carte, ce qui est le but recherché
            # (tentative précédente arrivée malgré une coupure, ou collection affichée en retard par le site).
            if not (e.status == 409 and e.code == "23505"):
                raise
        card.tag_ids[norm(name)] = tag_id

    def remove_tag(self, card, name):
        # Suppression via l'API Supabase standard (même table que l'ajout) ; à confirmer avec une capture.
        tag_id = card.tag_ids.get(norm(name)) or self.known_tags().get(norm(name))
        if not tag_id:
            raise ApiError(f"étiquette « {name} » introuvable sur le compte")
        removed = self._rest(
            "DELETE",
            "user_card_tags",
            params={"user_card_id": f"eq.{card.copy_id}", "tag_id": f"eq.{tag_id}"},
            extra_headers={"Prefer": "return=representation"},
            retry="idempotent",
        )
        # PostgREST répond OK même si rien n'a été supprimé (droits, mauvais filtre). Après une tentative coupée,
        # une réponse vide veut dire au contraire que cette tentative avait déjà retiré l'étiquette.
        if not removed and not self.last_uncertain:
            raise ApiError(f"étiquette « {name} » non retirée : suppression refusée par le site ?")
        card.tag_ids.pop(norm(name), None)


# --- Journal et suivi des ventes --------------------------------------------


JOURNAL_FIELDS = ["date", "compte", "commande", "action", "carte", "rarete", "exemplaire", "carte_id", "enchere_id",
                  "valeur_moyenne", "prix", "duree_min", "resultat", "solde"]


class Journal:
    """journal.csv : une ligne par action réelle (jamais en dry-run), lisible directement dans Excel."""

    def __init__(self, path, delimiter, enabled, account, command):
        self.path, self.delimiter, self.enabled = pathlib.Path(path), delimiter, enabled
        self.account, self.command = account, command
        # Si le journal est verrouillé (ouvert dans Excel), les lignes vont ici puis y sont recopiées plus tard.
        self.backup = self.path.with_name(f"{self.path.stem}_secours{self.path.suffix}")
        self.lost = 0
        self.diverted = 0
        self._checked = False

    def _number(self, x):
        if x is None or x is FAILED:
            return ""
        text = f"{x:.2f}".rstrip("0").rstrip(".") if isinstance(x, float) else str(x)  # jamais 1,2e+06
        return text.replace(".", ",") if self.delimiter == ";" else text  # Excel français : virgule décimale

    @staticmethod
    def _text(value):
        text = "" if value is None else str(value)
        # Un titre comme « +44 (groupe) » serait lu comme une formule par Excel : on le neutralise.
        return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text

    def _prepare(self):
        """Un ancien journal d'un autre format (colonnes, séparateur) est mis de côté plutôt que mélangé."""
        if self._checked:
            return
        self._checked = True
        try:
            if not self.path.exists() or self.path.stat().st_size == 0:
                return
            with open(self.path, encoding="utf-8-sig", newline="") as f:
                first = f.readline().rstrip("\r\n")
        except (OSError, UnicodeDecodeError):
            first = None
        if first != self.delimiter.join(JOURNAL_FIELDS):
            old = self.path.with_name(f"{self.path.stem}-ancien-{time.strftime('%Y%m%d-%H%M%S')}{self.path.suffix}")
            try:
                os.replace(self.path, old)
                print(f"Journal : ancien format mis de côté dans {old.name}, un nouveau journal commence.")
            except OSError:
                pass

    def write(self, action, name="", rarity="", copy_id="", card_id="", value=None, price=None, duration=None,
              result="OK", balance=None, auction_id=""):
        """Retourne False si la ligne n'a pas pu être écrite (fichier ouvert dans Excel, disque plein…)."""
        if not self.enabled:
            return True
        self._prepare()
        row = {
            "date": time.strftime("%Y-%m-%d %H:%M:%S"), "compte": self._text(self.account), "commande": self.command,
            "action": action, "carte": self._text(name), "rarete": self._text(rarity), "exemplaire": copy_id,
            "carte_id": card_id, "enchere_id": auction_id or "", "valeur_moyenne": self._number(value),
            "prix": self._number(price), "duree_min": self._number(duration), "resultat": self._text(result),
            "solde": self._number(balance),
        }
        self.merge_backup()
        try:
            self._append(self.path, [row])
        except OSError as e:
            try:
                self._append(self.backup, [row])
            except OSError:
                if not self.lost:
                    print(f"Attention : journal non écrit ({type(e).__name__}). Est-il ouvert dans Excel ? Fermez-le.")
                self.lost += 1
                return False
            if not self.diverted:
                print(f"Attention : {self.path.name} est verrouillé (Excel ?) : lignes écrites dans {self.backup.name}, "
                      "recopiées dans le journal au prochain passage.")
            self.diverted += 1
        return True

    def merge_backup(self):
        """Recopie dans le journal les lignes écrites dans le fichier de secours pendant qu'il était verrouillé."""
        if not self.enabled or not self.backup.exists():
            return
        self._prepare()
        pending = self._backup_rows()
        if not pending:
            return
        try:
            self._append(self.path, pending)
        except OSError:
            return  # toujours verrouillé : ce sera pour plus tard
        try:
            self.backup.unlink()
        except OSError:
            pass
        print(f"Journal : {len(pending)} ligne(s) de {self.backup.name} recopiée(s) dans {self.path.name}.")

    def _append(self, path, rows):
        new = not path.exists() or path.stat().st_size == 0
        # BOM UTF-8 seulement à la création : Excel reconnaît alors les accents.
        with open(path, "a", newline="", encoding="utf-8-sig" if new else "utf-8") as f:
            writer = csv.DictWriter(f, JOURNAL_FIELDS, delimiter=self.delimiter)
            if new:
                writer.writeheader()
            writer.writerows(rows)

    def _backup_rows(self):
        """Lignes en attente dans le fichier de secours ([] s'il n'existe pas ou n'a pas le bon format)."""
        try:
            with open(self.backup, encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f, delimiter=self.delimiter)
                rows = list(reader)
        except (OSError, UnicodeDecodeError, csv.Error):  # absent (cas normal) ou illisible : laissé tel quel
            return []
        return rows if reader.fieldnames == JOURNAL_FIELDS else []

    def card(self, action, card, value=None, **kw):
        return self.write(action, card.name, card.rarity, card.copy_id, card.card_id, value, **kw)


class AuctionFile:
    """Enchères suivies par le script dans un fichier JSON du compte : {"auctions": {id: entrée avec "status"}}.

    Seules les entrées aux statuts KEPT sont gardées à l'enregistrement, les autres sont terminées.
    """

    KEPT = ()
    WHAT = ""  # « suivi des … », pour les messages
    LOST = ""  # conséquence d'un enregistrement raté

    def __init__(self, path):
        self.path = pathlib.Path(path)
        self.auctions = {}
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            auctions = data.get("auctions") if isinstance(data, dict) else None
            if not isinstance(auctions, dict):
                raise ValueError
            self.auctions = {str(a): e for a, e in auctions.items() if isinstance(e, dict)}
        except (OSError, ValueError):
            backup = self.path.with_name(self.path.name + ".illisible")
            try:
                os.replace(self.path, backup)
            except OSError:
                pass
            print(f"Attention : {self.path.name} illisible, mis de côté dans {backup.name} : le {self.WHAT} repart à zéro.")

    def with_status(self, status):
        return [(aid, e) for aid, e in self.auctions.items() if e.get("status") == status]

    def set_status(self, auction_id, status, **extra):
        if auction_id in self.auctions:
            self.auctions[auction_id].update(status=status, **extra)

    def save(self):
        keep = {a: e for a, e in self.auctions.items() if e.get("status") in self.KEPT}
        try:
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.stem}.", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"auctions": keep}, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except OSError as e:
            print(f"Attention : {self.WHAT} non enregistré ({type(e).__name__}) : {self.LOST}.")


class SalesState(AuctionFile):
    """ventes.json : enchères lancées par le script, pour le bilan (vendue / invendue) et les relances.

    Statuts : open (en cours), unsold (revenue sans acheteur), abandoned (trop d'essais, on ne la vend plus) ;
    les autres (sold, cancelled, relisted, gone) sont terminés et retirés du fichier à l'enregistrement.
    """

    KEPT = ("open", "unsold", "abandoned")
    WHAT = "suivi des ventes"
    LOST = "le bilan et les relances seront incomplets"

    def latest_for(self, copy_id, status):
        found = [(e.get("listed_at") or 0, aid, e) for aid, e in self.with_status(status) if e.get("copy_id") == copy_id]
        return max(found, key=lambda x: x[0])[1:] if found else None

    def close_copy(self, copy_id, status="relisted"):
        """Clôt les anciennes entrées « invendue » d'un exemplaire qui vient d'être remis en vente."""
        for aid, entry in self.with_status("unsold"):
            if entry.get("copy_id") == copy_id:
                entry["status"] = status

    def record(self, auction_id, card, offer, value, duration):
        self.auctions[str(auction_id)] = {
            "copy_id": card.copy_id, "card_id": card.card_id, "name": card.name, "rarity": card.rarity,
            "price": offer.price, "value": value, "duration": duration, "attempt": offer.attempt,
            "listed_at": time.time(), "status": "open",
        }


class PurchaseState(AuctionFile):
    """achats.json : enchères du marché où le script a misé, jusqu'au résultat puis à l'étiquette posée sur la carte.

    Statuts : bid (mise posée, résultat pas encore lu), won (gagnée : buy.tag reste à poser sur la carte) ;
    les autres (lost, tagged, gone) sont terminés et retirés du fichier à l'enregistrement.
    """

    KEPT = ("bid", "won")
    WHAT = "suivi des achats"
    LOST = "le résultat des mises et l'étiquette des cartes gagnées seront incomplets"

    def record(self, target, amount):
        card = target.card
        self.auctions[target.auction_id] = {
            "card_id": card.card_id, "name": card.name, "rarity": card.rarity, "value": target.value,
            "price": amount, "end_at": target.end_at, "status": "bid",
        }


def pid_alive(pid):
    """Le processus existe-t-il encore ?"""
    if os.name == "nt":
        # Sous Windows, os.kill(pid, 0) n'est pas un test d'existence (0 y vaut CTRL_C_EVENT).
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.GetExitCodeProcess.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong))
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED : il existe, sous un autre compte
        try:
            code = ctypes.c_ulong()
            return not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True  # il existe, sous un autre compte
    except OSError:
        return False
    return True


class RunLock:
    """Empêche deux passages --execute en même temps (ils se marcheraient dessus dans ventes.json et le journal).

    Tant qu'il est tenu, le verrou est « touché » chaque minute, même pendant la pause de --loop.
    """

    MAX_AGE = 3600  # sans signe de vie depuis 1 h : passage planté ou figé (ou PID réutilisé), on reprend le verrou
    HEARTBEAT = 60

    def __init__(self, path):
        self.path = pathlib.Path(path)
        self.held = False
        self._stop = threading.Event()

    def acquire(self):
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                try:
                    content = self.path.read_text().split()
                    age = time.time() - self.path.stat().st_mtime
                except OSError:
                    continue  # verrou retiré entre-temps : on réessaie
                pid = int(content[0]) if content and content[0].isdigit() else None
                alive = pid_alive(pid) if pid else True  # sans PID lisible, seul l'âge du verrou compte
                if alive and age < self.MAX_AGE:
                    sys.exit(f"Un autre passage --execute est actif (PID {pid or '?'}). "
                             f"Sinon, supprimez {self.path.name}.")
                try:
                    os.unlink(self.path)  # passage disparu ou figé : on reprend le verrou
                except OSError:
                    pass
                continue
            with os.fdopen(fd, "w") as f:
                f.write(f"{os.getpid()} {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            self.held = True
            self._stop.clear()
            threading.Thread(target=self._heartbeat, daemon=True).start()
            return
        sys.exit(f"Impossible de prendre le verrou {self.path}.")

    def _heartbeat(self):
        while not self._stop.wait(self.HEARTBEAT):
            try:
                os.utime(self.path)
            except OSError:
                pass

    def release(self):
        if self.held:
            self._stop.set()
            try:
                os.unlink(self.path)
            except OSError:
                pass
            self.held = False


@dataclass
class Offer:
    """Mise en vente prévue : mise de départ, numéro d'essai et enchère invendue qu'elle relance."""

    price: int
    attempt: int = 1
    relaunches: str | None = None


@dataclass
class Context:
    client: object
    cfg: dict
    execute: bool
    summary: dict
    cache: object
    verbose: bool
    journal: Journal
    state: SalesState
    run_started: float = 0.0  # début du passage : « prix relu » = lu depuis ce moment
    collection_ids: set = field(default_factory=set)  # tous les exemplaires de la collection, lignes groupées comprises
    on_sale: set = field(default_factory=set)  # exemplaires dont l'enchère est confirmée en cours
    # Vérification anti-robot demandée par le site : groupe d'actions -> {"until": fin de la pause, "strikes": n}.
    # Gardé d'un cycle de --loop à l'autre ; relancer le script la lève (après une vérification faite à la main).
    antibot: dict = field(default_factory=dict)
    purchases: object = None  # PurchaseState : enchères du marché où le script a misé
    sniper: object = None  # Sniper, quand les achats sont actifs (« acheter », ou « tout --loop » avec buy.keywords)


# --- Commandes --------------------------------------------------------------


FAILED = object()
FINAL_ACTIONS = ("discard", "auction")


def new_summary():
    counters = ("tag_add", "tag_remove", "discard", "auction", "relist", "sold", "unsold", "no_slot", "error",
                "deferred", "pack", "bid", "won", "lost")
    # Ensembles d'exemplaires : une carte vue dans plusieurs phases de « tout » n'est comptée qu'une fois.
    sets = ("protect", "skip", "up_to_date", "read_error")
    # paused : groupe d'actions -> fin de sa pause anti-robot (voir antibot_strike)
    return {**{k: 0 for k in counters}, **{k: set() for k in sets}, "paused": {}}


def clock_time(timestamp):
    return time.strftime("%H:%M", time.localtime(timestamp))


def antibot_until(ctx, kind):
    """Fin de la pause anti-robot de ce type d'action, ou None s'il n'est pas en pause."""
    group = ANTIBOT_GROUPS[kind]
    entry = ctx.antibot.get(group)
    if entry and entry["until"] > time.time():
        ctx.summary["paused"][group] = entry["until"]
        return entry["until"]
    return None


def antibot_strike(ctx, kind):
    """Le site demande une vérification anti-robot : ce type d'action est mis en pause, plus longtemps à chaque fois.

    Insister ne la ferait pas disparaître et ressemblerait encore plus à un robot : on attend, et on prévient.
    """
    group = ANTIBOT_GROUPS[kind]
    entry = ctx.antibot.setdefault(group, {"strikes": 0})
    entry["until"] = time.time() + ANTIBOT_PAUSES[min(entry["strikes"], len(ANTIBOT_PAUSES) - 1)]
    entry["strikes"] += 1
    ctx.summary["paused"][group] = entry["until"]
    print(f"     Le site demande une vérification anti-robot : {group} en pause jusqu'à {clock_time(entry['until'])} "
          "(le reste continue).")
    print(f"     {ANTIBOT_HINT}")


def read_values(client, cards, cfg, cache, since=0, quiet=False):
    """Prix moyen de chaque carte (par card_id) ; FAILED si illisible. Lecture seule.

    since : les prix lus avant ce moment sont relus sur le site (garde-fou avant une action irréversible).
    quiet : pas d'annonce ni de progression (petits lots).
    """
    safety = cfg["safety"]
    values, failures_in_a_row = {}, 0
    distinct = list(dict.fromkeys(c.card_id for c in cards))
    if not distinct:
        return values
    fresh = ", prix relus sur le site pendant ce passage" if since else ""
    if not quiet:
        print(f"  lecture des prix de {len(distinct)} carte(s) (aucune modification{fresh})…")
    for card in cards:
        if card.card_id in values:
            continue
        if len(values) and len(values) % 50 == 0 and not quiet:
            print(f"  … {len(values)}/{len(distinct)}")
        cached = cache.get(card, since)
        if cached is not PriceCache.MISSING:
            values[card.card_id] = cached
            continue
        try:
            values[card.card_id] = client.card_value(card)
            cache.set(card, values[card.card_id])
            failures_in_a_row = 0
            if len(values) % 25 == 0:
                cache.save()
        except ApiError as e:
            if e.fatal:
                raise
            values[card.card_id] = FAILED
            failures_in_a_row += 1
            print(f"  ÉCHEC     {card.name} [{card.rarity}] : prix illisible ({e})")
            if failures_in_a_row >= safety["max_consecutive_read_failures"]:
                raise ApiError(f"{failures_in_a_row} lectures de prix échouées d'affilée", fatal=True)
        finally:
            delay = safety["read_delay_seconds"]
            client.pause(random.uniform(delay * 0.8, delay * 1.5))
    return values


def count_read_errors(cards, values, summary):
    summary["read_error"].update(c.copy_id for c in cards if values.get(c.card_id) is FAILED)


def plan_analyse(ctx, cards):
    """Étiquettes de prix (defausse, +10, +100…) d'après le prix moyen de vente."""
    cfg, summary = ctx.cfg, ctx.summary
    pt = cfg["price_tags"]
    targets = []
    for card in cards:
        reason = protection_reason(card, cfg["protection"])
        if reason and not pt["tag_protected"]:
            summary["protect"].add(card.copy_id)
            if ctx.verbose:
                print(f"  PROTÉGÉE  {card.name} ({reason})")
            continue
        targets.append(card)
    values = read_values(ctx.client, targets, cfg, ctx.cache)
    count_read_errors(targets, values, summary)

    steps, spread = [], {}
    for card in targets:
        value = values.get(card.card_id)
        if value is FAILED:
            continue
        if value is None:
            bucket = pt.get("unknown_tag") or "jamais vendue"
        else:
            bucket = (band_for(value, pt["bands"]) or {}).get("tag") or "hors tranches"
        spread[bucket] = spread.get(bucket, 0) + 1
        add, remove = tag_changes(card, value, cfg)
        if not add and not remove:
            summary["up_to_date"].add(card.copy_id)
        steps += [(card, "remove_tag", t, value) for t in remove]
        steps += [(card, "add_tag", t, value) for t in add]
    order = [b["tag"] for b in pt["bands"]] + ["hors tranches", pt.get("unknown_tag") or "jamais vendue"]
    shown = ", ".join(f"{name} : {spread[name]}" for name in order if spread.get(name))
    print(f"  répartition par prix moyen : {shown or 'aucune carte'}")
    if summary["protect"] and not ctx.verbose:
        print(f"  {len(summary['protect'])} carte(s) protégée(s) laissée(s) sans étiquette de prix (--verbose pour la liste)")
    wanted = {norm(t): t for c, kind, t, v in steps if kind == "add_tag"}
    if wanted:
        missing = [name for key, name in wanted.items() if key not in ctx.client.known_tags()]
        if missing:
            print(f"  étiquette(s) absente(s) du compte, créée(s) au premier usage : {', '.join(missing)}")
    return steps


def filter_candidates(ctx, cards, tags_for, other_tags, what, require_existing=False):
    """Exemplaires portant une des étiquettes tags_for(carte), hors protections et étiquettes contradictoires.

    require_existing : l'étiquette devait déjà être sur le site au début du passage (pas posée à l'instant par
    « analyser » dans « tout ») : vous avez ainsi le temps de la vérifier avant une action irréversible.
    """
    out, waiting = [], 0
    for card in cards:
        tags = tags_for(card)
        if not card.has_any_tag(tags):
            continue
        if require_existing and not card.has_any_tag(tags, initial_only=True):
            ctx.summary["skip"].add(card.copy_id)
            waiting += 1
            if ctx.verbose:
                print(f"  ignorée   {card.name} [{card.rarity}] : étiquetée {what} pendant ce passage, "
                      "traitée au prochain (vérifiez-la d'ici là)")
            continue
        reason = protection_reason(card, ctx.cfg["protection"])
        if reason:
            ctx.summary["protect"].add(card.copy_id)
            print(f"  PROTÉGÉE  {card.name} [{card.rarity}] ({reason}) : étiquetée {what}, on n'y touche pas")
            continue
        if card.has_any_tag(other_tags):
            ctx.summary["skip"].add(card.copy_id)
            print(f"  ignorée   {card.name} [{card.rarity}] : étiquettes contradictoires (vente et défausse)")
            continue
        out.append(card)
    if waiting and not ctx.verbose:
        print(f"  {waiting} carte(s) étiquetée(s) {what} pendant ce passage : traitée(s) au prochain passage "
              "(vérifiez-les d'ici là, --verbose pour la liste)")
    return out


SOLD_OR_FINISHED = ("settled", "sold", "expired", "unsold", "completed", "finished", "closed")
CANCELLED = ("cancelled", "canceled", "annulee", "annulée")
# Une enchère « open » dont on n'a aucune nouvelle au-delà de ce délai après sa fin est abandonnée.
STALE_AFTER = 2 * 86400


def classify_auction(auction):
    """sold / unsold / cancelled, ou None si l'enchère n'est pas (encore) clairement terminée."""
    if not isinstance(auction, dict):
        return None
    status = str(auction.get("status") or "").strip().lower()
    if status in CANCELLED:
        return "cancelled"
    if status == "active":
        return None
    if not (auction.get("settled_at") or status in SOLD_OR_FINISHED):
        return None  # terminée mais pas encore réglée, ou statut inconnu : on attend le prochain passage
    return "sold" if auction.get("winner_id") else "unsold"


def settle_sales(ctx):
    """Bilan des enchères lancées lors des passages précédents : vendue (prix final), invendue ou annulée."""
    opened = ctx.state.with_status("open")
    if not opened:
        return
    print(f"  bilan de {len(opened)} enchère(s) lancée(s) précédemment…")
    for auction_id, entry in opened:
        name, rarity = entry.get("name", ""), entry.get("rarity", "")
        auction = None
        try:
            data = ctx.client.get_auction(auction_id)
            auction = data.get("auction", data) if isinstance(data, dict) else None
        except ApiError as e:
            if e.fatal:
                raise
            print(f"  ÉCHEC     bilan de l'enchère {name} : {e}")
        finally:
            ctx.client.pause(ctx.cfg["safety"]["read_delay_seconds"])
        outcome = classify_auction(auction)
        back = entry.get("copy_id") in ctx.collection_ids
        status = auction.get("status") if isinstance(auction, dict) else None
        if outcome is None and status == "active":
            ctx.on_sale.add(entry.get("copy_id"))  # confirmé en cours par le site : ne pas la remettre en vente
            continue
        if outcome is None:
            if back:
                # Une carte en vente disparaît de la collection : si elle est revenue, l'enchère est finie sans vente.
                outcome = "unsold"
            else:
                ended = (entry.get("listed_at") or 0) + 60 * (entry.get("duration") or 0)
                if time.time() - ended > STALE_AFTER:
                    print(f"  ?         {name} : aucune nouvelle de l'enchère depuis 2 jours, suivi abandonné")
                    ctx.state.set_status(auction_id, "gone")
                elif status not in (None, "active"):
                    print(f"  ?         {name} : statut d'enchère « {status} » non reconnu, on attend")
                continue
        common = dict(name=name, rarity=rarity, copy_id=entry.get("copy_id", ""), card_id=entry.get("card_id", ""),
                      value=entry.get("value"), duration=entry.get("duration"), auction_id=auction_id)
        if outcome == "sold":
            final = auction.get("final_price") or auction.get("current_bid") or auction.get("effective_bid")
            print(f"  VENDUE    {name} [{rarity}] : {final} wikicoins (mise de départ {entry.get('price')})")
            written = ctx.journal.write("vendue", price=final, **common)
            new_status, extra, counter = "sold", {"final_price": final}, "sold"
        elif outcome == "cancelled":
            print(f"  ANNULÉE   {name} [{rarity}] : annulée sur le site, pas de relance automatique")
            written = ctx.journal.write("annulee", price=entry.get("price"), **common)
            new_status, extra, counter = "cancelled", {}, None
        else:
            print(f"  INVENDUE  {name} [{rarity}] (mise de départ {entry.get('price')}, essai {entry.get('attempt', 1)})")
            written = ctx.journal.write("invendue", price=entry.get("price"), **common)
            new_status, extra, counter = "unsold", {}, "unsold"
        if counter:
            ctx.summary[counter] += 1
        if written:  # sinon on refera le bilan au prochain passage, pour ne pas perdre la ligne du journal
            ctx.state.set_status(auction_id, new_status, **extra)
    if ctx.execute:
        ctx.state.save()  # tout de suite : un plantage ensuite ne doit pas faire journaliser deux fois


def relist_price(previous, value, sell):
    """Mise d'une relance : moins que la précédente, jamais plus que la mise normale au prix actuel."""
    relist = sell["relist"]
    price = int(ROUNDING[sell["rounding"]](round(previous * relist["factor"], 6)))
    price = min(price, start_price(value, sell))
    if previous > relist["min_start_price"]:
        price = min(price, previous - 1)  # une relance baisse toujours d'au moins 1, même avec l'arrondi supérieur
    if sell.get("max_start_price"):
        price = min(price, sell["max_start_price"])
    return max(price, relist["min_start_price"])


def plan_sell(ctx, cards):
    """Mises aux enchères : relances des invendus, puis cartes étiquetées à vendre, dans les places libres."""
    cfg, summary = ctx.cfg, ctx.summary
    sell = cfg["sell"]
    relist = sell["relist"]
    settle_sales(ctx)
    # Invendus dont la carte n'est plus dans la collection (échangée, défaussée…) : plus rien à suivre.
    for status in ("unsold", "abandoned"):
        for auction_id, entry in ctx.state.with_status(status):
            if entry.get("copy_id") not in ctx.collection_ids:
                ctx.state.set_status(auction_id, "gone")
    until = antibot_until(ctx, "auction")
    if until:
        print(f"  mises en vente en pause jusqu'à {clock_time(until)} : le site a demandé une vérification anti-robot")
        return []
    candidates = filter_candidates(ctx, cards, lambda card: sell["tags"], cfg["discard"]["tags"], "à vendre",
                                   sell["require_existing_tag"])
    for card in [c for c in candidates if c.copy_id in ctx.on_sale]:
        candidates.remove(card)
        summary["skip"].add(card.copy_id)
        print(f"  ignorée   {card.name} [{card.rarity}] : déjà en vente")
    # Pas de place libre : inutile de lire des centaines de prix pour rien.
    if candidates and min(ctx.client.auction_slots(), sell["max_auctions"]) <= 0:
        summary["no_slot"] += len(candidates)
        print(f"  {len(candidates)} carte(s) à vendre, mais aucune place d'enchère libre : rien à faire maintenant")
        return []
    values = read_values(ctx.client, candidates, cfg, ctx.cache, ctx.run_started if sell["fresh_price"] else 0)
    count_read_errors(candidates, values, summary)

    relists, fresh = [], []
    for card in candidates:
        value = values.get(card.card_id)
        if value is FAILED:
            continue
        if value is None:
            summary["skip"].add(card.copy_id)
            print(f"  ignorée   {label(card, value)} : jamais vendue, prix inconnu")
            continue
        if sell["min_value"] is not None and value < sell["min_value"]:
            summary["skip"].add(card.copy_id)
            print(f"  ignorée   {label(card, value)} : vaut moins de {sell['min_value']:g} (relancez analyser)")
            continue
        if ctx.state.latest_for(card.copy_id, "abandoned"):
            summary["skip"].add(card.copy_id)
            print(f"  ignorée   {label(card, value)} : invendue {relist['max_attempts']} fois, on ne la remet plus en vente"
                  " (retirez-la de ventes.json pour réessayer)")
            continue
        previous = ctx.state.latest_for(card.copy_id, "unsold") if relist["enabled"] else None
        if previous:
            auction_id, entry = previous
            attempt = int(entry.get("attempt", 1)) + 1
            if attempt > relist["max_attempts"]:
                summary["skip"].add(card.copy_id)
                ctx.state.set_status(auction_id, "abandoned")
                print(f"  ignorée   {label(card, value)} : invendue {attempt - 1} fois (sell.relist.max_attempts)")
                continue
            relists.append((card, value, Offer(relist_price(entry["price"], value, sell), attempt, auction_id)))
        else:
            fresh.append((card, value, Offer(start_price(value, sell), 1)))

    if sell["order"] == "random":
        # Tirage fixe pour la journée : le dry-run montre les mêmes cartes que le --execute qui suit.
        fresh.sort(key=lambda o: o[0].copy_id)
        random.Random(f"{ctx.client.user_id}:{time.strftime('%Y-%m-%d')}").shuffle(fresh)
    elif sell["order"] == "tags":
        rank = {norm(t): i for i, t in enumerate(sell["tags"])}
        fresh.sort(key=lambda o: (min(rank.get(norm(t), len(rank)) for t in o[0].tags), -o[1]))
    else:
        fresh.sort(key=lambda o: -o[1])
    offers = relists + fresh if relist["first"] else fresh + relists

    # Relu après la lecture des prix, qui peut durer : des enchères ont pu se terminer entre-temps.
    slots = ctx.client.auction_slots()
    room = max(0, min(slots, sell["max_auctions"]))
    print(f"  {len(offers)} carte(s) à vendre dont {len(relists)} relance(s), {slots} place(s) d'enchère libre(s) "
          f"→ {min(room, len(offers))} vente(s)")
    chosen, waiting = offers[:room], offers[room:]
    summary["no_slot"] += len(waiting)
    limit = len(waiting) if ctx.verbose else cfg.get("display", {}).get("waiting_shown", 5)
    for card, value, _ in waiting[:limit]:
        print(f"  en attente d'une place : {label(card, value)}")
    if len(waiting) > limit:
        print(f"  … et {len(waiting) - limit} autre(s) en attente (--verbose pour tout voir)")
    return [(card, "auction", offer, value) for card, value, offer in chosen]


def plan_discard(ctx, cards):
    """Défausses : cartes « defausse », et cartes jamais vendues (« inconnu ») des raretés de discard.unknown_rarities.

    Générateur de lots de DISCARD_BATCH cartes : le prix de chaque lot est relu sur le site (sauf
    discard.fresh_price: false) juste avant de le défausser. Les défausses commencent donc tout de suite, et le prix
    vérifié date d'une minute, pas du début de la relecture. Seules les cartes nécessaires pour remplir
    discard.max_per_run sont lues, la suite attend le passage suivant.
    """
    cfg, summary = ctx.cfg, ctx.summary
    until = antibot_until(ctx, "discard")
    if until:
        print(f"  défausses en pause jusqu'à {clock_time(until)} : le site a demandé une vérification anti-robot")
        return
    disc = cfg["discard"]
    unknown_tag = cfg["price_tags"].get("unknown_tag")
    unknown_rarities = {norm(r) for r in disc["unknown_rarities"]}

    def tags_for(card):
        if unknown_tag and norm(card.rarity) in unknown_rarities:
            return disc["tags"] + [unknown_tag]
        return disc["tags"]

    candidates = filter_candidates(ctx, cards, tags_for, cfg["sell"]["tags"], "à défausser",
                                   disc["require_existing_tag"])
    candidates.sort(key=lambda c: not c.has_any_tag(disc["tags"]))  # « defausse » d'abord, puis « inconnu »
    since = ctx.run_started if disc["fresh_price"] else 0
    limit = disc["max_per_run"]
    if candidates and limit:
        fresh = " ; prix relu sur le site juste avant" if since else ""
        print(f"  {len(candidates)} carte(s) à défausser, {min(len(candidates), limit)} au plus ce passage, "
              f"par lots de {DISCARD_BATCH}{fresh}")
    planned, rest = 0, candidates
    while rest and planned < limit:
        if antibot_until(ctx, "discard"):  # vérification anti-robot demandée pendant ce passage
            summary["deferred"] += len(rest)
            return
        size = min(DISCARD_BATCH, limit - planned)
        batch, rest = rest[:size], rest[size:]
        values = read_values(ctx.client, batch, cfg, ctx.cache, since, quiet=True)
        count_read_errors(batch, values, summary)
        steps = []
        for card in batch:
            value = values.get(card.card_id)
            if value is FAILED:
                continue
            if value is None and norm(card.rarity) not in unknown_rarities:
                summary["skip"].add(card.copy_id)
                print(f"  ignorée   {label(card, value)} : jamais vendue, prix inconnu (discard.unknown_rarities)")
                continue
            if value is not None and disc["max_value"] is not None and value >= disc["max_value"]:
                summary["skip"].add(card.copy_id)
                print(f"  ignorée   {label(card, value)} : vaut maintenant {value:g} (>= {disc['max_value']:g}), "
                      "relancez analyser")
                continue
            steps.append((card, "discard", None, value))
        planned += len(steps)
        steps.sort(key=lambda s: -1 if s[3] is None else s[3])  # les moins chères d'abord
        yield steps
    if rest:
        summary["deferred"] += len(rest)
        print(f"  {len(rest)} autre(s) carte(s) à défausser : {limit} au plus par passage (discard.max_per_run), "
              "la suite au prochain passage")


class Runner:
    """Exécute (ou simule en dry-run) une liste d'étapes, avec les garde-fous communs à toutes les commandes."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.client, self.cfg, self.execute, self.summary = ctx.client, ctx.cfg, ctx.execute, ctx.summary
        self.actions = 0
        self.errors_in_a_row = 0
        self.consumed = set()  # exemplaires déjà défaussés ou vendus : jamais deux fois
        self.failed = set()  # exemplaires dont une action a échoué : plus rien sur eux pendant ce passage
        self.colors = {norm(b["tag"]): b.get("color") for b in self.cfg["price_tags"]["bands"]}
        self.colors.update({norm(r["tag"]): r.get("color") for r in self.cfg.get("auto_tags", [])})

    def run(self, steps):
        """Retourne False si la limite d'actions est atteinte (la suite est reportée)."""
        limit = self.cfg["safety"]["max_actions_per_run"]
        failed = self.failed
        for i, (card, kind, arg, value) in enumerate(steps):
            if card.copy_id in failed or card.copy_id in self.consumed:
                continue
            if self.actions >= limit:
                left = sum(1 for c, *_ in steps[i:] if c.copy_id not in failed and c.copy_id not in self.consumed)
                self.summary["deferred"] += left
                print(f"\n  Limite de {limit} actions atteinte (safety.max_actions_per_run) : "
                      f"{left} action(s) reportée(s), relancez pour continuer.")
                return False
            if self.execute and self.client.session_lost:
                raise ApiError(self.client.session_lost, fatal=True)
            if antibot_until(self.ctx, kind):
                self.summary["deferred"] += 1
                continue
            print(f"  -> {self._describe(kind, arg):<26} {label(card, value)}")
            if self.execute:
                try:
                    result = self._send(card, kind, arg, value)
                except KeyboardInterrupt:
                    print(f"     Interrompu pendant l'envoi. {MAYBE_DONE}")
                    self._log(card, kind, arg, value, f"INTERROMPU : {MAYBE_DONE}")
                    raise
                except ApiError as e:
                    self._log(card, kind, arg, value, f"ECHEC : {e}")
                    if e.fatal:
                        raise
                    self.summary["error"] += 1
                    failed.add(card.copy_id)  # on n'enchaîne pas les autres actions prévues pour cette carte
                    print(f"     ÉCHEC : {e}")
                    if e.antibot:
                        antibot_strike(self.ctx, kind)  # les actions suivantes de ce type attendront
                    else:
                        self.errors_in_a_row += 1
                        if self.errors_in_a_row >= self.cfg["safety"]["max_consecutive_errors"]:
                            raise ApiError(f"{self.errors_in_a_row} erreurs consécutives", fatal=True)
                    self.client.pause(self.cfg["safety"]["delay_seconds"])  # jamais deux requêtes collées
                    continue
            self._apply(card, kind, arg)
            # Compté tout de suite : un Ctrl+C pendant la pause ne doit pas faire disparaître l'action du résumé.
            self.summary[{"add_tag": "tag_add", "remove_tag": "tag_remove"}.get(kind, kind)] += 1
            if kind == "auction" and arg.attempt > 1:
                self.summary["relist"] += 1
            self.actions += 1
            if self.execute:
                self.errors_in_a_row = 0
                self.ctx.antibot.pop(ANTIBOT_GROUPS[kind], None)  # acceptée à nouveau : la prochaine pause repart à 1 h
                print(f"     {result}")
                # Des mises en vente enchaînées en quelques secondes déclenchent la vérification anti-robot du site.
                safety = self.cfg["safety"]
                self.client.pause(safety["auction_delay_seconds"] if kind == "auction" else safety["delay_seconds"])
        return True

    @staticmethod
    def _describe(kind, arg):
        if kind == "auction":
            return f"ENCHÈRE départ={arg.price}" + (f" (relance {arg.attempt})" if arg.attempt > 1 else "")
        return {"add_tag": f"+ étiquette « {arg} »", "remove_tag": f"- étiquette « {arg} »", "discard": "DÉFAUSSE"}[kind]

    def _log(self, card, kind, arg, value, result, balance=None, auction_id=""):
        action = {"add_tag": "etiquette+", "remove_tag": "etiquette-", "discard": "defausse",
                  "auction": "relance" if kind == "auction" and arg.attempt > 1 else "enchere"}[kind]
        extra = {}
        if kind == "auction":
            extra = dict(price=arg.price, duration=self.cfg["sell"]["duration_minutes"])
        elif kind in ("add_tag", "remove_tag"):
            result = f"{result} ({arg})"
        self.ctx.journal.card(action, card, value, result=result, balance=balance, auction_id=auction_id, **extra)

    def _send(self, card, kind, arg, value):
        if kind == "add_tag":
            self.client.add_tag(card, arg, self.colors.get(norm(arg)) or "#94a3b8")
            self._log(card, kind, arg, value, "OK")
            return "OK"
        if kind == "remove_tag":
            self.client.remove_tag(card, arg)
            self._log(card, kind, arg, value, "OK")
            return "OK"
        if kind == "discard":
            balance = (self.client.discard(card) or {}).get("balance")
            self._log(card, kind, arg, value, "OK", balance=balance)
            return f"OK, solde : {'?' if balance is None else balance}"
        duration = self.cfg["sell"]["duration_minutes"]
        response = self.client.create_auction(card, arg.price, duration) or {}
        auction_id = response.get("auction_id")
        # D'abord clore les anciens invendus de cet exemplaire : le site pourrait redonner le même identifiant.
        self.ctx.state.close_copy(card.copy_id)
        if auction_id:
            self.ctx.state.record(auction_id, card, arg, value, duration)
        self.ctx.state.save()  # tout de suite : un plantage ne doit pas faire oublier une enchère en cours
        self._log(card, kind, arg, value, "OK", auction_id=auction_id or "")
        return "OK"

    def _apply(self, card, kind, arg):
        """Met à jour la carte en mémoire, pour que les commandes suivantes de « tout » voient le résultat."""
        if kind == "add_tag":
            card.tags.append(arg)
        elif kind == "remove_tag":
            card.tags = [t for t in card.tags if norm(t) != norm(arg)]
        else:
            self.consumed.add(card.copy_id)


def open_packs(ctx):
    """Ouvre les boosters disponibles (au plus packs.max_per_run), espacés de packs.delay_seconds."""
    client, packs, summary = ctx.client, ctx.cfg["packs"], ctx.summary
    until = antibot_until(ctx, "pack")
    if until:
        print(f"  boosters en pause jusqu'à {clock_time(until)} : le site a demandé une vérification anti-robot")
        return
    try:
        left = client.packs_left()
    except ApiError as e:
        if e.fatal:
            raise
        print(f"  ÉCHEC     boosters : {e}")
        return
    count = min(left, packs["max_per_run"])
    print(f"  {left} booster(s) disponible(s) → {count} ouverture(s)")
    if not ctx.execute:
        summary["pack"] += count
        return
    for n in range(count):
        if n:
            delay = packs["delay_seconds"]
            client.pause(random.uniform(delay * 0.8, delay * 1.5))  # un rythme trop régulier ferait robot
        try:
            data = client.open_pack() or {}
        except ApiError as e:
            ctx.journal.write("booster", result=f"ECHEC : {e}")
            if e.fatal:
                raise
            summary["error"] += 1
            print(f"  ÉCHEC     booster : {e}")
            if e.antibot:
                antibot_strike(ctx, "pack")
            return
        summary["pack"] += 1
        ctx.antibot.pop(ANTIBOT_GROUPS["pack"], None)  # accepté à nouveau : la prochaine pause repart à 1 h
        cards = [c for c in data.get("cards") or [] if isinstance(c, dict)]
        shown = ", ".join(f"{c.get('wikipedia_title', '?')} [{c.get('rarity', '?')}]" for c in cards)
        print(f"  booster {n + 1}/{count} : {shown or 'aucune carte reçue'}")
        for c in cards:
            ctx.journal.write("booster", c.get("wikipedia_title", ""), c.get("rarity", ""),
                              str(c.get("user_card_id") or ""), str(c.get("id") or ""))
        if _is_int(data.get("packs_remaining")) and data["packs_remaining"] <= 0:
            break


@dataclass
class Target:
    """Enchère du marché suivie par les achats : mise à fin - buy.snipe_seconds, revue à fin - LAST_CHECK_SECONDS."""

    auction_id: str
    card: Card  # copy_id vide : la carte n'est pas (encore) à nous
    end_at: float
    value: float
    ceiling: int | None  # None : pas de plafond
    stage: int = 0  # 0 : mise à venir, 1 : dernière vérification à venir, 2 : fin (ou prolongation) à constater
    bid: int | None = None  # dernière mise du script sur cette enchère


class Sniper:
    """Achats aux enchères : repère sur le marché les annonces qui correspondent à buy.keywords, puis mise le minimum
    quelques secondes avant la fin, sans jamais dépasser le plafond (prix moyen de la carte × buy.price_factor).

    Tout se fait pendant les pauses (client.waiter) : celle de --loop entre deux passages, et celles entre deux
    requêtes d'un passage. Une seule requête à la fois et une seule session, donc rien à partager entre processus.
    """

    def __init__(self, ctx, one_shot=False, mine=None):
        self.ctx, self.client, self.buy = ctx, ctx.client, ctx.cfg["buy"]
        self.one_shot = one_shot  # sans --loop : « acheter » attend la fin des enchères repérées, puis s'arrête
        # Vos autres comptes enregistrés (id -> nom) : on ne surenchérit pas sur eux et on n'achète pas leurs annonces.
        self.mine = {k: v for k, v in (mine or {}).items() if k != self.client.user_id}
        self.targets = {}  # auction_id -> Target
        self.ignored = {}  # auction_id -> fin : annonces écartées pour de bon (trop chère, déjà possédée…)
        self.results = {}  # auction_id -> moment de lire le résultat d'une enchère où le script a misé
        self.next_scan = 0.0
        self.busy = False  # lecture du marché ou mise en cours : une pause demandée entre-temps est une simple pause
        if ctx.execute:
            for aid, entry in ctx.purchases.with_status("bid"):
                self.results[aid] = (entry.get("end_at") or 0) + RESULT_DELAY

    def say(self, text):
        print(f"  [achats] {text}")

    # --- attente ---

    def wait(self, seconds, scan=True):
        """Remplace time.sleep : attend `seconds` secondes en traitant les enchères qui arrivent à échéance."""
        deadline = time.time() + seconds
        if self.busy:
            time.sleep(seconds)
            return
        while True:
            if self._paused():
                time.sleep(max(0.0, deadline - time.time()))
                return
            self._run_due(scan)
            now = time.time()
            # Vérification imminente : on l'attend, plutôt que de laisser partir une requête qui la retarderait.
            soon = self._next_check() <= now + CHECK_MARGIN
            if now >= deadline and not soon:
                return
            until = self._next_event(scan) if soon else min(deadline, self._next_event(scan))
            time.sleep(max(0.0, until - now))

    def drain(self):
        """Sans --loop : attend la fin des enchères repérées, et leur résultat, sans relire le marché."""
        while not self._paused():
            due = self._next_event(scan=False)
            if due == math.inf:
                return
            self.wait(max(0.0, due - time.time()), scan=False)

    def _paused(self):
        return antibot_until(self.ctx, "bid") is not None

    def _due(self, target):
        if target.stage == 0:
            return target.end_at - self.buy["snipe_seconds"]
        if target.stage == 1:
            return target.end_at - LAST_CHECK_SECONDS
        return target.end_at + AFTER_END_SECONDS

    def _next_check(self):
        return min((self._due(t) for t in self.targets.values()), default=math.inf)

    def _clear_ahead(self):
        """Avant une lecture du marché : les vérifications des secondes à venir passent d'abord."""
        while not self._paused():
            due, now = self._next_check(), time.time()
            if due > now + CHECK_MARGIN:
                return
            time.sleep(max(0.0, due - now))
            self._checks_due()

    def _next_event(self, scan):
        times = [self._due(t) for t in self.targets.values()] + list(self.results.values())
        return min(times + ([self.next_scan] if scan else []), default=math.inf)

    def _run_due(self, scan):
        if scan and time.time() >= self.next_scan:
            self._guarded(self.scan)
        self._checks_due()
        for aid, at in sorted(self.results.items(), key=lambda x: x[1]):
            if at <= time.time():
                self._guarded(self._result, aid)

    def _checks_due(self):
        for target in sorted(self.targets.values(), key=self._due):
            if self._due(target) > time.time():
                break
            if target.auction_id in self.targets:
                self._guarded(self._check, target)

    def _guarded(self, action, *args):
        """Une erreur n'arrête pas les achats (sauf session perdue) : elle est affichée, la suite continue."""
        was_busy, self.busy = self.busy, True
        try:
            action(*args)
        except ApiError as e:
            if e.antibot:
                antibot_strike(self.ctx, "bid")
            elif e.fatal and self.client.session_lost:
                raise
            else:
                self.say(f"ÉCHEC : {e}")
        finally:
            self.busy = was_busy

    # --- marché ---

    def scan(self):
        """Relit le marché : annonces qui se terminent avant la relecture d'après (avec de la marge)."""
        now = time.time()
        self.next_scan = now + self.buy["scan_minutes"] * 60
        horizon = now + 2 * self.buy["scan_minutes"] * 60 + self.buy["snipe_seconds"]
        self.ignored = {aid: end for aid, end in self.ignored.items() if end > now}
        found = {}
        for keyword in self.buy["keywords"]:
            for page in range(1, MAX_SCAN_PAGES + 1):
                self._clear_ahead()
                data = self.client.search_auctions(keyword, page) or {}
                self.client.pause(self.ctx.cfg["safety"]["read_delay_seconds"])
                auctions = [a for a in data.get("auctions") or [] if isinstance(a, dict)]
                ends = [parse_time(a.get("end_at")) for a in auctions]
                for auction, end in zip(auctions, ends):
                    if end is not None and now + LAST_CHECK_SECONDS + 2 < end <= horizon:
                        found.setdefault(str(auction.get("id")), (auction, end))
                if not data.get("hasMore") or not auctions or None in ends or max(ends) > horizon:
                    break
        new = sorted((v for aid, v in found.items() if aid not in self.targets and aid not in self.ignored),
                     key=lambda v: v[1])
        read = 0
        for auction, end in new:
            self._clear_ahead()  # lire un prix prend du temps : les mises à faire bientôt passent avant
            read += self._consider(auction, end)
        if read:
            self.ctx.cache.save()

    def _consider(self, auction, end):
        """Suit l'annonce si elle convient. Retourne 1 si un prix a été lu sur le site, 0 sinon."""
        aid = str(auction.get("id"))
        info = auction.get("card") if isinstance(auction.get("card"), dict) else {}
        card = Card(copy_id="", card_id=str(auction.get("card_id") or info.get("id") or ""),
                    name=str(info.get("wikipedia_title") or "?"),
                    rarity=str(auction.get("snapshot_rarity") or info.get("rarity") or ""),
                    is_shiny=bool(auction.get("is_shiny")))
        reason, value, ceiling, read = self._reject(auction, info, card), None, None, 0
        quiet = bool(reason)
        if not reason:
            cached = self.ctx.cache.get(card)
            if cached is not PriceCache.MISSING:
                value = cached
            elif self.buy["price_factor"] is not None:  # sans plafond lié au prix, inutile de le lire
                read = 1
                try:
                    value = self._value(card)
                except ApiError as e:
                    if e.fatal:
                        raise
                    self.say(f"ÉCHEC     {card.name} [{card.rarity}] : prix illisible ({e})")
                    return read  # nouvel essai à la prochaine relecture
            need, ceiling = next_bid(auction), buy_ceiling(value, self.buy)
            if value is None and self.buy["price_factor"] is not None and not self.buy["buy_unknown"]:
                reason = "jamais vendue, prix inconnu (buy_unknown: true pour l'acheter quand même)"
            elif need is None:
                reason = "mise actuelle illisible"
            elif ceiling is not None and need > ceiling:
                reason = f"mise minimale {need} > plafond {ceiling}"
        if reason:
            self.ignored[aid] = end
            if self.ctx.verbose or not quiet:
                self.say(f"ignorée   {label(card, value)} : {reason}")
            return read
        self.targets[aid] = Target(aid, card, end, value, ceiling)
        current = auction.get("current_bid")
        self.say(f"repérée   {label(card, value)} : fin à {time.strftime('%H:%M:%S', time.localtime(end))}, "
                 f"mise actuelle {'aucune' if current is None else current}, plafond {show_ceiling(ceiling)}")
        return read

    def _reject(self, auction, info, card):
        """Pourquoi cette annonce n'est pas pour nous (None si elle l'est)."""
        if auction.get("status") != "active":
            return "enchère terminée"
        if auction.get("seller_id") == self.client.user_id or auction.get("seller_id") in self.mine:
            return "votre propre annonce"
        if self.buy["skip_owned"] and auction.get("owned"):
            return "déjà dans votre collection"
        # Deux annonces de la même carte : on n'en suit qu'une, pour ne pas l'acheter deux fois.
        if any(t.card.card_id == card.card_id for t in self.targets.values()) \
                or any(e.get("card_id") == card.card_id for status in ("bid", "won")
                       for _, e in self.ctx.purchases.with_status(status)):
            return "même carte déjà suivie ou achetée"
        # Le site cherche peut-être plus large : on vérifie que le mot-clé est bien dans le titre ou la catégorie.
        text = norm(" ".join(str(x or "") for x in (auction.get("snapshot_search_document"),
                                                     info.get("wikipedia_title"), info.get("category"))))
        # En début de mot : « lyon » trouve « lyonnais » ou « To-Lyon », pas « Elyon ».
        if not any(re.search(r"(?<!\w)" + re.escape(norm(k)), text) for k in self.buy["keywords"]):
            return "mot-clé absent du titre et de la catégorie"
        return None

    def _value(self, card):
        try:
            value = self.client.card_value(card)
        finally:
            self.client.pause(self.ctx.cfg["safety"]["read_delay_seconds"])
        self.ctx.cache.set(card, value)
        return value

    # --- mises ---

    def _check(self, target):
        """Suivie jusqu'à sa vraie fin : une mise de dernière minute (la nôtre ou une autre) la fait prolonger."""
        stage = target.stage
        target.stage = min(stage + 1, 2)  # avancé d'abord : un échec ne la refait pas en boucle
        data = self.client.get_auction(target.auction_id)
        auction = data.get("auction", data) if isinstance(data, dict) else {}
        end, now = parse_time(auction.get("end_at")) or target.end_at, time.time()
        if auction.get("status") != "active" or end <= now:
            self._finish(target)
            return
        if end > target.end_at + 1:
            target.end_at, target.stage = end, 0
            self.say(f"prolongée {target.card.name} : nouvelle fin à {time.strftime('%H:%M:%S', time.localtime(end))}"
                     f" (mise actuelle {auction.get('current_bid')})")
            return
        if stage >= 2:
            return  # pas encore finie (horloge de ce PC en avance ?) : revue un peu plus tard
        current, leader, need = auction.get("current_bid"), auction.get("current_bidder_id"), next_bid(auction)
        # Simulation : la mise n'est pas partie, mais tant que personne n'a misé depuis, elle serait en tête.
        simulated = not self.ctx.execute and target.bid is not None and need is not None and need <= target.bid
        if leader == self.client.user_id or simulated:
            shown = f"{target.bid} (simulation)" if simulated else current
            self.say(f"en tête   {label(target.card, target.value)} : {shown} "
                     f"(plafond {show_ceiling(target.ceiling)}), fin dans {end - now:.0f} s")
        elif leader in self.mine:
            self.say(f"laissée   {label(target.card, target.value)} : votre compte {self.mine[leader]} est en tête")
            self._finish(target)
        elif need is None or (target.ceiling is not None and need > target.ceiling):
            self.say(f"trop chère {label(target.card, target.value)} : mise minimale {need} > plafond "
                     f"{target.ceiling}, on laisse")
            self._finish(target)
        else:
            outbid = f"dépassé à {current}, " if target.bid is not None else ""
            self._bid(target, need, end - now, outbid)

    def _bid(self, target, amount, left, note=""):
        ctx = self.ctx
        self.say(f"-> MISE {amount:<8} {label(target.card, target.value)} ({note}plafond "
                 f"{show_ceiling(target.ceiling)}, fin dans {left:.0f} s)")
        if not ctx.execute:
            target.bid = amount
            ctx.summary["bid"] += 1
            return
        try:
            response = self.client.bid(target.auction_id, amount) or {}
        except ApiError as e:
            self._log(target, amount, f"ECHEC : {e}")
            if e.fatal:
                # Mise peut-être partie : on lira le résultat de l'enchère comme si elle l'était.
                target.bid = amount
                ctx.purchases.record(target, amount)
                ctx.purchases.save()
                raise
            ctx.summary["error"] += 1
            self.say(f"   ÉCHEC : {e}")
            if e.antibot:
                antibot_strike(ctx, "bid")
            return
        target.bid = amount
        ctx.purchases.record(target, amount)
        ctx.purchases.save()  # tout de suite : un plantage ne doit pas faire oublier une mise
        balance = response.get("bidder_balance")
        self._log(target, amount, "OK", balance)
        ctx.summary["bid"] += 1
        ctx.antibot.pop(ANTIBOT_GROUPS["bid"], None)
        self.say(f"   OK, solde : {'?' if balance is None else balance}")

    def _log(self, target, amount, result, balance=None):
        card = target.card
        self.ctx.journal.write("mise", card.name, card.rarity, "", card.card_id, target.value, price=amount,
                               result=result, balance=balance, auction_id=target.auction_id)

    def _finish(self, target):
        self.targets.pop(target.auction_id, None)
        self.ignored[target.auction_id] = target.end_at  # pas reprise à la prochaine relecture
        if target.bid is not None and self.ctx.execute:
            self.results[target.auction_id] = target.end_at + RESULT_DELAY

    def _result(self, aid):
        """Gagnée ou perdue ? Une carte gagnée recevra buy.tag au prochain tri de la collection."""
        purchases = self.ctx.purchases
        entry = purchases.auctions.get(aid)
        if not entry or entry.get("status") != "bid":
            self.results.pop(aid, None)
            return
        self.results[aid] = time.time() + RESULT_RETRY  # si la lecture échoue ou si l'enchère n'est pas réglée
        data = self.client.get_auction(aid)
        auction = data.get("auction", data) if isinstance(data, dict) else None
        outcome, name = classify_auction(auction), f"{entry.get('name', '')} [{entry.get('rarity', '')}]"
        if outcome is None:
            if time.time() - (entry.get("end_at") or 0) > STALE_AFTER:
                self.say(f"?         {name} : aucun résultat depuis 2 jours, suivi abandonné")
                purchases.set_status(aid, "gone")
                purchases.save()
                self.results.pop(aid, None)
            return
        self.results.pop(aid, None)
        final = auction.get("final_price") or auction.get("current_bid")
        if outcome == "sold" and auction.get("winner_id") == self.client.user_id:
            self.say(f"ACHETÉE   {name} pour {final} wikibidous (étiquette « {self.buy['tag']} » au prochain tri)")
            self.ctx.journal.write("achat", entry.get("name", ""), entry.get("rarity", ""), "", entry.get("card_id", ""),
                                   entry.get("value"), price=final, auction_id=aid)
            status = "won"
        else:
            self.say(f"perdue    {name}" + (f" : adjugée {final} à un autre joueur" if outcome == "sold" else ""))
            self.ctx.journal.write("perdue", entry.get("name", ""), entry.get("rarity", ""), "",
                                   entry.get("card_id", ""), entry.get("value"), price=final, auction_id=aid,
                                   result=f"notre dernière mise : {entry.get('price')}")
            status = "lost"
        self.ctx.summary[status] += 1
        purchases.set_status(aid, status, final_price=final)
        purchases.save()


def plan_bought(ctx, cards, all_cards):
    """Cartes gagnées aux enchères : on leur pose buy.tag, une étiquette de protection, avant tout le reste."""
    tag, steps = ctx.cfg["buy"]["tag"], []
    for aid, entry in ctx.purchases.with_status("won"):
        card_id = entry.get("card_id")
        if not any(c.card_id == card_id for c in all_cards):
            if time.time() - (entry.get("end_at") or 0) > STALE_AFTER:
                ctx.purchases.set_status(aid, "gone")
            else:
                print(f"  {entry.get('name', '')} : pas encore dans la collection, étiquette au prochain passage")
            continue
        steps += [(c, "add_tag", tag, entry.get("value")) for c in cards
                  if c.card_id == card_id and not c.has_any_tag([tag])]
    return steps


def bought_tagged(ctx, cards, all_cards):
    """Après plan_bought : les achats dont la carte porte maintenant buy.tag sont terminés."""
    tag = ctx.cfg["buy"]["tag"]
    for aid, entry in ctx.purchases.with_status("won"):
        card_id = entry.get("card_id")
        # Une ligne à plusieurs exemplaires (absente de cards) n'est jamais touchée : rien de plus à faire.
        if any(c.card_id == card_id for c in all_cards) \
                and all(c.has_any_tag([tag]) for c in cards if c.card_id == card_id):
            ctx.purchases.set_status(aid, "tagged")
    if ctx.execute:
        ctx.purchases.save()


def run_command(ctx, command):
    ctx.run_started = time.time()
    if command == "boosters" or (command == "tout" and ctx.cfg["packs"]["in_tout"]):
        print("\n== Boosters ==")
        open_packs(ctx)
    if command == "acheter":
        print("\n== Achats aux enchères ==")
        ctx.sniper.next_scan = 0  # relecture du marché tout de suite
        ctx.sniper.wait(0)
        if not ctx.sniper.targets and not ctx.sniper.results:
            print(f"  aucune enchère à suivre pour l'instant ({', '.join(ctx.cfg['buy']['keywords'])})")
        elif ctx.sniper.one_shot:
            print(f"  {len(ctx.sniper.targets)} enchère(s) suivie(s) : attente de leur fin (Ctrl+C pour quitter)…")
            ctx.sniper.drain()
    if command in ("boosters", "acheter"):
        return
    cards, warning = ctx.client.list_cards()
    ctx.collection_ids = {c.copy_id for c in cards}
    ctx.on_sale = set()  # relu par le bilan à chaque passage (sinon --loop ne relancerait jamais une carte)
    print(f"{len(cards)} exemplaire(s) dans la collection.")
    if warning:
        print(f"Attention : {warning}")
    # Une ligne ×2, ×3… : on ne sait pas à quel exemplaire s'appliquent étiquettes et défausse, on n'y touche pas.
    all_cards = cards
    grouped = [c for c in cards if c.count > 1]
    if grouped:
        print(f"  {len(grouped)} ligne(s) à plusieurs exemplaires laissée(s) de côté (le script n'y touche pas).")
        if ctx.verbose:
            for card in grouped:
                print(f"  groupée   {card.name} [{card.rarity}] ×{card.count}")
        cards = [c for c in cards if c.count <= 1]

    runner = Runner(ctx)
    tag = ctx.cfg["buy"]["tag"]
    if tag and ctx.purchases.with_status("won"):
        print(f"\n== Cartes achetées : étiquette « {tag} » ==")
        steps = plan_bought(ctx, cards, all_cards)
        if not steps:
            print("  rien à faire")
        elif not runner.run(steps):
            return
        bought_tagged(ctx, cards, all_cards)
        cards = [c for c in cards if c.copy_id not in runner.failed]  # sans sa protection, on n'y touche pas
    phases = {
        "analyser": [("Analyse des prix et étiquettes", plan_analyse)],
        "vendre": [("Ventes aux enchères", plan_sell)],
        "defausser": [("Défausses", plan_discard)],
        "tout": [("Analyse des prix et étiquettes", plan_analyse), ("Ventes aux enchères", plan_sell),
                 ("Défausses", plan_discard)],
    }[command]
    try:
        for n, (title, plan) in enumerate(phases):
            print(f"\n== {title} ==")
            batches = plan(ctx, cards)
            if isinstance(batches, list):  # sinon : générateur de lots (défausses, prix relus au fur et à mesure)
                batches = [batches]
            acted = False
            for steps in batches:
                acted = acted or bool(steps)
                if steps and not runner.run(steps):
                    skipped = [t for t, _ in phases[n + 1:]]
                    if skipped:
                        print(f"  Étape(s) non lancée(s) pour cette raison : {', '.join(skipped)}.")
                    return
            if not acted:
                print("  rien à faire")
            cards = [c for c in cards if c.copy_id not in runner.consumed and c.copy_id not in runner.failed]
    finally:
        ctx.cache.save()  # même en dry-run : un --execute juste après ne relit pas tous les prix
        if ctx.execute:
            ctx.state.save()


LOGIN_STEPS = """Ajout d'un compte (à refaire seulement si le script affiche « session révoquée »).
  1. Ouvrez une fenêtre de NAVIGATION PRIVÉE et connectez-vous sur https://www.wiki-masters.com.
  2. Outils développeur (Cmd+Option+I sur Mac, F12 sur Windows) > onglet Network (Réseau).
  3. Allez sur la page Collection, tapez my-collection dans le filtre, puis rechargez la page.
  4. Cliquez sur la requête my-collection (GET, www.wiki-masters.com).
  5. Dans Request Headers, clic droit sur « cookie » > Copy value.
  6. Collez ci-dessous puis Entrée (rien ne s'affiche, c'est normal).
  7. Fermez ensuite la fenêtre privée SANS vous déconnecter.
"""


def account_name(claims):
    """Nom du compte d'après le jeton : pseudo du site, sinon e-mail, sinon début de l'identifiant."""
    name = (claims.get("user_metadata") or {}).get("username") or claims.get("email") or claims["sub"][:8]
    return str(name)


def account_dir(cfg_file, name):
    """Dossier du compte : session, suivi des ventes et verrou (le nom est nettoyé pour le système de fichiers)."""
    safe = re.sub(r"[^\w.@-]", "_", name).strip("._") or "compte"
    return config_path(cfg_file, ACCOUNTS_DIR) / safe


def known_accounts(cfg_file):
    root = config_path(cfg_file, ACCOUNTS_DIR)
    if not root.is_dir():
        return []
    return sorted((p.name for p in root.iterdir() if (p / "session.json").is_file()), key=str.casefold)


def choose_account(cfg_file, wanted):
    """Le compte demandé par --compte, ou le seul compte enregistré."""
    accounts = known_accounts(cfg_file)
    if wanted:
        found = [a for a in accounts if a.casefold() == wanted.casefold()]
        if not found:
            listed = ", ".join(accounts) or "aucun"
            sys.exit(f"Compte « {wanted} » inconnu (comptes enregistrés : {listed}). "
                     f"Pour l'ajouter : « {cmd('login')} ».")
        return found[0]
    if not accounts:
        sys.exit(f"Aucun compte enregistré : lancez d'abord « {cmd('login')} » (voir README).")
    if len(accounts) > 1:
        sys.exit(f"Plusieurs comptes enregistrés ({', '.join(accounts)}) : précisez lequel, "
                 f"ex. « {cmd('tout --compte ' + accounts[0])} ».")
    return accounts[0]


def account_user_ids(cfg_file):
    """Identifiant sur le site de chaque compte enregistré (id -> nom), d'après sa session."""
    ids = {}
    for name in known_accounts(cfg_file):
        try:
            data = json.loads((account_dir(cfg_file, name) / "session.json").read_text(encoding="utf-8"))
            ids[jwt_claims(data["session"]["access_token"])["sub"]] = name
        except (OSError, ValueError, KeyError, TypeError):
            continue  # session illisible : ce compte se signalera lui-même à son prochain lancement
    return ids


def cmd_login(cfg, cfg_file):
    print(LOGIN_STEPS)
    raw = read_secret("Cookie : ")
    ref = urlparse(cfg["site"]["supabase_url"]).netloc.split(".", 1)[0]
    try:
        session = supabase_session(parse_cookie_header(normalize_cookie_header(raw)), ref)
        claims = jwt_claims(session["access_token"])
    except ValueError as e:
        sys.exit(f"Cookie invalide : {e}. Recommencez en copiant toute la valeur de l'en-tête cookie.")
    folder = account_dir(cfg_file, account_name(claims))
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        sys.exit(f"Impossible de créer {folder} ({type(e).__name__}).")
    store = SessionStore(folder / "session.json")
    client = WikiMastersClient(cfg["site"], store, session=session)
    try:
        client.store.save(client.session)
    except SessionSaveError as e:
        sys.exit(f"Session non enregistrée : {e}.")
    try:
        slots = client.auction_slots()  # vérifie que le site accepte la session (et la renouvelle si besoin)
    except ApiError as e:
        sys.exit(f"Session enregistrée, mais le site la refuse : {e}")
    if client.session_lost:
        sys.exit(f"Session refusée : {client.session_lost}")
    print(f"Compte {client.username} enregistré ({slots} place(s) d'enchère libre(s)).")
    others = [a for a in known_accounts(cfg_file) if a != folder.name]
    option = f" --compte {folder.name}" if others else ""
    print(f"Simulation (rien n'est modifié) : {cmd('tout' + option)}")
    print(f"Pour de vrai :                   {cmd('tout' + option + ' --execute')}")


def cmd_accounts(cfg_file):
    root = config_path(cfg_file, ACCOUNTS_DIR)
    folders = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name.casefold()) \
        if root.is_dir() else []
    if not folders:
        print(f"Aucun compte enregistré : lancez « {cmd('login')} ».")
        return
    print("Comptes :")
    for folder in folders:
        state = SalesState(folder / "ventes.json")
        session = "" if (folder / "session.json").is_file() else f"   (pas de session : « {cmd('login')} »)"
        print(f"  {folder.name:<24} {len(state.with_status('open'))} enchère(s) suivie(s){session}")
    if len(known_accounts(cfg_file)) > 1:
        print(f"Choisissez avec --compte, ex. « {cmd('tout --compte ' + known_accounts(cfg_file)[0])} ».")


def keep_awake():
    """Empêche la mise en veille pendant le passage : une veille coupe le réseau et arrête le script."""
    if sys.platform == "darwin" and shutil.which("caffeinate"):
        try:  # caffeinate s'arrête tout seul à la fin de ce processus
            subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass
    elif os.name == "nt":
        try:
            import ctypes

            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)  # ES_CONTINUOUS | SYSTEM_REQUIRED
        except (AttributeError, OSError):
            pass


def telegram_settings(cfg):
    """Réglages Telegram (jeton dans perso.yaml), ou None si les notifications ne sont pas configurées."""
    tg = cfg.get("telegram") or {}
    token, chat_id = tg.get("bot_token"), tg.get("chat_id")
    if not token and not chat_id:
        return None
    if not isinstance(token, str) or not token.strip() or not str(chat_id or "").strip():
        print(f"Attention : il faut telegram.bot_token ET telegram.chat_id dans {PERSO_FILE} : pas de notification.")
        return None
    return {"bot_token": token.strip(), "chat_id": str(chat_id).strip(),
            "notify_on_dry_run": tg.get("notify_on_dry_run", False)}


def telegram_message(username, command, outcome, summary_text):
    """Message HTML. Tout ce qui vient du site ou d'une erreur est échappé, sinon Telegram refuse le message."""
    status, detail = outcome
    header = f"<b>[WikiMasters]</b> {html.escape(username)} — <code>{html.escape(command)}</code>"
    line = {
        "ok": "✅ <b>Bilan</b>",
        "error": f"⚠️ <b>Arrêt :</b> {html.escape(detail or '')}",
        "interrupted": "⏹ <b>Interrompu</b> (Ctrl+C) : bilan partiel",
        "crash": f"💥 <b>Plantage</b> ({html.escape(detail or '')}) : voir le terminal, bilan partiel",
    }[status]
    return f"{header}\n{line}\n{html.escape(summary_text)}"


def send_telegram(settings, message):
    """Envoie la notification. Un échec ne fait jamais échouer le passage."""
    url = f"https://api.telegram.org/bot{settings['bot_token']}/sendMessage"
    try:
        resp = requests.post(url, json={"chat_id": settings["chat_id"], "text": message, "parse_mode": "HTML"},
                             timeout=10)
    except requests.RequestException as e:
        print(f"Telegram : message non envoyé ({type(e).__name__})")  # jamais str(e) : l'URL contient le jeton
        return
    if not resp.ok:
        print(f"Telegram : message refusé (erreur {resp.status_code})")


def loop_minutes(text):
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("nombre entier de minutes attendu") from None
    if value < 1:
        raise argparse.ArgumentTypeError("au moins 1 minute")
    return value


def main():
    parser = argparse.ArgumentParser(prog=CMD, description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=COMMANDS + ("login", "comptes"), help="ce que le script doit faire")
    parser.add_argument("--execute", action="store_true", help="agir pour de vrai (sinon : simulation)")
    parser.add_argument("--loop", type=loop_minutes, nargs="?", const=15, metavar="MINUTES",
                        help="recommencer toutes les N minutes (15 si rien n'est précisé), jusqu'à Ctrl+C")
    parser.add_argument("--compte", help="compte à utiliser, s'il y en a plusieurs (voir « comptes »)")
    parser.add_argument("--fresh", action="store_true",
                        help="relire tous les prix sur le site au lieu du cache (pour actualiser les étiquettes)")
    parser.add_argument("--verbose", action="store_true", help="tout afficher (cartes protégées, en attente…)")
    parser.add_argument("--config", default="config.yaml", help="fichier de règles (par défaut config.yaml)")
    args = parser.parse_args()

    cfg = load_config(args.config)
    check_config(cfg)
    if args.command == "login":
        try:
            cmd_login(cfg, args.config)
        except KeyboardInterrupt:
            sys.exit("\nConnexion annulée.")
        return
    if args.command == "comptes":
        cmd_accounts(args.config)
        return

    folder = account_dir(args.config, choose_account(args.config, args.compte))
    client = WikiMastersClient(cfg["site"], SessionStore(folder / "session.json"),
                               page_delay=cfg["safety"]["read_delay_seconds"])
    telegram = telegram_settings(cfg)
    journal_cfg = cfg["journal"]
    ctx = Context(
        client=client,
        cfg=cfg,
        execute=args.execute,
        summary=new_summary(),
        cache=PriceCache(config_path(args.config, cfg["price"]["cache_file"]), cfg["price"]["cache_hours"]),
        verbose=args.verbose or cfg.get("display", {}).get("verbose", False),
        # Le journal n'enregistre que les actions réelles : rien en simulation.
        journal=Journal(config_path(args.config, journal_cfg["file"]), journal_cfg["delimiter"],
                        journal_cfg["enabled"] and args.execute, client.username, args.command),
        state=SalesState(folder / "ventes.json"),
        purchases=PurchaseState(folder / "achats.json"),
    )
    keywords = cfg["buy"]["keywords"]
    if args.command == "acheter" and not keywords:
        sys.exit(f"buy.keywords est vide : rien à chercher sur le marché. Ajoutez vos mots-clés dans {PERSO_FILE}, "
                 "ex. buy: { keywords: [\"lyon\"], tag: \"lyon\" } (voir README).")
    if keywords and (args.command == "acheter" or (args.command == "tout" and args.loop)):
        ctx.sniper = Sniper(ctx, one_shot=not args.loop, mine=account_user_ids(args.config))
        client.waiter = ctx.sniper.wait
    mode = "EXÉCUTION RÉELLE" if args.execute else "SIMULATION (rien n'est modifié, ajoutez --execute pour agir)"
    lock = RunLock(folder / ".wikimasters.lock")
    if args.execute:
        lock.acquire()
    keep_awake()

    outcome, cycle = ("ok", None), 0
    try:
        while True:
            # Un bilan par cycle de --loop.
            cycle += 1
            ctx.journal.merge_backup()
            print(f"\n=== [{time.strftime('%H:%M:%S')}] {args.command} — {mode} — compte {client.username} ===")
            if keywords and args.command == "tout" and not args.loop and cycle == 1:
                print("Achats aux enchères (buy.keywords) : seulement avec --loop, ou avec la commande « acheter ».")
            if args.fresh and cycle == 1:  # les cycles suivants de --loop réutilisent ces prix
                ctx.cache.not_before = time.time()
                print("Option --fresh : tous les prix sont relus sur le site (cache ignoré).")
            outcome = ("ok", None)
            try:
                run_command(ctx, args.command)
            except ApiError as e:
                outcome = ("error", str(e))
                print(f"\nARRÊT : {e}")
                if args.loop:
                    print("Le prochain cycle reprendra là où ce passage s'est arrêté (prix déjà lus gardés).")
                elif cfg["price"]["cache_hours"] > 0:
                    again = [args.command] + (["--compte", args.compte] if args.compte else []) \
                        + (["--execute"] if args.execute else []) \
                        + (["--config", args.config] if args.config != "config.yaml" else [])
                    print(f"Les prix déjà lus sont gardés : pour reprendre, relancez « {cmd(' '.join(again))} »"
                          f"{' (sans --fresh)' if args.fresh else ''}. Avec --loop, le script reprend tout seul.")
            except KeyboardInterrupt:
                outcome = ("interrupted", None)
                raise
            except Exception as e:
                outcome = ("crash", type(e).__name__)
                raise
            finally:
                summary_text = print_summary(ctx.summary, args.execute)
                if ctx.journal.lost:
                    print(f"Attention : {ctx.journal.lost} ligne(s) non écrite(s) dans le journal ({journal_cfg['file']}).")
                if ctx.journal.diverted:
                    print(f"Attention : {ctx.journal.diverted} ligne(s) écrite(s) dans {ctx.journal.backup.name} "
                          f"({journal_cfg['file']} verrouillé), recopiée(s) au prochain passage.")
                if telegram and (args.execute or telegram["notify_on_dry_run"]):
                    send_telegram(telegram, telegram_message(client.username, args.command, outcome, summary_text))
                # Nouveau bilan dès maintenant : les mises faites pendant la pause de --loop iront dans le suivant.
                ctx.summary = new_summary()
                ctx.journal.lost = ctx.journal.diverted = 0

            if client.session_lost:
                if client.session_lost != outcome[1]:  # sinon déjà affiché par « ARRÊT »
                    print(f"\nAttention : {client.session_lost}")
                sys.exit(1)
            if outcome[0] == "error" and not args.loop:
                sys.exit(1)
            if not args.loop:
                break
            print(f"\nProchain passage dans {args.loop} minute(s)… (Ctrl+C pour quitter)")
            try:
                (ctx.sniper.wait if ctx.sniper else time.sleep)(args.loop * 60)
            except ApiError as e:  # session perdue pendant les achats (les autres erreurs n'arrêtent que la mise)
                print(f"\nARRÊT : {e}")
                print_summary(ctx.summary, args.execute)
                sys.exit(1)
    except KeyboardInterrupt:
        print("\nArrêt demandé par l'utilisateur.")
        if outcome[0] == "interrupted":
            sys.exit(1)  # passage coupé en cours de route (un Ctrl+C pendant la pause de --loop est un arrêt normal)
    finally:
        lock.release()


def print_summary(summary, execute):
    verb = "effectuée(s)" if execute else "prévue(s)"
    auctions = f"{summary['auction']} enchère(s)"
    if summary["relist"]:
        auctions += f" dont {summary['relist']} relance(s)"
    parts = [
        auctions,
        f"{summary['discard']} défausse(s)",
        f"{summary['tag_add']} étiquette(s) posée(s)",
        f"{summary['tag_remove']} retirée(s)",
    ]
    if summary["pack"]:
        parts.append(f"{summary['pack']} booster(s) ouvert(s)")
    if summary["bid"]:
        parts.append(f"{summary['bid']} mise(s) sur le marché")
    others = [
        f"{len(summary['protect'])} protégée(s)",
        f"{len(summary['skip'] - summary['protect'])} ignorée(s)",
        f"{summary['no_slot']} en attente d'une place d'enchère",
        f"{summary['error']} échec(s)",
    ]
    if summary["sold"] or summary["unsold"]:
        others.insert(0, f"ventes passées : {summary['sold']} vendue(s), {summary['unsold']} invendue(s)")
    if summary["won"] or summary["lost"]:
        others.insert(0, f"achats : {summary['won']} gagné(s), {summary['lost']} perdu(s)")
    if summary["up_to_date"]:
        others.insert(0, f"{len(summary['up_to_date'])} déjà à jour")
    if summary["read_error"]:
        others.append(f"{len(summary['read_error'])} prix illisible(s)")
    if summary["deferred"]:
        others.append(f"{summary['deferred']} action(s) reportée(s) au prochain passage")
    for group, until in summary["paused"].items():
        others.append(f"{group} en pause jusqu'à {clock_time(until)} (vérification anti-robot demandée par le site)")
    summary_text = f"Résumé ({verb}) : {', '.join(parts)} | {', '.join(others)}"
    print(f"\n{summary_text}")
    return summary_text


if __name__ == "__main__":
    main()
