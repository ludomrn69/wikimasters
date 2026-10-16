#!/usr/bin/env python3
"""Bot WikiMasters : trie les cartes de votre collection selon config.yaml (et vos réglages de perso.yaml).

    ./wm login        ajoute un compte (cookie du navigateur, voir README)
    ./wm tout         vendre et défausser les cartes déjà étiquetées, analyser, puis traiter les nouvelles
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
import traceback
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
# Requêtes des achats : quelques secondes de nouvelles tentatives au plus. Une mise ne peut pas attendre, et une
# lecture ratée (marché, résultat) est simplement refaite au tour suivant.
QUICK_RETRY_DELAYS = (1, 3)
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
# Ventes, mises et boosters sont bloqués ensemble : la vérification vaut pour tout le marché du compte (vu les 15 et
# 16/10/2026 : après un blocage des mises, la mise en vente suivante l'est aussi, et l'inverse). Les défausses non.
MARKET_GROUP = "ventes, achats et boosters"
ANTIBOT_GROUPS = {"auction": MARKET_GROUP, "pack": MARKET_GROUP, "bid": MARKET_GROUP, "discard": "défausses",
                  "add_tag": "étiquettes", "remove_tag": "étiquettes"}
# Nouvelles tentatives du renouvellement de session (réseau, 429, 5xx). Rapides : si la première demande était
# arrivée, Supabase accepte encore l'ancien jeton pendant quelques secondes, au-delà il révoquerait la session.
REFRESH_RETRY_DELAYS = (1, 3)
MAYBE_DONE = "L'action a peut-être été effectuée : vérifiez sur le site."
COMMANDS = ("tout", "analyser", "vendre", "defausser", "boosters", "acheter")
# Achats : une annonce qui finit dans moins de ce nombre de secondes n'est plus suivie (trop tard pour miser). La
# dernière vérification, pour répondre à une surenchère, a lieu à buy.last_check_seconds de la fin.
LAST_CHECK_SECONDS = 6
# buy.bid_on_sight : les mises sur les annonces repérées à la même relecture partent espacées d'au moins ce nombre de
# secondes (des mises enchaînées déclenchent la vérification anti-robot du site).
SIGHT_SPACING = 15
# buy.max_bids : le site bloque le marché du compte après une dizaine de mises rapprochées (vu les 15 et 16/10/2026 :
# 10 mises acceptées, la 11e refusée, 6 fois sur 8). Ces dernières places sont gardées pour répondre à une surenchère
# sur une enchère où le script a déjà misé, plutôt que de la perdre pour une nouvelle.
BID_RESERVE = 2
# Mise refusée parce qu'un autre joueur vient de miser (409 « Mise trop basse », avec le nouveau minimum) : ce minimum
# est remisé aussitôt, toujours sous le plafond, au plus ce nombre de fois de suite.
TOO_LOW_RETRIES = 2
# Une mise tardive fait prolonger l'enchère par le site (+60 s, vu le 10/10/2026) : l'enchère suivie est revue
# ce nombre de secondes après sa fin prévue ; prolongée, tout recommence (mise, vérification) avant la nouvelle fin.
AFTER_END_SECONDS = 3
# Aucune autre requête ne part quand une vérification d'enchère tombe dans les N secondes : le site met parfois
# plusieurs secondes à répondre, et la mise arriverait trop tard.
CHECK_MARGIN = 10
# Résultat d'une enchère où le script a misé : lu une minute après sa fin, puis toutes les 5 minutes.
RESULT_DELAY, RESULT_RETRY = 60, 300
# Enchère retirée du site avant la lecture de son résultat (404) : déduit de la collection lue au moins ce nombre de
# secondes après sa fin (le temps que le site rende la carte, au vendeur ou au gagnant).
SETTLE_GRACE = 120
# Pages du marché lues au plus par mot-clé et par relecture (triées par fin la plus proche).
MAX_SCAN_PAGES = 5
# Réglages personnels (et jeton Telegram) par-dessus config.yaml, à côté de lui et jamais envoyés sur GitHub.
PERSO_FILE = "perso.yaml"
# Un dossier par compte : session, suivi des ventes, verrou.
ACCOUNTS_DIR = "comptes"
# Dans ACCOUNTS_DIR : enchère du marché -> compte qui la suit, pour que vos comptes ne misent pas l'un contre l'autre.
CLAIMS_FILE = "achats_en_cours.json"
# Commande affichée dans les messages : le lanceur (./wm ou wm) la précise.
CMD = os.environ.get("WM_CMD") or "python wikimasters.py"


def cmd(args):
    return f"{CMD} {args}"


def as_dict(data):
    """Réponse du site attendue sous forme de dictionnaire : {} si elle est vide ou d'une autre forme."""
    return data if isinstance(data, dict) else {}


class ApiError(Exception):
    """Erreur d'API. fatal=True arrête le passage, sinon seule l'action en cours est abandonnée."""

    def __init__(self, message, fatal=False, status=None, code=None, body=None):
        super().__init__(message)
        self.fatal = fatal
        self.status = status
        self.code = code  # code d'erreur renvoyé par le site ou Supabase (ex. "23505")
        self.body = as_dict(body)  # réponse JSON du site (ex. {"code": "bid_too_low", "min": 43})

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


CONDITION_KEYS = {"name_contains", "name_words", "rarity_in", "shiny", "min_value", "max_value"}


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
        for key in ("name_contains", "name_words"):
            if key in cond and not isinstance(cond[key], str):
                str_list(cond, where, key, allow_empty=False)  # ou une liste : l'un de ces textes
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
                               "require_existing_tag", "unsold_max_value"))
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
    number(disc, "discard", "unsold_max_value", 0, allow_none=True)
    boolean(disc, "discard", "fresh_price")
    boolean(disc, "discard", "require_existing_tag")

    sell = section("sell", ("tags", "order", "price_factor", "rounding", "min_start_price", "max_start_price",
                            "duration_minutes", "long_duration", "max_auctions", "min_value", "fresh_price",
                            "require_existing_tag",
                            "relist", "night"))
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
    long_duration = sell.get("long_duration")
    if long_duration is not None and not isinstance(long_duration, dict):
        err("sell.long_duration doit valoir { from_value: 100, minutes: 30 } (ou null)")
    elif long_duration is not None:
        unknown_keys(long_duration, "sell.long_duration", ("from_value", "minutes"))
        number(long_duration, "sell.long_duration", "from_value", 0)
        choice(long_duration, "sell.long_duration", "minutes", sorted(KNOWN_DURATIONS))
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
    night = sell.get("night")
    if night is not None and not isinstance(night, dict):
        err("sell.night doit valoir { start: \"23:00\", end: \"08:00\" } (ou null)")
    elif night is not None:
        unknown_keys(night, "sell.night", ("start", "end"))
        for key in ("start", "end"):
            if clock_minutes(night.get(key)) is None:
                err(f"sell.night.{key} doit être une heure entre guillemets, ex. \"23:00\"")
        if clock_minutes(night.get("start")) is not None \
                and clock_minutes(night.get("start")) == clock_minutes(night.get("end")):
            err("sell.night : start et end doivent être différents")

    packs = section("packs", ("in_tout", "max_per_run", "delay_seconds"))
    boolean(packs, "packs", "in_tout")
    number(packs, "packs", "max_per_run", 0, integer=True)
    number(packs, "packs", "delay_seconds", 0)

    buy = section("buy", ("keywords", "tag", "keyword_tags", "names", "names_tags", "names_minutes", "price_factor",
                          "max_price", "buy_unknown", "skip_owned", "snipe_seconds", "last_check_seconds",
                          "bid_on_sight", "max_bids", "exclude", "scan_minutes", "accounts", "by_account"))
    str_list(buy, "buy", "exclude")
    max_bids = buy.get("max_bids")
    if max_bids is not None and not isinstance(max_bids, dict):
        err("buy.max_bids doit valoir { count: 8, minutes: 30 } (ou null)")
    elif max_bids is not None:
        unknown_keys(max_bids, "buy.max_bids", ("count", "minutes"))
        number(max_bids, "buy.max_bids", "count", BID_RESERVE + 1, integer=True)
        number(max_bids, "buy.max_bids", "minutes", 1)
    by_account = buy.get("by_account")
    if by_account is not None and not isinstance(by_account, dict):
        err("buy.by_account doit valoir { compte: { keywords: […], names: […] }, … } (ou null)")
    for account, over in (by_account or {}).items() if isinstance(by_account, dict) else ():
        if not isinstance(over, dict):
            err(f"buy.by_account.{account} doit valoir {{ keywords: […], names: […] }}")
        else:  # chaque réglage est vérifié avec les autres, une fois le compte choisi (account_buy)
            unknown_keys(over, f"buy.by_account.{account}", BY_ACCOUNT_KEYS)
    keywords = str_list(buy, "buy", "keywords")
    names = str_list(buy, "buy", "names")
    number(buy, "buy", "names_minutes", 1)
    if buy.get("accounts") is not None:
        str_list(buy, "buy", "accounts", allow_empty=False)
    number(buy, "buy", "price_factor", 0, strict=True, allow_none=True)
    number(buy, "buy", "max_price", 1, integer=True, allow_none=True)
    boolean(buy, "buy", "buy_unknown")
    boolean(buy, "buy", "skip_owned")
    number(buy, "buy", "snipe_seconds", 10)
    number(buy, "buy", "last_check_seconds", 3)
    if _is_number(buy.get("snipe_seconds")) and _is_number(buy.get("last_check_seconds")) \
            and buy["last_check_seconds"] >= buy["snipe_seconds"]:
        err("buy.last_check_seconds doit être plus petit que buy.snipe_seconds (la vérification suit la mise)")
    boolean(buy, "buy", "bid_on_sight")
    number(buy, "buy", "scan_minutes", 1)
    if buy.get("tag") is not None and (not isinstance(buy["tag"], str) or not buy["tag"].strip()):
        err("buy.tag doit être un texte (ou null)")
    elif (keywords or names) and norm(buy.get("tag") or "") not in {norm(t) for t in prot_tags}:
        # Sans elle, « analyser » mettrait en vente ou défausserait les cartes que le script vient d'acheter.
        err("buy.tag doit être une étiquette de protection.tags (ex. « garder ») : elle protège les cartes achetées")
    protecting = {norm(t) for t in prot_tags}

    def bought_tags_ok(value, where):
        tags = text_list(value) if isinstance(value, (str, list)) else None
        if not tags or not all(isinstance(t, str) and norm(t) in protecting for t in tags):
            err(f"{where} doit être une étiquette de protection.tags, ou une liste d'étiquettes de protection.tags : "
                "elles protègent les cartes achetées")

    keyword_tags = buy.get("keyword_tags")
    if not isinstance(keyword_tags, dict):
        err("buy.keyword_tags doit valoir { texte: étiquette(s), … } (ou {}), ex. { pnl: \"pnl\" }")
    else:
        for text, tags in keyword_tags.items():
            bought_tags_ok(tags, f"buy.keyword_tags.{text}")
    if buy.get("names_tags") is not None:
        bought_tags_ok(buy["names_tags"], "buy.names_tags")

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
        unknown_keys(telegram, "telegram", ("notify_on_dry_run", "every_pass", "daily_report", "bot_token", "chat_id"))
        for key in ("notify_on_dry_run", "every_pass"):
            if key in telegram:
                boolean(telegram, "telegram", key)
        if telegram.get("daily_report") is not None and clock_minutes(telegram["daily_report"]) is None:
            err("telegram.daily_report doit être une heure entre guillemets, ex. \"22:00\" (ou null)")
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

    def peek(self):
        """Session enregistrée, ou None si le fichier est absent ou illisible (sans arrêter le script)."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return as_dict(data).get("session")

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


def text_list(value):
    """Un texte ou une liste de textes (réglages qui acceptent les deux), en liste."""
    return [value] if isinstance(value, str) else list(value or [])


def found_in(key, text, whole=False):
    """key est-il dans text (déjà normalisé), en début de mot ? « lyon » trouve « lyonnais » ou « To-Lyon », pas
    « Elyon ». whole : en mots entiers (un nom : « Lucas Perri » ne trouve pas « Lucas Perrin »)."""
    return re.search(r"(?<!\w)" + re.escape(norm(key)) + (r"(?!\w)" if whole else ""), text) is not None


def matches(card, value, cond):
    if "name_contains" in cond and not any(norm(n) in norm(card.name) for n in text_list(cond["name_contains"])):
        return False
    if "name_words" in cond and not any(found_in(n, norm(card.name), whole=True) for n in text_list(cond["name_words"])):
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


def auto_protection(card, value, cfg):
    """Étiquette de protection que « analyser » poserait sur cette carte (auto_tags), sinon None.

    « tout » vend et défausse les cartes déjà étiquetées avant l'analyse : une règle qui va les protéger compte déjà.
    """
    protecting = {norm(t) for t in cfg["protection"]["tags"]}
    for rule in cfg.get("auto_tags", []):
        if norm(rule["tag"]) in protecting and matches(card, value, rule.get("when", {})):
            return rule["tag"]
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


def clock_minutes(text):
    """Heure de la config (ex. "23:00") en minutes depuis minuit, ou None si ce n'est pas une heure."""
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", text.strip()) if isinstance(text, str) else None
    return int(m[1]) * 60 + int(m[2]) if m else None


def at_night(timestamp, night):
    """Ce moment tombe-t-il dans la nuit de sell.night (heure de l'ordinateur) ? La nuit peut passer minuit."""
    if not night:
        return False
    t = time.localtime(timestamp)
    now, start, end = t.tm_hour * 60 + t.tm_min, clock_minutes(night["start"]), clock_minutes(night["end"])
    return start <= now < end if start < end else now >= start or now < end


# --loop : une place d'enchère libérée est remplie au passage suivant, lancé ce délai après la fin prévue de l'enchère
# (le site la règle d'abord), mais jamais moins de MIN_LOOP_SECONDS après la fin du passage.
SLOT_GRACE_SECONDS, MIN_LOOP_SECONDS = 60, 120


def next_free_slot(ctx):
    """Moment où une enchère lancée par le script libère sa place (fin prévue + SLOT_GRACE_SECONDS), ou None.
    Une enchère prolongée par une mise de dernière minute (fin prévue passée de peu) compte aussi : bientôt finie."""
    now = time.time()
    ends = [(e.get("listed_at") or 0) + 60 * (e.get("duration") or 0) + SLOT_GRACE_SECONDS
            for _, e in ctx.state.with_status("open")]
    ends = [t for t in ends if t > now - 300]
    return min(ends) if ends else None


def night_end(timestamp, night):
    """Prochaine fin de la nuit de sell.night (heure de l'ordinateur) après ce moment."""
    t, end = time.localtime(timestamp), clock_minutes(night["end"])
    wake = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, end // 60, end % 60, 0, 0, 0, -1))
    if wake <= timestamp:  # demain (mktime ajuste le jour, le mois et l'heure d'été)
        wake = time.mktime((t.tm_year, t.tm_mon, t.tm_mday + 1, end // 60, end % 60, 0, 0, 0, -1))
    return wake


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


def _json_body(resp):
    """Réponse JSON en dictionnaire, ou {} : on y lit le code d'erreur (ex. "42501" de Supabase, "bid_too_low")."""
    try:
        return as_dict(resp.json())
    except ValueError:
        return {}


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
        self.quick = False  # requêtes des achats en cours : nouvelles tentatives rapides seulement (QUICK_RETRY_DELAYS)
        self.last_uncertain = False  # la dernière écriture a dû être renvoyée après une tentative peut-être effectuée
        self.retried = 0  # requêtes renvoyées après une erreur passagère du site (résumé du passage)
        self._tag_ids = None
        self._last_refresh = None

        session = session or store.load()
        if not session:
            sys.exit(f"Aucune session enregistrée : lancez d'abord « {cmd('login')} » (voir README).")
        try:
            self._use_session(session, save=False)
        except (ValueError, KeyError, TypeError):
            sys.exit(f"{store.path} est invalide : relancez « {cmd('login')} ».")

    @staticmethod
    def _session_expiry(session, fresh=False):
        """(claims du jeton, moment d'expiration) d'une session. ValueError, KeyError ou TypeError si inutilisable."""
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
        return claims, min(deadlines)

    def _use_session(self, session, save=True, fresh=False):
        """Adopte une session. Elle est d'abord écrite sur le disque, puis seulement utilisée."""
        claims, expires_at = self._session_expiry(session, fresh)
        if save:
            self.store.save(session)  # SessionSaveError remonte : rien n'est adopté si l'écriture échoue
        self.session = session
        self.access_token = session["access_token"]
        self.user_id = claims["sub"]
        self.username = account_name(claims)
        self.expires_at = expires_at
        self.cookies = session_cookies(session, self.ref)

    def _adopt_saved_session(self):
        """Reprend la session de session.json si un autre passage de ce compte l'a renouvelée entre-temps.

        Une simulation lancée pendant un --execute renouvelle la session de son côté et l'enregistre. Renouveler
        ensuite l'ancienne, avec un jeton de renouvellement déjà utilisé, ferait révoquer la session par Supabase.
        """
        saved = self.store.peek()
        if not isinstance(saved, dict) or saved.get("refresh_token") == self.session.get("refresh_token"):
            return
        try:
            claims, expires_at = self._session_expiry(saved)
        except (ValueError, KeyError, TypeError):
            return  # illisible : on garde la nôtre
        if claims["sub"] == self.user_id and expires_at > self.expires_at:
            self._use_session(saved, save=False)

    def time_left(self):
        return self.expires_at - time.time()

    def refresh(self):
        """Renouvelle la session comme le fait le navigateur (jeton de renouvellement Supabase)."""
        self._adopt_saved_session()
        if self.time_left() >= REFRESH_MARGIN:
            return  # déjà renouvelée par un autre passage de ce compte
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
        Pendant les achats (self.quick), quelques secondes de nouvelles tentatives au plus.
        """
        what = f"{method} {urlparse(url).path}"
        if method == "GET":
            retry, delays = "idempotent", GET_RETRY_DELAYS
        else:
            delays = WRITE_RETRY_DELAYS if retry else ()
        if self.quick and delays:
            delays = QUICK_RETRY_DELAYS
        uncertain = False  # une tentative d'écriture a peut-être été effectuée par le serveur
        for attempt in range(len(delays) + 1):
            self.ensure_fresh()
            wait, resp = delays[attempt] if attempt < len(delays) else 0, None
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
                retry_after = self._retry_after(resp)
                if self.quick and retry_after > wait:
                    retryable = False  # le site demande d'attendre plus longtemps : les achats réessaieront plus tard
                wait = max(wait, min(retry_after, 300))
            self.slowdown = min(MAX_SLOWDOWN, self.slowdown * 2)
            if not retryable or attempt == len(delays):
                break
            self.retried += 1
            # Le site renvoie souvent une erreur 500 isolée (vu le 14/10/2026) que le 1er nouvel essai d'une lecture
            # corrige : pas affichée (comptée dans le résumé). Celle d'une action, une erreur qui se répète ou un 429
            # (trop rapide) le sont.
            if attempt or method != "GET" or resp is not None and resp.status_code == 429:
                print(f"     {problem} sur {what} : nouvel essai dans {wait:g} s…")
            self._retry_wait(wait)
        raise error

    def _retry_wait(self, seconds):
        """Attente avant de renvoyer une requête : les achats continuent de miser pendant ce temps (sans relire le
        marché), une panne du serveur pendant la lecture des prix ne doit pas faire manquer une fin d'enchère."""
        if self.waiter:
            self.waiter(seconds, scan=False)
        else:
            time.sleep(seconds)

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
        body = _json_body(resp) if is_json else {}
        return ApiError(f"erreur {resp.status_code} sur {what}{detail}{maybe_done}", status=resp.status_code,
                        code=body.get("code"), body=body)

    def _parse(self, resp, what, maybe_done):
        is_json = resp.headers.get("content-type", "").startswith("application/json")
        detail = f" : {resp.text[:200]}" if is_json and resp.text else ""
        body = _json_body(resp) if is_json else {}
        code = body.get("code")
        if 300 <= resp.status_code < 400:
            raise ApiError(f"redirection {resp.status_code} sur {what} (session refusée ?)", fatal=True)
        if resp.status_code == 403 and code == ANTIBOT_CODE:
            # Le site veut qu'un humain passe une vérification : surtout ne pas insister. Une lecture refusée arrête
            # le passage ; une action refusée met ce type d'action en pause (voir Runner).
            hint = f" {ANTIBOT_HINT}" if what.startswith("GET") else ""
            raise ApiError(f"le site demande une vérification anti-robot (« Vérification anti-bot requise ») sur "
                           f"{what}.{hint}", fatal=what.startswith("GET"), status=403, code=code)
        if resp.status_code == 403 and "packs_remaining" in body:
            # « Plus de paquets disponibles » (vu le 14/10/2026) : le compteur lu juste avant annonçait plus de
            # boosters que le site n'en accorde. Rien à voir avec la session : seuls les boosters s'arrêtent.
            raise ApiError(f"plus de booster disponible sur {what}{detail}", status=403, code="no_packs", body=body)
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
            raise ApiError(f"erreur {resp.status_code} sur {what}{detail}", status=resp.status_code, code=code,
                           body=body)
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
            items = data.get("collection", []) if isinstance(data, dict) else None
            if not isinstance(items, list) or not all(isinstance(i, dict) and "id" in i for i in items):
                raise ApiError("réponse inattendue sur /api/my-collection (le site a-t-il changé ?) : rien n'est fait.",
                               fatal=True)
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
        """Places d'enchère libres, ou None si le site ne les donne pas.

        Surchargé, le site répond parfois sans ces champs (vu le 14/10/2026, au milieu d'erreurs 500) : relu deux
        fois, puis None (les ventes attendent le passage suivant, le reste continue).
        """
        for wait in GET_RETRY_DELAYS[:2] + (None,):
            data = as_dict(self._api("GET", "/api/marketplace/mine"))
            most, selling = data.get("maxConcurrentAuctions"), data.get("sellingCount")
            if _is_int(most) and _is_int(selling):
                return most - selling
            if wait is not None:
                self.retried += 1
                self._retry_wait(wait)
        print(f"  places d'enchère illisibles sur /api/marketplace/mine (champs reçus : {', '.join(sorted(data)) or 'aucun'}"
              ") : le site est surchargé, ou a changé si ça dure")
        return None

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
            "date": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time())), "compte": self._text(self.account), "commande": self.command,
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

    def __init__(self, path, set_aside=True):
        """set_aside : un fichier illisible est mis de côté avant d'être remplacé. Sinon (simulation, « comptes »),
        il est seulement ignoré : rien n'est modifié sur le disque."""
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
            if not set_aside:
                print(f"Attention : {self.path.name} illisible, ignoré (il sera mis de côté au prochain --execute).")
                return
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
        """Clôt les anciennes entrées « invendue » d'un exemplaire qui vient d'être remis en vente (ou défaussé :
        « abandonnée » aussi, sinon follow_returned_copies passerait son suivi à un autre exemplaire de la carte)."""
        for aid, entry in self.with_status("unsold") + self.with_status("abandoned"):
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

    Statuts : bid (mise posée, résultat pas encore lu), vanished (enchère retirée du site avant la lecture du
    résultat : déduit de la collection, voir settle_vanished), won (gagnée : buy.tag reste à poser sur la carte) ;
    les autres (lost, tagged, gone) sont terminés et retirés du fichier à l'enregistrement.
    """

    KEPT = ("bid", "vanished", "won")
    WHAT = "suivi des achats"
    LOST = "le résultat des mises et l'étiquette des cartes gagnées seront incomplets"

    def record(self, target, amount):
        card = target.card
        self.auctions[target.auction_id] = {
            "card_id": card.card_id, "name": card.name, "rarity": card.rarity, "value": target.value,
            "price": amount, "end_at": target.end_at, "status": "bid", "tags": target.tags,
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


class BuyClaims:
    """comptes/achats_en_cours.json : quel compte suit chaque enchère du marché.

    Vos comptes lancés en même temps repèrent les mêmes annonces et miseraient tous à la même seconde, avant de voir
    qu'un autre est en tête. Le premier qui repère une annonce la réserve, les autres la laissent. Une réservation
    tient tant que le passage qui l'a prise tourne encore (au plus 1 h après la fin prévue de l'enchère).
    """

    KEEP_AFTER_END = 3600
    LOCK_WAIT = 5  # secondes d'attente au plus du fichier, que l'autre compte tient un instant
    STALE_LOCK = 30  # verrou plus vieux : laissé par un passage planté, on le reprend

    def __init__(self, path, account):
        self.path = pathlib.Path(path)
        self.lock = self.path.with_name(f".{self.path.stem}.lock")
        self.account = account

    def holder(self, auction_id, end_at, take):
        """L'autre compte qui suit déjà cette enchère, ou None. take : la réserver pour ce compte si elle est libre."""
        if not self._acquire():
            return None  # fichier inaccessible : chaque compte décide seul
        try:
            now = time.time()
            claims = {a: e for a, e in self._read().items() if self._alive(e, now)}
            other = (claims.get(auction_id) or {}).get("account")
            if other and other != self.account:
                return other
            if take:
                claims[auction_id] = {"account": self.account, "pid": os.getpid(), "end_at": end_at}
                self._write(claims)
            return None
        finally:
            try:
                os.unlink(self.lock)
            except OSError:
                pass

    def _alive(self, entry, now):
        return isinstance(entry, dict) and _is_number(entry.get("end_at")) \
            and now < entry["end_at"] + self.KEEP_AFTER_END and isinstance(entry.get("pid"), int) \
            and pid_alive(entry["pid"])

    def _acquire(self):
        deadline = time.time() + self.LOCK_WAIT
        while True:
            try:
                os.close(os.open(self.lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
                return True
            except FileExistsError:
                try:
                    if time.time() - self.lock.stat().st_mtime > self.STALE_LOCK:
                        os.unlink(self.lock)
                        continue
                except OSError:
                    continue  # verrou retiré entre-temps : on réessaie
            except OSError as e:
                print(f"Attention : {self.path.name} inaccessible ({type(e).__name__}) : "
                      "vos comptes peuvent miser sur la même enchère.")
                return False
            if time.time() >= deadline:
                return False
            time.sleep(0.05)

    def _read(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, claims):
        try:
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.stem}.", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(claims, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except OSError as e:
            print(f"Attention : {self.path.name} non enregistré ({type(e).__name__}) : "
                  "vos comptes peuvent miser sur la même enchère.")


@dataclass
class Offer:
    """Mise en vente prévue : mise de départ, numéro d'essai et enchère invendue qu'elle relance."""

    price: int
    attempt: int = 1
    relaunches: str | None = None
    duration: int | None = None  # minutes (None : sell.duration_minutes)


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
    collection: list = field(default_factory=list)  # ces mêmes exemplaires (Card)
    on_sale: set = field(default_factory=set)  # exemplaires dont l'enchère est confirmée en cours
    # Vérification anti-robot demandée par le site : groupe d'actions -> {"until": fin de la pause, "strikes": n}.
    # Gardé d'un cycle de --loop à l'autre ; relancer le script la lève (après une vérification faite à la main).
    antibot: dict = field(default_factory=dict)
    purchases: object = None  # PurchaseState : enchères du marché où le script a misé
    sniper: object = None  # Sniper, quand les achats sont actifs (« acheter », ou « tout --loop » avec buy.keywords)
    notify: object = None  # envoie aussitôt une alerte Telegram (texte), si les notifications sont configurées


# --- Commandes --------------------------------------------------------------


FAILED = object()
FINAL_ACTIONS = ("discard", "auction")


def new_summary():
    counters = ("tag_add", "tag_remove", "discard", "auction", "relist", "sold", "unsold", "no_slot", "error",
                "deferred", "pack", "bid", "won", "lost", "retried")
    # Ensembles d'exemplaires : une carte vue dans plusieurs phases de « tout » n'est comptée qu'une fois.
    sets = ("protect", "skip", "up_to_date", "read_error")
    # paused : groupe d'actions -> fin de sa pause anti-robot (voir antibot_strike) ; night : fin de la nuit sans
    # mise en vente (sell.night), si les ventes ont été sautées pour cette raison
    return {**{k: 0 for k in counters}, **{k: set() for k in sets}, "paused": {}, "night": None}


def crash_report(e):
    """Plantage (bug, ou réponse du site que le script ne sait pas lire) : quoi et où, pour pouvoir le corriger.
    Le message d'une erreur réseau n'est jamais affiché : il peut contenir les en-têtes, donc le cookie."""
    what = type(e).__name__ if isinstance(e, requests.RequestException) else f"{type(e).__name__}: {e}"
    return what + "\n" + "".join(traceback.format_tb(e.__traceback__)[-3:]).rstrip()


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
    if ctx.notify:
        # Le blocage ne se lève souvent pas tout seul (vu le 16/10/2026 : encore là au matin, 7 h plus tard).
        again = f" ({entry['strikes']}e fois de suite)" if entry["strikes"] > 1 else ""
        ctx.notify(f"🛑 {group.capitalize()} bloqués : le site demande une vérification anti-robot{again}. Il faut la "
                   f"passer à la main, souvent ça ne repart pas tout seul : sur wiki-masters.com avec ce compte, "
                   f"faites une action (mise ou mise en vente) et validez la vérification. Le script réessaie à "
                   f"{clock_time(entry['until'])} ; les défausses continuent.")


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
    targets, steps = [], []
    # Étiquettes automatiques qui ne dépendent pas du prix (nom, rareté…) : posées aussi sur les cartes protégées,
    # sans lire leur prix (ex. « OL » sur un joueur déjà protégé par « lyon »).
    priceless = [r for r in cfg.get("auto_tags", []) if not {"min_value", "max_value"} & set(r.get("when", {}))]
    for card in cards:
        reason = protection_reason(card, cfg["protection"])
        if reason and not pt["tag_protected"]:
            summary["protect"].add(card.copy_id)
            if ctx.verbose:
                print(f"  PROTÉGÉE  {card.name} ({reason})")
            have = {norm(t) for t in card.tags}
            for tag in dict.fromkeys(r["tag"] for r in priceless if matches(card, None, r.get("when", {}))):
                if norm(tag) not in have:
                    steps.append((card, "add_tag", tag, None))
            continue
        targets.append(card)
    values = read_values(ctx.client, targets, cfg, ctx.cache)
    count_read_errors(targets, values, summary)

    spread = {}
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


def filter_candidates(ctx, cards, tags_for, other_tags, what, require_existing=False, tagged=None):
    """Exemplaires portant une des étiquettes tags_for(carte), hors protections et étiquettes contradictoires.

    require_existing : l'étiquette devait déjà être sur le site au début du passage (pas posée à l'instant par
    « analyser » dans « tout ») : vous avez ainsi le temps de la vérifier avant une action irréversible.
    tagged : "before" = seulement les cartes qui portaient déjà l'étiquette au début du passage, "during" =
    seulement celles qui l'ont reçue pendant ce passage, None = toutes (voir TOUT_PHASES).
    """
    out, waiting = [], 0
    for card in cards:
        tags = tags_for(card)
        if not card.has_any_tag(tags):
            continue
        if tagged is not None and card.has_any_tag(tags, initial_only=True) != (tagged == "before"):
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


SOLD_OR_FINISHED = ("settled", "sold", "expired", "unsold", "completed", "finished", "closed", "settled_sold",
                    "settled_unsold")  # « settled_unsold » : invendue, vu sur le site le 14/10/2026
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
    taken = {e.get("copy_id") for e in ctx.state.auctions.values()}
    for auction_id, entry in opened:
        name, rarity = entry.get("name", ""), entry.get("rarity", "")
        auction, vanished = None, False
        try:
            data = ctx.client.get_auction(auction_id)
            auction = data.get("auction", data) if isinstance(data, dict) else None
        except ApiError as e:
            if e.fatal:
                raise
            vanished = e.status == 404  # « Enchère introuvable » : retirée du site une fois finie (vu le 14/10/2026)
            if not vanished:
                print(f"  ÉCHEC     bilan de l'enchère {name} : {e}")
        finally:
            ctx.client.pause(ctx.cfg["safety"]["read_delay_seconds"])
        outcome = classify_auction(auction)
        back = entry.get("copy_id") in ctx.collection_ids
        status = auction.get("status") if isinstance(auction, dict) else None
        ended = (entry.get("listed_at") or 0) + 60 * (entry.get("duration") or 0)
        if outcome is None and status == "active":
            ctx.on_sale.add(entry.get("copy_id"))  # confirmé en cours par le site : ne pas la remettre en vente
            continue
        if vanished and ended + SETTLE_GRACE <= ctx.run_started:
            # Résultat déduit de la collection : la carte invendue y revient, souvent sous un nouvel exemplaire
            # (voir follow_returned_copies) ; sinon elle a été vendue, à un prix que le site ne donne plus.
            returned = next((c for c in ctx.collection
                             if c.card_id == entry.get("card_id") and c.copy_id not in taken), None)
            if returned:
                taken.add(returned.copy_id)
            outcome = "unsold" if back or returned else "sold"
        if outcome is None:
            if back:
                # Une carte en vente disparaît de la collection : si elle est revenue, l'enchère est finie sans vente.
                outcome = "unsold"
            else:
                if time.time() - ended > STALE_AFTER:
                    print(f"  ?         {name} : aucune nouvelle de l'enchère depuis 2 jours, suivi abandonné")
                    ctx.state.set_status(auction_id, "gone")
                elif status not in (None, "active"):
                    print(f"  ?         {name} : statut d'enchère « {status} » non reconnu, on attend")
                continue
        common = dict(name=name, rarity=rarity, copy_id=entry.get("copy_id", ""), card_id=entry.get("card_id", ""),
                      value=entry.get("value"), duration=entry.get("duration"), auction_id=auction_id)
        if outcome == "sold" and vanished:
            print(f"  VENDUE    {name} [{rarity}] : prix inconnu, enchère retirée du site (mise de départ "
                  f"{entry.get('price')})")
            written = ctx.journal.write("vendue", result="OK (déduit de la collection, prix inconnu)", **common)
            new_status, extra, counter = "sold", {}, "sold"
        elif outcome == "sold":
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


def follow_returned_copies(ctx):
    """Une carte invendue revient dans la collection sous un NOUVEL identifiant d'exemplaire (vu le 14/10/2026 :
    42 invendues sur 43, aucune relancée). Le suivi (invendue, abandonnée) passe sur cet exemplaire, sinon la carte
    ne serait jamais relancée moins cher. Sans exemplaire de cette carte (échangée, défaussée…) : plus rien à suivre.
    """
    taken = {e.get("copy_id") for e in ctx.state.auctions.values()}
    entries = ctx.state.with_status("unsold") + ctx.state.with_status("abandoned")
    for auction_id, entry in sorted(entries, key=lambda x: -(x[1].get("listed_at") or 0)):  # la plus récente d'abord
        if entry.get("copy_id") in ctx.collection_ids:
            continue
        back = sorted((c for c in ctx.collection if c.card_id == entry.get("card_id") and c.copy_id not in taken),
                      key=lambda c: c.count > 1)  # une ligne groupée n'est jamais vendue : en dernier recours
        if back:
            entry["copy_id"] = back[0].copy_id
            taken.add(back[0].copy_id)
        else:
            ctx.state.set_status(auction_id, "gone")


def auction_minutes(value, sell):
    """Durée de l'enchère : plus longue pour les cartes chères (sell.long_duration), qui trouvent rarement preneur en
    10 minutes (vu le 15/10/2026 : 1 vendue sur 18 à 100 ou plus)."""
    long = sell.get("long_duration")
    if long and value is not None and value >= long["from_value"]:
        return long["minutes"]
    return sell["duration_minutes"]


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


def plan_sell(ctx, cards, tagged=None):
    """Mises aux enchères : relances des invendus, puis cartes étiquetées à vendre, dans les places libres.

    tagged : voir filter_candidates. Le bilan des enchères précédentes n'est fait qu'une fois par passage.
    Au hasard (sell.order: random), seuls les prix des cartes tirées sont lus : la vente commence tout de suite,
    même quand les prix gardés ont expiré et que toute la collection est à relire.
    """
    cfg, summary = ctx.cfg, ctx.summary
    sell = cfg["sell"]
    relist = sell["relist"]
    if tagged != "during":
        settle_sales(ctx)
        follow_returned_copies(ctx)
    # La nuit, presque personne n'achète : ni mise en vente ni relance, et aucune enchère ne doit s'y terminer.
    # Le bilan ci-dessus continue ; les invendus seront relancés au matin.
    night, now = sell.get("night"), time.time()
    ends = now + 60 * sell["duration_minutes"]
    if at_night(now, night) or at_night(ends, night):
        if summary["night"] is None:  # sinon déjà dit plus haut dans ce passage
            late = "" if at_night(now, night) else f" (une enchère lancée maintenant finirait à {clock_time(ends)})"
            print(f"  nuit ({night['start']}-{night['end']}) : aucune mise en vente avant {night['end']}{late}")
        summary["night"] = night["end"]
        return []
    until = antibot_until(ctx, "auction")
    if until:
        print(f"  mises en vente en pause jusqu'à {clock_time(until)} : le site a demandé une vérification anti-robot")
        return []
    candidates = filter_candidates(ctx, cards, lambda card: sell["tags"], cfg["discard"]["tags"], "à vendre",
                                   sell["require_existing_tag"], tagged)
    for card in [c for c in candidates if c.copy_id in ctx.on_sale]:
        candidates.remove(card)
        summary["skip"].add(card.copy_id)
        print(f"  ignorée   {card.name} [{card.rarity}] : déjà en vente")
    if not candidates:
        return []
    # sell.max_auctions vaut pour tout le passage, même quand « tout » vend en deux étapes.
    quota = sell["max_auctions"] - summary["auction"]
    # Pas de place libre : inutile de lire des centaines de prix pour rien.
    slots = ctx.client.auction_slots() if quota > 0 else 0
    if slots is None:
        summary["no_slot"] += len(candidates)
        print(f"  {len(candidates)} carte(s) à vendre : ventes au prochain passage")
        return []
    room = min(slots, quota)
    if room <= 0:
        summary["no_slot"] += len(candidates)
        print(f"  {len(candidates)} carte(s) à vendre, mais aucune place d'enchère libre : rien à faire maintenant")
        return []

    def make_offer(card, value):
        """(carte, valeur, Offer) si elle est à vendre maintenant, sinon None (raison affichée)."""
        if value is FAILED:
            return None
        protecting = auto_protection(card, value, cfg)
        if protecting:
            summary["protect"].add(card.copy_id)
            print(f"  PROTÉGÉE  {label(card, value)} : « analyser » va lui poser « {protecting} » (auto_tags)")
            return None
        if value is None:
            summary["skip"].add(card.copy_id)
            print(f"  ignorée   {label(card, value)} : jamais vendue, prix inconnu")
            return None
        if sell["min_value"] is not None and value < sell["min_value"]:
            summary["skip"].add(card.copy_id)
            print(f"  ignorée   {label(card, value)} : vaut moins de {sell['min_value']:g} (relancez analyser)")
            return None
        if ctx.state.latest_for(card.copy_id, "abandoned"):
            summary["skip"].add(card.copy_id)
            print(f"  ignorée   {label(card, value)} : invendue {relist['max_attempts']} fois, on ne la remet plus en vente"
                  " (retirez-la de ventes.json pour réessayer)")
            return None
        cap = cfg["discard"]["unsold_max_value"]
        if cap is not None and value < cap and ctx.state.latest_for(card.copy_id, "unsold"):
            # Relancée, elle partirait à 1 ou 2 wikicoins et occuperait une place d'enchère : plan_discard la défausse.
            summary["skip"].add(card.copy_id)
            print(f"  ignorée   {label(card, value)} : invendue à moins de {cap:g}, défaussée plutôt que relancée "
                  "(discard.unsold_max_value)")
            return None
        previous = ctx.state.latest_for(card.copy_id, "unsold") if relist["enabled"] else None
        duration = auction_minutes(value, sell)
        ends = time.time() + 60 * duration
        if at_night(ends, night):
            summary["skip"].add(card.copy_id)
            print(f"  ignorée   {label(card, value)} : une enchère de {duration} min finirait à {clock_time(ends)}, "
                  f"pendant la nuit (sell.night) : vendue au matin")
            return None
        if not previous:
            return card, value, Offer(start_price(value, sell), 1, duration=duration)
        auction_id, entry = previous
        attempt = int(entry.get("attempt", 1)) + 1
        if attempt > relist["max_attempts"]:
            summary["skip"].add(card.copy_id)
            ctx.state.set_status(auction_id, "abandoned")
            print(f"  ignorée   {label(card, value)} : invendue {attempt - 1} fois (sell.relist.max_attempts)")
            return None
        return card, value, Offer(relist_price(entry["price"], value, sell), attempt, auction_id, duration)

    since = ctx.run_started if sell["fresh_price"] else 0
    if sell["order"] == "random":
        # Tirage fixe pour la journée : le dry-run montre les mêmes cartes que le --execute qui suit.
        candidates.sort(key=lambda c: c.copy_id)
        random.Random(f"{ctx.client.user_id}:{time.strftime('%Y-%m-%d')}").shuffle(candidates)
        relaunch = {c.copy_id for c in candidates if relist["enabled"] and ctx.state.latest_for(c.copy_id, "unsold")}
        candidates.sort(key=lambda c: (c.copy_id in relaunch) != relist["first"])  # relances d'abord (ou à la fin)
        offers, unread = [], candidates
        while unread and len(offers) < room:
            batch, unread = unread[:room - len(offers)], unread[room - len(offers):]
            values = read_values(ctx.client, batch, cfg, ctx.cache, since, quiet=True)
            count_read_errors(batch, values, summary)
            offers += [o for o in (make_offer(c, values.get(c.card_id)) for c in batch) if o]
        relaunches = sum(1 for o in offers if o[2].relaunches) + sum(1 for c in unread if c.copy_id in relaunch)
    else:
        values = read_values(ctx.client, candidates, cfg, ctx.cache, since)
        count_read_errors(candidates, values, summary)
        offers = [o for o in (make_offer(c, values.get(c.card_id)) for c in candidates) if o]
        relists, fresh = [o for o in offers if o[2].relaunches], [o for o in offers if not o[2].relaunches]
        if sell["order"] == "tags":
            rank = {norm(t): i for i, t in enumerate(sell["tags"])}
            fresh.sort(key=lambda o: (min(rank.get(norm(t), len(rank)) for t in o[0].tags), -o[1]))
        else:
            fresh.sort(key=lambda o: -o[1])
        offers, unread, relaunches = relists + fresh if relist["first"] else fresh + relists, [], len(relists)

    # Relu après la lecture des prix, qui peut durer : des enchères ont pu se terminer entre-temps.
    slots = ctx.client.auction_slots()
    room = 0 if slots is None else max(0, min(slots, quota))  # illisibles : toutes attendent le passage suivant
    free = "?" if slots is None else slots
    print(f"  {len(offers) + len(unread)} carte(s) à vendre dont {relaunches} relance(s), {free} place(s) d'enchère "
          f"libre(s) → {min(room, len(offers))} vente(s)")
    chosen = offers[:room]
    waiting = [label(card, value) for card, value, _ in offers[room:]] + [f"{c.name} [{c.rarity}]" for c in unread]
    summary["no_slot"] += len(waiting)
    limit = len(waiting) if ctx.verbose else cfg.get("display", {}).get("waiting_shown", 5)
    for shown in waiting[:limit]:
        print(f"  en attente d'une place : {shown}")
    if len(waiting) > limit:
        print(f"  … et {len(waiting) - limit} autre(s) en attente (--verbose pour tout voir)")
    return [(card, "auction", offer, value) for card, value, offer in chosen]


def plan_discard(ctx, cards, tagged=None):
    """Défausses : cartes « defausse », et cartes jamais vendues (« inconnu ») des raretés de discard.unknown_rarities.
    Aussi les invendues (mises aux enchères sans trouver d'acheteur) qui valent moins de discard.unsold_max_value,
    au lieu de les relancer (plan_sell les laisse).

    Générateur de lots de DISCARD_BATCH cartes : le prix de chaque lot est relu sur le site (sauf
    discard.fresh_price: false) juste avant de le défausser. Les défausses commencent donc tout de suite, et le prix
    vérifié date d'une minute, pas du début de la relecture. Seules les cartes nécessaires pour remplir
    discard.max_per_run sont lues, la suite attend le passage suivant. tagged : voir filter_candidates.
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
                                   disc["require_existing_tag"], tagged)
    candidates.sort(key=lambda c: not c.has_any_tag(disc["tags"]))  # « defausse » d'abord, puis « inconnu »
    cap = disc["unsold_max_value"]
    by_unsold = {}  # invendues retenues : exemplaire -> prix gardé (pour défausser les moins chères d'abord)
    if cap is not None:
        follow_returned_copies(ctx)  # une invendue revient sous un nouvel exemplaire (déjà fait par « tout »)
        unsold = {e.get("copy_id") for status in ("unsold", "abandoned") for _, e in ctx.state.with_status(status)}
        taken = {c.copy_id for c in candidates}
        # Étiquetées à vendre seulement : une invendue étiquetée « defausse » suit les règles de jour, ci-dessus.
        returned = [c for c in cards
                    if c.copy_id in unsold and c.copy_id not in taken and not c.has_any_tag(tags_for(c))]
        for card in filter_candidates(ctx, returned, lambda c: cfg["sell"]["tags"], [], "à vendre", tagged=tagged):
            known = ctx.cache.get(card)
            if not (_is_number(known) and known >= cap):  # valait déjà cap ou plus : gardée, prix même pas relu
                by_unsold[card.copy_id] = known if _is_number(known) else 0
                candidates.append(card)
        candidates.sort(key=lambda c: by_unsold.get(c.copy_id, -1))  # tri stable : « defausse », « inconnu » d'abord
        if by_unsold:
            print(f"  {len(by_unsold)} invendue(s) : défaussée(s) au lieu d'être relancée(s) si elle(s) vaut moins de "
                  f"{cap:g} (discard.unsold_max_value)")
    since = ctx.run_started if disc["fresh_price"] else 0
    limit = max(0, disc["max_per_run"] - summary["discard"])  # pour tout le passage, même en deux étapes (« tout »)
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
            protecting = auto_protection(card, value, cfg)
            if protecting:
                summary["protect"].add(card.copy_id)
                print(f"  PROTÉGÉE  {label(card, value)} : « analyser » va lui poser « {protecting} » (auto_tags)")
                continue
            if card.copy_id in by_unsold:
                if value is None or value >= cap:
                    summary["skip"].add(card.copy_id)
                    worth = "jamais vendue" if value is None else f"vaut {value:g}"
                    print(f"  ignorée   {label(card, value)} : invendue, mais {worth} (pas moins de {cap:g}, "
                          "discard.unsold_max_value) : sera relancée")
                    continue
                steps.append((card, "discard", None, value))
                continue
            if value is None and norm(card.rarity) not in unknown_rarities:
                summary["skip"].add(card.copy_id)
                print(f"  ignorée   {label(card, value)} : jamais vendue, prix inconnu (discard.unknown_rarities)")
                continue
            if value is not None and disc["max_value"] is None and not card.has_any_tag(disc["tags"]):
                # Retenue seulement comme « jamais vendue », mais elle a maintenant un prix : sans discard.max_value
                # pour vérifier qu'il est bas, elle n'est pas défaussée (elle vaut peut-être cher).
                summary["skip"].add(card.copy_id)
                print(f"  ignorée   {label(card, value)} : n'est plus « jamais vendue » (relancez analyser)")
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
        print(f"  {len(rest)} autre(s) carte(s) à défausser : {disc['max_per_run']} au plus par passage "
              "(discard.max_per_run), la suite au prochain passage")


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
            return f"ENCHÈRE départ={arg.price}" + (f" (relance {arg.attempt})" if arg.attempt > 1 else "") \
                + (f" {arg.duration} min" if arg.duration and arg.duration >= 30 else "")
        return {"add_tag": f"+ étiquette « {arg} »", "remove_tag": f"- étiquette « {arg} »", "discard": "DÉFAUSSE"}[kind]

    def _log(self, card, kind, arg, value, result, balance=None, auction_id=""):
        action = {"add_tag": "etiquette+", "remove_tag": "etiquette-", "discard": "defausse",
                  "auction": "relance" if kind == "auction" and arg.attempt > 1 else "enchere"}[kind]
        extra = {}
        if kind == "auction":
            extra = dict(price=arg.price, duration=arg.duration or self.cfg["sell"]["duration_minutes"])
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
            balance = as_dict(self.client.discard(card)).get("balance")
            self.ctx.state.close_copy(card.copy_id, "gone")  # invendue défaussée la nuit : plus rien à relancer
            self._log(card, kind, arg, value, "OK", balance=balance)
            return f"OK, solde : {'?' if balance is None else balance}"
        duration = arg.duration or self.cfg["sell"]["duration_minutes"]
        response = as_dict(self.client.create_auction(card, arg.price, duration))
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
            data = as_dict(client.open_pack())
        except ApiError as e:
            if e.code == "no_packs":
                regen = parse_time(e.body.get("next_regen_at"))
                print("  plus de booster disponible pour l'instant"
                      + (f" (prochain vers {clock_time(regen)})" if regen else ""))
                return
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
    """Enchère du marché suivie par les achats : mise à fin - buy.snipe_seconds (ou dès sight_at, voir
    buy.bid_on_sight), revue à fin - buy.last_check_seconds."""

    auction_id: str
    card: Card  # copy_id vide : la carte n'est pas (encore) à nous
    end_at: float
    value: float
    ceiling: int | None  # None : pas de plafond
    stage: int = 0  # 0 : mise à venir, 1 : dernière vérification à venir, 2 : fin (ou prolongation) à constater
    bid: int | None = None  # dernière mise du script sur cette enchère
    tags: list = field(default_factory=list)  # étiquettes de protection de la carte gagnée (voir Sniper._tags_for)
    sight_at: float | None = None  # buy.bid_on_sight : première mise dès ce moment, sans attendre la fin


class Sniper:
    """Achats aux enchères : repère sur le marché les annonces qui correspondent à buy.keywords, puis mise le minimum
    quelques secondes avant la fin, sans jamais dépasser le plafond (prix moyen de la carte × buy.price_factor).

    Tout se fait pendant les pauses (client.waiter) : celle de --loop entre deux passages, et celles entre deux
    requêtes d'un passage. Une seule requête à la fois et une seule session, donc rien à partager entre processus,
    sauf avec vos autres comptes : chaque annonce n'est suivie que par l'un d'eux (BuyClaims).
    """

    def __init__(self, ctx, one_shot=False, mine=None, claims=None, market=True):
        self.ctx, self.client, self.buy = ctx, ctx.client, ctx.cfg["buy"]
        self.one_shot = one_shot  # sans --loop : « acheter » attend la fin des enchères repérées, puis s'arrête
        # False : ce compte n'achète pas (buy.accounts). Le marché n'est jamais lu ; seul le résultat des mises déjà
        # posées l'est, pour que buy.tag protège une carte gagnée.
        self.market = market
        # Vos autres comptes enregistrés (id -> nom) : on ne surenchérit pas sur eux et on n'achète pas leurs annonces.
        self.mine = {k: v for k, v in (mine or {}).items() if k != self.client.user_id}
        self.claims = claims  # BuyClaims : une annonce n'est suivie que par un de vos comptes
        self.targets = {}  # auction_id -> Target
        self.ignored = {}  # auction_id -> fin : annonces écartées pour de bon (trop chère, déjà possédée…)
        # card_id -> {auction_id: (annonce, fin)} : autres annonces d'une carte déjà suivie, reprises si elle est perdue
        self.spares = {}
        self.name_turn = 0  # prochain nom de buy.names à chercher
        self.last_sight = 0.0  # buy.bid_on_sight : moment prévu de la dernière première mise
        # Mises envoyées (buy.max_bids), y compris avant un redémarrage du script : relues dans le journal.
        self.bid_times = recent_bids(ctx.journal, (self.buy.get("max_bids") or {}).get("minutes", 0) * 60) \
            if ctx.execute else []
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
        deadline, scan = time.time() + seconds, scan and self.market
        if self.busy:
            time.sleep(seconds)
            return
        while True:
            # Vérification anti-robot demandée sur une mise : ni mise ni lecture du marché, mais le résultat des mises
            # déjà posées est toujours lu (sinon une carte gagnée n'aurait pas buy.tag pour la protéger).
            bidding = not self._paused()
            if bidding:
                self._run_due(scan)
            else:
                self.read_results()
            now = time.time()
            # Vérification imminente : on l'attend, plutôt que de laisser partir une requête qui la retarderait.
            soon = bidding and self._next_check() <= now + CHECK_MARGIN
            if now >= deadline and not soon:
                return
            due = self._next_event(scan and bidding, checks=bidding)
            time.sleep(max(0.0, (due if soon else min(deadline, due)) - now))

    def drain(self):
        """Sans --loop : attend la fin des enchères repérées, et leur résultat, sans relire le marché."""
        while True:
            due = self._next_event(scan=False, checks=not self._paused())
            if due == math.inf:
                return
            self.wait(max(0.0, due - time.time()), scan=False)

    def _paused(self):
        return antibot_until(self.ctx, "bid") is not None

    def _due(self, target):
        if target.stage == 0:
            snipe = target.end_at - self.buy["snipe_seconds"]
            return snipe if target.sight_at is None else min(snipe, target.sight_at)
        if target.stage == 1:
            return target.end_at - self.buy["last_check_seconds"]
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

    def _next_event(self, scan, checks=True):
        times = list(self.results.values())
        if checks:
            times += [self._due(t) for t in self.targets.values()]
        if scan:
            times.append(self.next_scan)
        return min(times, default=math.inf)

    def _run_due(self, scan):
        if scan and time.time() >= self.next_scan:
            self._guarded(self.scan)
        self._checks_due()
        self.read_results()

    def read_results(self):
        """Lit le résultat (gagnée ou perdue) des enchères où le script a misé, une fois leur heure venue."""
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
        was_quick, self.client.quick = self.client.quick, True  # une mise n'attend pas : nouvelles tentatives rapides
        try:
            action(*args)
        except ApiError as e:
            if e.antibot:
                self._stand_down()
            elif e.fatal and self.client.session_lost:
                raise
            else:
                self.say(f"ÉCHEC : {e}")
        except Exception as e:
            # Bug ou réponse imprévue : seule cette enchère est laissée, les achats continuent. Relire le marché ou un
            # résultat est déjà reprogrammé ; une enchère suivie serait revérifiée aussitôt, sans fin.
            self.say(f"PLANTAGE (achats continués) : {crash_report(e)}")
            if action == self._check:
                self._finish(*args)
        finally:
            self.busy, self.client.quick = was_busy, was_quick

    def _stand_down(self):
        """Vérification anti-robot demandée : plus aucune mise pendant la pause. Les enchères suivies sont laissées ;
        celles où le script a déjà misé attendent leur résultat, lu même pendant la pause."""
        antibot_strike(self.ctx, "bid")
        for target in list(self.targets.values()):
            self._finish(target)

    # --- marché ---

    def scan(self):
        """Relit le marché : annonces qui se terminent avant la relecture d'après (avec de la marge).

        Les mots-clés sont cherchés à chaque relecture. Les noms (buy.names) chacun à son tour, environ toutes les
        buy.names_minutes : une recherche par nom renvoie d'un coup toutes ses annonces, même celles qui finissent des
        heures plus tard (vu le 15/10/2026), et le site met jusqu'à 30 s à répondre à chaque recherche.
        """
        now = time.time()
        self.next_scan = now + self.buy["scan_minutes"] * 60
        horizon = now + 2 * self.buy["scan_minutes"] * 60 + self.buy["snipe_seconds"]
        self.ignored = {aid: end for aid, end in self.ignored.items() if end > now}
        self.spares = {cid: kept for cid, spares in self.spares.items()
                       if (kept := {aid: v for aid, v in spares.items() if v[1] > now})}
        searches = [(keyword, horizon) for keyword in self.buy["keywords"]]
        names = self.buy["names"]
        if names:
            turn = min(len(names), math.ceil(len(names) * self.buy["scan_minutes"] / self.buy["names_minutes"]))
            for _ in range(turn):  # ses annonces sont suivies jusqu'à son tour suivant
                searches.append((names[self.name_turn % len(names)], horizon + self.buy["names_minutes"] * 60))
                self.name_turn += 1
        found = {}
        for query, until in searches:
            for page in range(1, MAX_SCAN_PAGES + 1):
                self._clear_ahead()
                if self._paused():  # vérification anti-robot demandée sur une mise de _clear_ahead
                    return
                data = as_dict(self.client.search_auctions(query, page))
                self.client.pause(self.ctx.cfg["safety"]["read_delay_seconds"])
                auctions = [a for a in data.get("auctions") or [] if isinstance(a, dict)]
                ends = [parse_time(a.get("end_at")) for a in auctions]
                for auction, end in zip(auctions, ends):
                    if end is not None and now + LAST_CHECK_SECONDS + 2 < end <= until:
                        found.setdefault(str(auction.get("id")), (auction, end))
                if not data.get("hasMore") or not auctions or None in ends or max(ends) > until:
                    break
        new = sorted((v for aid, v in found.items() if aid not in self.targets and aid not in self.ignored),
                     key=lambda v: v[1])
        read = 0
        for auction, end in new:
            self._clear_ahead()  # lire un prix prend du temps : les mises à faire bientôt passent avant
            if self._paused():  # plus aucune mise pendant la pause : inutile de repérer d'autres annonces
                break
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
        if not reason and any(t.card.card_id == card.card_id for t in self.targets.values()):
            # Une seule annonce suivie par carte, pour ne pas l'acheter deux fois. Celle-ci est gardée de côté :
            # reprise si l'autre est perdue (voir _promote), sans attendre que son nom soit recherché à nouveau.
            self.spares.setdefault(card.card_id, {})[aid] = (auction, end)
            if self.ctx.verbose:
                self.say(f"ignorée   {card.name} [{card.rarity}] : même carte déjà suivie (reprise si elle est perdue)")
            return 0
        quiet = bool(reason)
        if not reason:
            reason = self._taken(aid, end, take=False)  # suivie par un autre compte : prix même pas lu
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
            else:
                # Réservée pour ce compte, sauf si un autre l'a prise entre-temps. Pas en simulation : elle
                # priverait d'achats un compte qui tourne pour de vrai.
                reason = self._taken(aid, end, take=self.ctx.execute)
        if reason:
            self.ignored[aid] = end
            if self.ctx.verbose or not quiet:
                self.say(f"ignorée   {label(card, value)} : {reason}")
            return read
        sight = None
        if self.buy["bid_on_sight"]:
            # Mise sans attendre la fin : jamais en retard (le site met parfois 30 s à répondre), et une mise faite
            # dans la dernière minute fait prolonger l'enchère de toute façon. Espacées entre elles (anti-robot).
            sight = self.last_sight = max(time.time(), self.last_sight + SIGHT_SPACING)
        self.targets[aid] = Target(aid, card, end, value, ceiling, tags=self._tags_for(auction, info), sight_at=sight)
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
        # Déjà misée ou achetée : on ne l'achète pas deux fois (une annonce de la même carte encore suivie : _consider).
        if any(e.get("card_id") == card.card_id for status in ("bid", "vanished", "won")
               for _, e in self.ctx.purchases.with_status(status)):
            return "même carte déjà misée ou achetée"
        if not self._match(auction, info):
            return "mot-clé absent du titre et de la catégorie"
        excluded = next((x for x in self.buy["exclude"] if found_in(x, self._text(auction, info))), None)
        if excluded:
            return f"« {excluded} » dans le titre ou la catégorie (buy.exclude)"
        return None

    def _match(self, auction, info):
        """Mot-clé (buy.keywords) ou nom (buy.names) présent dans le titre ou la catégorie de l'annonce, sinon None.

        Le site cherche plus large (« aouar » trouve aussi « Anas Tajaouart ») : on vérifie, en début de mot. « lyon »
        trouve « lyonnais » ou « To-Lyon », pas « Elyon ».
        """
        text = self._text(auction, info)
        return next((k for k in self.buy["keywords"] if found_in(k, text)), None) \
            or next((n for n in self.buy["names"] if found_in(n, text, whole=True)), None)

    @staticmethod
    def _text(auction, info):
        return norm(" ".join(str(x or "") for x in (auction.get("snapshot_search_document"),
                                                     info.get("wikipedia_title"), info.get("category"))))

    def _tags_for(self, auction, info):
        """Étiquettes de protection de la carte gagnée : celles de buy.keyword_tags dont le texte est dans son titre ou
        sa catégorie, celles de buy.names_tags si elle a été trouvée par son nom, sinon buy.tag."""
        text, tags = self._text(auction, info), []
        for key, value in (self.buy.get("keyword_tags") or {}).items():
            if found_in(key, text):
                tags += text_list(value)
        if any(found_in(name, text, whole=True) for name in self.buy["names"]):
            tags += text_list(self.buy.get("names_tags")) or [self.buy["tag"]]
        return list({norm(t): t for t in tags or [self.buy["tag"]] if t}.values())  # sans doublon, dans l'ordre

    def _promote(self, card_id):
        """L'annonce suivie de cette carte est perdue (ou trop chère) : une autre de ses annonces est suivie à la place."""
        if self._paused():
            return  # plus aucune mise pendant la pause : elles restent de côté
        for aid, (auction, end) in sorted(self.spares.pop(card_id, {}).items(), key=lambda x: x[1][1]):
            if end > time.time() + LAST_CHECK_SECONDS + 2 and aid not in self.targets and aid not in self.ignored:
                self._consider(auction, end)  # suivie, ou remise de côté si une autre vient d'être reprise

    def _taken(self, aid, end, take):
        """Raison de laisser l'annonce si un autre de vos comptes la suit déjà, sinon None."""
        holder = self.claims.holder(aid, end, take) if self.claims else None
        return f"suivie par votre compte {holder}" if holder else None

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
        try:
            data = self.client.get_auction(target.auction_id)
        except ApiError as e:
            if stage >= 2 or e.status == 404:
                # Déjà finie, plus rien à miser : la relire sans fin bloquerait tout le script (attente jamais
                # terminée). Le résultat d'une mise posée sera lu plus tard, toutes les 5 minutes.
                self._finish(target)
            if e.status == 404:
                return  # « Enchère introuvable » : retirée du site une fois finie (vu le 14/10/2026), c'est tout
            raise
        auction = data.get("auction", data) if isinstance(data, dict) else {}
        end, now = parse_time(auction.get("end_at")) or target.end_at, time.time()
        if auction.get("status") != "active" or end <= now:
            if auction.get("status") != "active" and end > now and stage < 2:
                self.say(f"terminée  {label(target.card, target.value)} : plus en cours avant la fin prévue "
                         f"(statut « {auction.get('status')} » sur le site), on laisse")
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
            first = target.bid is None
            free_at = self._bid_budget(first)
            if free_at is not None:
                # Limite de mises atteinte : une première mise attend qu'une place se libère (au plus tard
                # buy.snipe_seconds avant la fin) ; sinon l'enchère est laissée.
                if first and stage == 0 and free_at < end - self.buy["snipe_seconds"]:
                    target.stage, target.sight_at = 0, free_at
                    return
                limit = self.buy["max_bids"]
                self.say(f"laissée   {label(target.card, target.value)} : {limit['count']} mises en "
                         f"{limit['minutes']:g} min au plus (buy.max_bids, contre la vérification anti-robot)")
                self._finish(target)
                return
            outbid = f"dépassé à {current}, " if not first else ""
            self._bid(target, need, end - now, outbid)

    def _bid_budget(self, first):
        """None si une mise peut partir (buy.max_bids), sinon le moment où une place se libère."""
        limit = self.buy.get("max_bids")
        if not limit:
            return None
        window, now = limit["minutes"] * 60, time.time()
        self.bid_times = sorted(t for t in self.bid_times if now - t < window)
        allowed = limit["count"] - (BID_RESERVE if first else 0)
        if len(self.bid_times) < allowed:
            return None
        return self.bid_times[len(self.bid_times) - allowed] + window

    def _bid(self, target, amount, left, note=""):
        ctx = self.ctx
        self.say(f"-> MISE {amount:<8} {label(target.card, target.value)} ({note}plafond "
                 f"{show_ceiling(target.ceiling)}, fin dans {left:.0f} s)")
        if not ctx.execute:
            target.bid = amount
            ctx.summary["bid"] += 1
            return
        for attempt in range(TOO_LOW_RETRIES + 1):
            try:
                self.bid_times.append(time.time())  # même refusée, elle compte peut-être pour le site
                response = as_dict(self.client.bid(target.auction_id, amount))
                break
            except ApiError as e:
                self._log(target, amount, f"ECHEC : {e}")
                if e.fatal:
                    # Mise peut-être partie : on lira le résultat de l'enchère comme si elle l'était.
                    target.bid = amount
                    ctx.purchases.record(target, amount)
                    ctx.purchases.save()
                    raise
                # Un autre joueur a misé au même instant : le site refuse la mise et donne le nouveau minimum.
                minimum = e.body.get("min") if e.code == "bid_too_low" else None
                if _is_int(minimum) and target.ceiling is not None and minimum > target.ceiling:
                    self.say(f"   dépassé à l'instant : mise minimale {minimum} > plafond {target.ceiling}, on laisse")
                    self._finish(target)
                    return
                if _is_int(minimum) and minimum > amount and attempt < TOO_LOW_RETRIES:
                    self.say(f"   dépassé à l'instant : nouvelle mise à {minimum}")
                    amount = minimum
                    continue
                ctx.summary["error"] += 1
                self.say(f"   ÉCHEC : {e}")
                if e.antibot:
                    self._stand_down()
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
        elif target.bid is None:
            self._promote(target.card.card_id)  # jamais misée (trop chère, retirée…) : une autre annonce de la carte

    def _result(self, aid):
        """Gagnée ou perdue ? Une carte gagnée recevra buy.tag au prochain tri de la collection."""
        purchases = self.ctx.purchases
        entry = purchases.auctions.get(aid)
        if not entry or entry.get("status") != "bid":
            self.results.pop(aid, None)
            return
        self.results[aid] = time.time() + RESULT_RETRY  # si la lecture échoue ou si l'enchère n'est pas réglée
        stale = time.time() - (entry.get("end_at") or 0) > STALE_AFTER
        try:
            data = self.client.get_auction(aid)
        except ApiError as e:
            if e.status == 404:
                # « Enchère introuvable » : le site retire les enchères finies (vu le 14/10/2026, 25 min après la fin).
                # La relire toutes les 5 minutes n'y changerait rien : le résultat est déduit de la collection.
                self.results.pop(aid, None)
                purchases.set_status(aid, "vanished")
                purchases.save()
                return
            if e.fatal or not stale:
                raise
            data = None  # illisible depuis 2 jours : suivi abandonné ci-dessous
        auction = data.get("auction", data) if isinstance(data, dict) else None
        outcome, name = classify_auction(auction), f"{entry.get('name', '')} [{entry.get('rarity', '')}]"
        if outcome is None:
            if stale:
                self.say(f"?         {name} : aucun résultat depuis 2 jours, suivi abandonné")
                purchases.set_status(aid, "gone")
                purchases.save()
                self.results.pop(aid, None)
            return
        self.results.pop(aid, None)
        final = auction.get("final_price") or auction.get("current_bid")
        if outcome == "sold" and auction.get("winner_id") == self.client.user_id:
            tags = ", ".join(f"« {t} »" for t in bought_tags(self.ctx, entry))
            self.say(f"ACHETÉE   {name} pour {final} wikibidous (étiquette {tags} au prochain tri)")
            self.ctx.journal.write("achat", entry.get("name", ""), entry.get("rarity", ""), "", entry.get("card_id", ""),
                                   entry.get("value"), price=final, auction_id=aid)
            status = "won"
        else:
            self.say(f"perdue    {name}" + (f" : adjugée {final} à un autre joueur" if outcome == "sold" else ""))
            self._promote(entry.get("card_id"))
            self.ctx.journal.write("perdue", entry.get("name", ""), entry.get("rarity", ""), "",
                                   entry.get("card_id", ""), entry.get("value"), price=final, auction_id=aid,
                                   result=f"notre dernière mise : {entry.get('price')}")
            status = "lost"
        self.ctx.summary[status] += 1
        purchases.set_status(aid, status, final_price=final)
        purchases.save()


def settle_vanished(ctx, all_cards):
    """Mises dont l'enchère a été retirée du site avant la lecture du résultat : gagnée si la carte est maintenant dans
    la collection, perdue sinon. Le script ne mise que sur des cartes absentes de la collection (buy.skip_owned) ;
    sans ce réglage, une carte déjà possédée est comptée gagnée et reçoit buy.tag, ce qui la protège sans risque.
    """
    owned, changed = {c.card_id for c in all_cards}, False
    for aid, entry in ctx.purchases.with_status("vanished"):
        if (entry.get("end_at") or 0) + SETTLE_GRACE > ctx.run_started:
            continue  # collection lue trop tôt après la fin : au prochain passage
        name, price = f"{entry.get('name', '')} [{entry.get('rarity', '')}]", entry.get("price")
        common = (entry.get("name", ""), entry.get("rarity", ""), "", entry.get("card_id", ""), entry.get("value"))
        if entry.get("card_id") in owned:
            print(f"  ACHETÉE   {name} : dans la collection (enchère retirée du site, notre dernière mise : {price})")
            ctx.journal.write("achat", *common, price=price, auction_id=aid, result="OK (déduit de la collection)")
            status = "won"
        else:
            print(f"  perdue    {name} : pas dans la collection (enchère retirée du site)")
            ctx.journal.write("perdue", *common, auction_id=aid,
                              result=f"notre dernière mise : {price} (déduit de la collection)")
            status = "lost"
        ctx.summary[status] += 1
        ctx.purchases.set_status(aid, status)
        changed = True
    if changed and ctx.execute:
        ctx.purchases.save()


def bought_tags(ctx, entry):
    """Étiquettes de protection d'une carte gagnée : celles choisies à la mise (Sniper._tags_for), sinon buy.tag."""
    return [t for t in entry.get("tags") or [ctx.cfg["buy"]["tag"]] if t]


def recent_bids(journal, seconds):
    """Moments des mises de ce compte dans le journal, ces dernières secondes (buy.max_bids)."""
    if seconds <= 0:
        return []
    since, found = time.time() - seconds, []
    try:
        with open(journal.path, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f, delimiter=journal.delimiter):
                if row.get("action") == "mise" and row.get("compte") == journal.account:
                    try:
                        at = time.mktime(time.strptime(row["date"], "%Y-%m-%d %H:%M:%S"))
                    except (TypeError, ValueError):
                        continue
                    if at >= since:
                        found.append(at)
    except OSError:
        pass
    return found


def plan_bought(ctx, cards, all_cards):
    """Cartes gagnées aux enchères : on leur pose leurs étiquettes de protection (bought_tags), avant tout le reste."""
    steps = []
    for aid, entry in ctx.purchases.with_status("won"):
        card_id, tags = entry.get("card_id"), bought_tags(ctx, entry)
        if not tags:
            continue
        if not any(c.card_id == card_id for c in all_cards):
            if time.time() - (entry.get("end_at") or 0) > STALE_AFTER:
                ctx.purchases.set_status(aid, "gone")
            else:
                print(f"  {entry.get('name', '')} : pas encore dans la collection, étiquette au prochain passage")
            continue
        steps += [(c, "add_tag", tag, entry.get("value")) for c in cards if c.card_id == card_id
                  for tag in tags if not c.has_any_tag([tag])]
    return steps


def bought_tagged(ctx, cards, all_cards):
    """Après plan_bought : les achats dont la carte porte maintenant son étiquette de protection sont terminés."""
    for aid, entry in ctx.purchases.with_status("won"):
        card_id, tags = entry.get("card_id"), bought_tags(ctx, entry)
        if not tags:
            continue
        # Une ligne à plusieurs exemplaires (absente de cards) n'est jamais touchée : rien de plus à faire.
        if any(c.card_id == card_id for c in all_cards) \
                and all(c.has_any_tag([tag]) for c in cards if c.card_id == card_id for tag in tags):
            ctx.purchases.set_status(aid, "tagged")
    if ctx.execute:
        ctx.purchases.save()


# « tout » vend et défausse d'abord les cartes déjà étiquetées : ça commence tout de suite, même quand l'analyse doit
# relire les prix de toute la collection (une heure ou plus quand les prix gardés ont expiré). Les cartes étiquetées
# pendant l'analyse sont vendues et défaussées juste après (s'il reste des places et si require_existing_tag le permet).
TOUT_PHASES = [
    ("Ventes aux enchères", lambda ctx, cards: plan_sell(ctx, cards, tagged="before")),
    ("Défausses", lambda ctx, cards: plan_discard(ctx, cards, tagged="before")),
    ("Analyse des prix et étiquettes", plan_analyse),
    ("Ventes aux enchères (nouvelles étiquettes)", lambda ctx, cards: plan_sell(ctx, cards, tagged="during")),
    ("Défausses (nouvelles étiquettes)", lambda ctx, cards: plan_discard(ctx, cards, tagged="during")),
]


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
            names = len(ctx.cfg["buy"]["names"])
            print(f"  aucune enchère à suivre pour l'instant ({', '.join(ctx.cfg['buy']['keywords'])}"
                  + (f" et {names} nom(s)" if names else "") + ")")
        elif ctx.sniper.one_shot:
            print(f"  {len(ctx.sniper.targets)} enchère(s) suivie(s) : attente de leur fin (Ctrl+C pour quitter)…")
            ctx.sniper.drain()
    if command in ("boosters", "acheter"):
        return
    if ctx.sniper:
        # Résultat des mises dont l'enchère est finie, avant de lire la collection : une carte gagnée y reçoit buy.tag
        # avant d'être triée.
        ctx.sniper.read_results()
    cards, warning = ctx.client.list_cards()
    ctx.collection, ctx.collection_ids = cards, {c.copy_id for c in cards}
    ctx.on_sale = set()  # relu par le bilan à chaque passage (sinon --loop ne relancerait jamais une carte)
    print(f"{len(cards)} exemplaire(s) dans la collection.")
    if warning:
        print(f"Attention : {warning}")
    settle_vanished(ctx, cards)
    # Une ligne ×2, ×3… : on ne sait pas à quel exemplaire s'appliquent étiquettes et défausse, on n'y touche pas.
    all_cards = cards
    grouped = [c for c in cards if c.count > 1]
    if grouped:
        print(f"  {len(grouped)} ligne(s) à plusieurs exemplaires laissée(s) de côté (le script n'y touche pas).")
        if ctx.verbose:
            for card in grouped:
                print(f"  groupée   {card.name} [{card.rarity}] ×{card.count}")
        cards = [c for c in cards if c.count <= 1]
    # Mise posée dont le résultat n'est pas encore connu : gagnée, la carte est peut-être déjà là, sans buy.tag pour la
    # protéger. On n'y touche pas avant son résultat, sinon elle pourrait être vendue ou défaussée.
    pending = {e.get("card_id") for status in ("bid", "vanished") for _, e in ctx.purchases.with_status(status)}
    held = [c for c in cards if c.card_id in pending]
    if held:
        print(f"  {len(held)} carte(s) misée(s) aux enchères, résultat pas encore connu, laissée(s) de côté : "
              f"{', '.join(c.name for c in held)}.")
        cards = [c for c in cards if c.card_id not in pending]

    runner = Runner(ctx)
    tags = sorted({t for _, e in ctx.purchases.with_status("won") for t in bought_tags(ctx, e)})
    if tags:
        print(f"\n== Cartes achetées : étiquette{'s' if len(tags) > 1 else ''} {', '.join(f'« {t} »' for t in tags)} ==")
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
        "tout": TOUT_PHASES,
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


BY_ACCOUNT_KEYS = ("keywords", "names", "names_tags", "names_minutes", "scan_minutes", "price_factor", "max_price",
                   "buy_unknown")


def account_buy(cfg, cfg_file, account):
    """Réglages d'achat de ce compte : buy.by_account.<compte> par-dessus buy (pour répartir les recherches et les
    mises entre vos comptes). Un nom de compte inconnu arrête le script, comme dans buy.accounts."""
    by_account = cfg["buy"].get("by_account") or {}
    accounts = {a.casefold() for a in known_accounts(cfg_file)}
    unknown = [a for a in by_account if str(a).casefold() not in accounts]
    if unknown:
        sys.exit(f"buy.by_account : compte(s) inconnu(s) {', '.join(map(str, unknown))} (comptes enregistrés : "
                 f"{', '.join(known_accounts(cfg_file)) or 'aucun'}). Corrigez {PERSO_FILE}.")
    over = next((v for a, v in by_account.items() if str(a).casefold() == account.casefold()), None)
    if not over:
        return cfg
    cfg = dict(cfg, buy=merge(cfg["buy"], over))
    check_config(cfg)
    return cfg


def buying_accounts(cfg, cfg_file):
    """Comptes enregistrés qui achètent aux enchères : ceux de buy.accounts, ou tous si null.

    Un nom inconnu arrête le script : avec une faute de frappe, aucun compte n'achèterait sans rien dire.
    """
    accounts, wanted = known_accounts(cfg_file), cfg["buy"].get("accounts")
    if wanted is None:
        return accounts
    folded = {a.casefold(): a for a in accounts}
    unknown = [a for a in wanted if a.casefold() not in folded]
    if unknown:
        sys.exit(f"buy.accounts : compte(s) inconnu(s) {', '.join(unknown)} (comptes enregistrés : "
                 f"{', '.join(accounts) or 'aucun'}). Corrigez {PERSO_FILE}.")
    return [folded[a.casefold()] for a in wanted]


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
    print(f"Compte {client.username} enregistré ({'?' if slots is None else slots} place(s) d'enchère libre(s)).")
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
        state = SalesState(folder / "ventes.json", set_aside=False)
        session = "" if (folder / "session.json").is_file() else f"   (pas de session : « {cmd('login')} »)"
        print(f"  {folder.name:<24} {len(state.with_status('open'))} enchère(s) suivie(s){session}")
    if len(known_accounts(cfg_file)) > 1:
        print(f"Choisissez avec --compte, ex. « {cmd('tout --compte ' + known_accounts(cfg_file)[0])} ».")


def keep_awake():
    """Empêche la mise en veille pendant le passage : une veille coupe le réseau et fige le script.

    Rien ne l'empêche quand on ferme le capot sur batterie, ni quand la batterie se vide : watch_sleep le repère.
    """
    if sys.platform == "darwin" and shutil.which("caffeinate"):
        try:  # caffeinate s'arrête tout seul à la fin de ce processus ; -s : aussi la veille système, sur secteur
            subprocess.Popen(["caffeinate", "-i", "-s", "-w", str(os.getpid())],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass
    elif os.name == "nt":
        try:
            import ctypes

            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)  # ES_CONTINUOUS | SYSTEM_REQUIRED
        except (AttributeError, OSError):
            pass
    threading.Thread(target=watch_sleep, daemon=True).start()


# Mises en veille de l'ordinateur pendant que le script tournait : (début, fin). Il y était figé : ni boosters, ni
# ventes, ni défausses, ni mises (vu du 10 au 13/10/2026 : capot fermé sur batterie, script figé presque 3 jours).
SLEEPS = []
SLEEP_STEP, SLEEP_GAP = 30, 300


def watch_sleep():
    """Fil de fond : une pause de SLEEP_STEP secondes qui dure bien plus longtemps = l'ordinateur était en veille."""
    last = time.time()
    while True:
        time.sleep(SLEEP_STEP)
        now = time.time()
        if now - last > SLEEP_STEP + SLEEP_GAP:
            SLEEPS.append((last, now))
        last = now


def sleep_warning(since):
    """Avertissement si l'ordinateur s'est mis en veille depuis ce moment, sinon None."""
    gaps = [(start, end) for start, end in SLEEPS if end > since]
    if not gaps:
        return None
    asleep = int(sum(end - start for start, end in gaps)) // 60
    return (f"l'ordinateur s'est mis en veille ({asleep // 60} h {asleep % 60:02d} en tout, entre "
            f"{clock_time(gaps[0][0])} et {clock_time(gaps[-1][1])}) : le script était figé pendant ce temps "
            "(ni boosters, ni ventes, ni défausses, ni mises). Laissez l'ordinateur branché, capot ouvert.")


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
            "notify_on_dry_run": tg.get("notify_on_dry_run", False), "every_pass": tg.get("every_pass", False),
            "daily_report": tg.get("daily_report")}


def telegram_alert(username, text):
    """Alerte envoyée aussitôt (pas à la fin du passage)."""
    return f"<b>[WikiMasters]</b> {html.escape(username)}\n{html.escape(text, quote=False)}"


def daily_report(journal, day):
    """Bilan de la journée de ce compte, d'après journal.csv : ventes, défausses, achats, blocages."""
    try:
        with open(journal.path, encoding="utf-8-sig", newline="") as f:
            rows = [r for r in csv.DictReader(f, delimiter=journal.delimiter)
                    if r.get("compte") == journal.account and str(r.get("date", "")).startswith(day)]
    except OSError:
        return None

    def count(action):
        return sum(1 for r in rows if r.get("action") == action)

    def total(action):
        return sum(float(r.get("prix") or 0) for r in rows if r.get("action") == action and _is_price(r.get("prix")))

    sold, unsold = count("vendue"), count("invendue")
    lines = [
        f"Ventes : {sold} vendue(s) pour {total('vendue'):g} wikicoins, {unsold} invendue(s), "
        f"{count('enchere') + count('relance')} enchère(s) lancée(s)",
        f"Défausses : {count('defausse')}",
    ]
    if count("mise") or count("achat"):
        lines.append(f"Achats : {count('achat')} carte(s) pour {total('achat'):g} wikibidous, {count('perdue')} perdue(s)")
    blocked = sum(1 for r in rows if "anti-robot" in str(r.get("resultat", "")))
    if blocked:
        lines.append(f"Vérifications anti-robot : {blocked}")
    balance = next((r["solde"] for r in reversed(rows) if r.get("solde")), None)
    if balance:
        lines.append(f"Solde : {balance}")
    return "\n".join(lines)


def _is_price(text):
    try:
        float(text)
        return True
    except (TypeError, ValueError):
        return False


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
    cfg = account_buy(cfg, args.config, folder.name)
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
        state=SalesState(folder / "ventes.json", set_aside=args.execute),
        purchases=PurchaseState(folder / "achats.json", set_aside=args.execute),
    )
    keywords = cfg["buy"]["keywords"] + cfg["buy"]["names"]
    if args.command == "acheter" and not keywords:
        sys.exit(f"buy.keywords est vide : rien à chercher sur le marché. Ajoutez vos mots-clés dans {PERSO_FILE}, "
                 "ex. buy: { keywords: [\"lyon\"], tag: \"lyon\" } (voir README).")
    buyers = buying_accounts(cfg, args.config) if keywords else []
    buys = folder.name in buyers
    if args.command == "acheter" and not buys:
        sys.exit(f"Le compte {folder.name} n'achète pas : buy.accounts ({PERSO_FILE}) ne nomme que "
                 f"{', '.join(buyers)}.")
    sniping = bool(keywords) and (args.command == "acheter" or (args.command == "tout" and bool(args.loop)))
    # Mises déjà posées dont le résultat reste à lire : les achats tournent aussi pour ça, sans lire le marché, pour
    # que la carte gagnée reçoive buy.tag avant d'être triée.
    pending = args.execute and bool(ctx.purchases.with_status("bid"))
    if sniping or pending:
        mine = account_user_ids(args.config)
        # Plusieurs comptes qui achètent : chaque annonce n'est suivie que par l'un d'eux.
        claims = BuyClaims(config_path(args.config, ACCOUNTS_DIR) / CLAIMS_FILE, folder.name) \
            if len(buyers) > 1 else None
        ctx.sniper = Sniper(ctx, one_shot=not args.loop, mine=mine, claims=claims, market=buys and sniping)
        client.waiter = ctx.sniper.wait
    mode = "EXÉCUTION RÉELLE" if args.execute else "SIMULATION (rien n'est modifié, ajoutez --execute pour agir)"
    lock = RunLock(folder / ".wikimasters.lock")
    if args.execute:
        lock.acquire()
    keep_awake()

    alerts = telegram and (args.execute or telegram["notify_on_dry_run"])
    if alerts:
        ctx.notify = lambda text: send_telegram(telegram, telegram_alert(client.username, text))
    outcome, cycle, reported = ("ok", None), 0, time.time()
    last_problem, report_day = None, None
    try:
        while True:
            # Un bilan par cycle de --loop.
            cycle += 1
            ctx.journal.merge_backup()
            print(f"\n=== [{time.strftime('%H:%M:%S')}] {args.command} — {mode} — compte {client.username} ===")
            if keywords and args.command == "tout" and cycle == 1:
                if not buys:
                    print(f"Achats aux enchères : pas sur ce compte (buy.accounts : {', '.join(buyers)}).")
                elif not args.loop:
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
                if not args.loop:
                    raise
                # Avec --loop, un plantage n'arrête que ce passage : le suivant réessaie, comme après une erreur du site.
                print(f"\nPLANTAGE : {crash_report(e)}")
                print("Le script continue : le prochain passage réessaiera (copiez ce message pour le faire corriger).")
            finally:
                ctx.summary["retried"], client.retried = client.retried, 0
                summary_text = print_summary(ctx.summary, args.execute)
                asleep, reported = sleep_warning(reported), time.time()
                if asleep:
                    print(f"Attention : {asleep}")
                    summary_text += f"\nAttention : {asleep}"
                if ctx.journal.lost:
                    print(f"Attention : {ctx.journal.lost} ligne(s) non écrite(s) dans le journal ({journal_cfg['file']}).")
                if ctx.journal.diverted:
                    print(f"Attention : {ctx.journal.diverted} ligne(s) écrite(s) dans {ctx.journal.backup.name} "
                          f"({journal_cfg['file']} verrouillé), recopiée(s) au prochain passage.")
                # Telegram : un message seulement quand il faut agir (arrêt, plantage, veille), une fois par problème
                # (un site en panne des heures n'envoie pas un message par passage), sauf telegram.every_pass.
                problem = (outcome[1] if outcome[0] in ("error", "crash") else None) or asleep
                if alerts and (telegram["every_pass"] or (problem and problem != last_problem)):
                    send_telegram(telegram, telegram_message(client.username, args.command, outcome, summary_text))
                last_problem = problem
                # Bilan de la journée, une fois par jour, au premier passage après telegram.daily_report.
                local, at = time.localtime(time.time()), clock_minutes(telegram and telegram["daily_report"])
                today = time.strftime("%Y-%m-%d", local)
                if alerts and args.execute and at is not None and local.tm_hour * 60 + local.tm_min >= at \
                        and report_day != today:
                    report_day, text = today, daily_report(ctx.journal, today)
                    if text:
                        title = f"📊 Bilan du {time.strftime('%d/%m', local)}"
                        send_telegram(telegram, telegram_alert(client.username, f"{title}\n{text}"))
                # Cartes en attente d'une place : le passage suivant a lieu dès qu'une enchère se termine.
                waiting = ctx.summary["no_slot"] and not antibot_until(ctx, "auction")
                # Rien fait et rien de reporté : la nuit, inutile de revenir toutes les 15 minutes (voir plus bas).
                idle = not any(ctx.summary[k] for k in ("tag_add", "tag_remove", "discard", "auction", "pack",
                                                        "deferred"))
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
            night, now = cfg["sell"].get("night"), time.time()
            if outcome[0] == "ok" and idle and at_night(now, night):
                # Plus rien à vendre ni à défausser cette nuit : prochain passage au matin. Les achats continuent
                # pendant la pause ; une carte gagnée reçoit sa protection au début de ce passage, avant tout tri.
                pause = night_end(now, night) - now
                buying = " (les achats continuent)" if ctx.sniper and ctx.sniper.market else ""
                print(f"\nNuit : plus rien à vendre ni à défausser, prochain passage à {night['end']}{buying} "
                      "(Ctrl+C pour quitter)")
            elif outcome[0] == "ok" and waiting and (free := next_free_slot(ctx)) and free < now + args.loop * 60:
                # Une place d'enchère se libère avant le passage prévu : il a lieu dès qu'elle est libre.
                pause = max(MIN_LOOP_SECONDS, free - now)
                print(f"\nUne place d'enchère se libère vers {clock_time(free)} : prochain passage dans "
                      f"{pause / 60:.0f} minute(s)… (Ctrl+C pour quitter)")
            else:
                pause = args.loop * 60
                print(f"\nProchain passage dans {args.loop} minute(s)… (Ctrl+C pour quitter)")
            deadline = time.time() + pause
            try:
                (ctx.sniper.wait if ctx.sniper else time.sleep)(pause)
            except ApiError as e:  # session perdue pendant les achats (les autres erreurs n'arrêtent que la mise)
                print(f"\nARRÊT : {e}")
                print_summary(ctx.summary, args.execute)
                sys.exit(1)
            except Exception as e:  # plantage des achats : la pause se finit sans eux, le passage suivant a lieu
                print(f"\n[achats] PLANTAGE : {crash_report(e)}")
                time.sleep(max(0.0, deadline - time.time()))
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
    if summary["retried"]:
        others.append(f"{summary['retried']} requête(s) renvoyée(s) après une erreur passagère du site")
    if summary["night"]:
        others.append(f"nuit : aucune mise en vente avant {summary['night']}")
    for group, until in summary["paused"].items():
        others.append(f"{group} en pause jusqu'à {clock_time(until)} (vérification anti-robot demandée par le site)")
    summary_text = f"Résumé ({verb}) : {', '.join(parts)} | {', '.join(others)}"
    print(f"\n{summary_text}")
    return summary_text


if __name__ == "__main__":
    main()
