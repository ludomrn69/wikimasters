#!/usr/bin/env python3
"""Bot WikiMasters : trie les cartes de votre collection selon config.yaml.

    python wikimasters.py login        une seule fois : enregistre la session (cookie du navigateur)
    python wikimasters.py analyser     lit les prix moyens et pose les étiquettes de prix (defausse, +10, +100…)
    python wikimasters.py vendre       met aux enchères les cartes étiquetées à vendre (places libres)
    python wikimasters.py defausser    défausse les cartes étiquetées « defausse »
    python wikimasters.py tout         analyser, puis vendre, puis defausser

Sans --execute, chaque commande est un dry-run : elle affiche ce qu'elle ferait sans rien modifier.
"""

import argparse
import base64
import binascii
import csv
import html
import json
import math
import os
import pathlib
import random
import re
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
MAX_PAGES = 500
# Le jeton d'accès est renouvelé quand il lui reste moins que cette marge (secondes).
REFRESH_MARGIN = 300
# Taille maximale d'un morceau de cookie, comme le fait la bibliothèque Supabase du site.
COOKIE_CHUNK_SIZE = 3180
# Nouvelles tentatives pour une lecture (GET) en cas de coupure réseau, 429 ou 5xx.
GET_RETRY_DELAYS = (2, 8)
MAYBE_DONE = "L'action a peut-être été effectuée : vérifiez sur le site."
COMMANDS = ("analyser", "vendre", "defausser", "tout")
# Identifiants personnels (jeton Telegram…), à côté de config.yaml et exclus de git.
SECRETS_FILE = "secrets.yaml"


class ApiError(Exception):
    """Erreur d'API. fatal=True arrête le passage, sinon seule l'action en cours est abandonnée."""

    def __init__(self, message, fatal=False, status=None):
        super().__init__(message)
        self.fatal = fatal
        self.status = status


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


def load_config(path):
    try:
        with open(path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
    except OSError as e:
        sys.exit(f"Impossible de lire {path} ({type(e).__name__}).")
    except yaml.YAMLError as e:
        where = getattr(e, "problem_mark", None)
        line = f" ligne {where.line + 1}" if where else ""
        sys.exit(f"{path} est mal écrit{line} (indentation, guillemets, deux-points ?).")
    if not isinstance(cfg, dict):
        sys.exit(f"{path} est vide ou mal écrit : il doit contenir les sections site, protection, price…")
    return cfg


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

    def section(name):
        value = cfg.get(name) if isinstance(cfg, dict) else None
        if not isinstance(value, dict):
            err(f"{name} : section manquante ou mal écrite")
            return {}
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
        unknown = set(cond) - CONDITION_KEYS
        if unknown:
            err(f"{where} : condition(s) inconnue(s) {sorted(unknown)}")
        if "rarity_in" in cond:
            str_list(cond, where, "rarity_in", allow_empty=False)
        if "name_contains" in cond and not isinstance(cond["name_contains"], str):
            err(f"{where}.name_contains doit être un texte")
        if "shiny" in cond and not isinstance(cond["shiny"], bool):
            err(f"{where}.shiny doit valoir true ou false")
        for key in ("min_value", "max_value"):
            if key in cond and not _is_number(cond[key]):
                err(f"{where}.{key} doit être un nombre")

    site = section("site")
    for key in ("base_url", "supabase_url", "supabase_anon_key", "session_file"):
        if not isinstance(site.get(key), str) or not site.get(key):
            err(f"site.{key} doit être un texte")

    prot = section("protection")
    prot_tags = str_list(prot, "protection", "tags")
    boolean(prot, "protection", "starred")
    boolean(prot, "protection", "shiny")
    str_list(prot, "protection", "rarities")
    str_list(prot, "protection", "name_contains")

    price = section("price")
    number(price, "price", "cache_hours", 0)
    if not isinstance(price.get("cache_file"), str) or not price.get("cache_file"):
        err("price.cache_file doit être un nom de fichier")

    pt = section("price_tags")
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
        unknown = set(band) - {"tag", "from", "below", "color"}
        if unknown:
            err(f"{where} : clé(s) inconnue(s) {sorted(unknown)}")
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
        unknown = set(rule) - {"tag", "when", "color"}
        if unknown:
            err(f"{where} : clé(s) inconnue(s) {sorted(unknown)} (les conditions vont sous « when: »)")
        if not isinstance(rule.get("when"), dict) or not rule.get("when"):
            err(f"{where}.when doit contenir au moins une condition (sinon l'étiquette irait sur toutes les cartes)")
        else:
            conditions(rule["when"], f"{where}.when")
        if norm(rule["tag"]) in {norm(t) for t in band_tags}:
            err(f"{where}.tag « {rule['tag']} » est déjà une étiquette de prix : elle serait retirée puis remise à chaque analyse")
        if rule.get("color") is not None and not isinstance(rule.get("color"), str):
            err(f"{where}.color doit être un texte, ex. \"#22c55e\"")

    disc = section("discard")
    disc_tags = str_list(disc, "discard", "tags", allow_empty=False)
    number(disc, "discard", "max_value", 0, allow_none=True)
    choice(disc, "discard", "unknown_price", ["skip", "discard"])
    number(disc, "discard", "max_per_run", 0, integer=True)
    boolean(disc, "discard", "fresh_price")
    boolean(disc, "discard", "require_existing_tag")

    sell = section("sell")
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
    if not isinstance(sell.get("state_file"), str) or not sell.get("state_file"):
        err("sell.state_file doit être un nom de fichier")
    relist = sell.get("relist")
    if not isinstance(relist, dict):
        err("sell.relist : section manquante ou mal écrite")
    else:
        boolean(relist, "sell.relist", "enabled")
        boolean(relist, "sell.relist", "first")
        number(relist, "sell.relist", "factor", 0, strict=True)
        if _is_number(relist.get("factor")) and relist["factor"] > 1:
            err("sell.relist.factor doit être <= 1 (une relance ne se fait pas plus cher)")
        number(relist, "sell.relist", "min_start_price", 1, integer=True)
        number(relist, "sell.relist", "max_attempts", 1, integer=True)

    journal = section("journal")
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

    safety = section("safety")
    number(safety, "safety", "max_actions_per_run", 0, integer=True)
    number(safety, "safety", "delay_seconds", 0)
    number(safety, "safety", "read_delay_seconds", 0)
    number(safety, "safety", "max_consecutive_errors", 1, integer=True)
    number(safety, "safety", "max_consecutive_read_failures", 1, integer=True)

    display = cfg.get("display", {})
    if not isinstance(display, dict) or not isinstance(display.get("verbose", False), bool):
        err("display.verbose doit valoir true ou false")
    elif not _is_int(display.get("waiting_shown", 5)) or display.get("waiting_shown", 5) < 0:
        err("display.waiting_shown doit être un entier >= 0")

    telegram = cfg.get("telegram", {"enabled": False})
    if not isinstance(telegram, dict):
        err("telegram : section mal écrite")
    else:
        for key in ("bot_token", "chat_id"):
            if key in telegram:
                err(f"telegram.{key} ne doit pas être dans ce fichier (il finirait dans git) : "
                    f"mettez-le dans {SECRETS_FILE}, voir le README")
        unknown = set(telegram) - {"enabled", "notify_on_dry_run", "bot_token", "chat_id"}
        if unknown:
            err(f"telegram : clé(s) inconnue(s) {sorted(unknown)}")
        boolean(telegram, "telegram", "enabled")
        if "notify_on_dry_run" in telegram:
            boolean(telegram, "telegram", "notify_on_dry_run")

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
            sys.exit(f"{self.path} est illisible : relancez « python wikimasters.py login ».")
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

    def get(self, card):
        entry = self.data.get(self._key(card))
        if self.ttl <= 0 or not isinstance(entry, dict) or not _is_number(entry.get("at")):
            return self.MISSING
        if time.time() - entry["at"] > self.ttl or entry["at"] < self.not_before:
            return self.MISSING
        return entry.get("value")

    def set(self, card, value):
        if self.ttl > 0:
            self.data[self._key(card)] = {"value": value, "at": time.time()}

    def save(self):
        if self.ttl <= 0:
            return
        try:
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(self.data), encoding="utf-8")
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
        self._tag_ids = None
        self._last_refresh = None

        session = session or store.load()
        if not session:
            sys.exit("Aucune session enregistrée : lancez d'abord « python wikimasters.py login » (voir README).")
        try:
            self._use_session(session, save=False)
        except (ValueError, KeyError, TypeError):
            sys.exit(f"{store.path} est invalide : relancez « python wikimasters.py login ».")

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
        self.username = (claims.get("user_metadata") or {}).get("username") or claims.get("email") or "?"
        self.expires_at = min(deadlines)
        self.cookies = session_cookies(session, self.ref)

    def time_left(self):
        return self.expires_at - time.time()

    def refresh(self):
        """Renouvelle la session comme le fait le navigateur (jeton de renouvellement Supabase)."""
        url = f"{self.supabase_url}/auth/v1/token"
        self._last_refresh = time.time()
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
            raise ApiError(f"renouvellement de session impossible (réseau : {type(e).__name__})", fatal=True) from None
        if resp.status_code in (400, 401, 403):
            # Définitif : --loop s'arrête au lieu de réessayer à chaque cycle.
            self.session_lost = ("session révoquée ou expirée (déconnexion, ou session partagée avec un navigateur) : "
                                 "relancez « python wikimasters.py login ».")
            raise ApiError(self.session_lost, fatal=True)
        if not resp.ok:
            raise ApiError(f"renouvellement de session impossible (erreur {resp.status_code})", fatal=True)
        try:
            self._use_session(resp.json(), fresh=True)
        except SessionSaveError as e:
            raise ApiError(f"{e} : la nouvelle session est perdue, relancez « python wikimasters.py login ».",
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
            self.session_lost = "le site a renouvelé la session de façon illisible : relancez « python wikimasters.py login »."
            return
        try:
            self._use_session(session)
        except SessionSaveError as e:
            self.session_lost = f"{e} : la nouvelle session est perdue, relancez « python wikimasters.py login »."
        except (ValueError, KeyError, TypeError):
            self.session_lost = "le site a renvoyé une session invalide : relancez « python wikimasters.py login »."

    def _request(self, method, url, headers_fn, **kwargs):
        what = f"{method} {urlparse(url).path}"
        maybe_done = f" {MAYBE_DONE}" if method != "GET" else ""
        delays = GET_RETRY_DELAYS if method == "GET" else ()
        for attempt in range(len(delays) + 1):
            self.ensure_fresh()
            try:
                resp = self.http.request(
                    method, url, timeout=20, allow_redirects=False, headers=headers_fn(), **kwargs
                )
            except requests.RequestException as e:
                # Jamais str(e) : le message peut contenir les en-têtes, donc le cookie.
                error = ApiError(f"erreur réseau ({type(e).__name__}) sur {what}.{maybe_done}", fatal=True)
            else:
                self._adopt_server_cookies(resp)
                if not (resp.status_code == 429 or resp.status_code >= 500):
                    return self._parse(resp, what, maybe_done)
                error = self._status_error(resp, what, maybe_done)
            if attempt < len(delays):
                time.sleep(delays[attempt])
        raise error

    def _status_error(self, resp, what, maybe_done):
        is_json = resp.headers.get("content-type", "").startswith("application/json")
        detail = f" : {resp.text[:200]}" if is_json and resp.text else ""
        if resp.status_code == 429:
            setting = "safety.read_delay_seconds" if what.startswith("GET") else "safety.delay_seconds"
            return ApiError(f"trop de requêtes (429) : augmentez {setting} et réessayez plus tard.", True, 429)
        return ApiError(f"erreur {resp.status_code} sur {what}{detail}", status=resp.status_code)

    def _parse(self, resp, what, maybe_done):
        is_json = resp.headers.get("content-type", "").startswith("application/json")
        detail = f" : {resp.text[:200]}" if is_json and resp.text else ""
        if 300 <= resp.status_code < 400:
            raise ApiError(f"redirection {resp.status_code} sur {what} (session refusée ?)", fatal=True)
        if resp.status_code == 403 and is_json and _json_code(resp) == "42501":
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
                "Session refusée ou blocage Cloudflare : relancez « python wikimasters.py login ».",
                fatal=True,
                status=resp.status_code,
            )
        if not resp.ok:
            raise ApiError(f"erreur {resp.status_code} sur {what}{detail}", status=resp.status_code)
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
            time.sleep(self.page_delay)
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
        return self._api("POST", f"/api/user-cards/{card.copy_id}/discard")

    def get_auction(self, auction_id):
        return self._api("GET", f"/api/marketplace/{auction_id}")

    def create_auction(self, card, base_amount, duration_minutes):
        # Le champ s'appelle "card_id" mais attend bien l'id de l'EXEMPLAIRE (vérifié dans le HAR).
        body = {"card_id": card.copy_id, "base_amount": base_amount, "duration_minutes": duration_minutes}
        return self._api("POST", "/api/marketplace", json=body)

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
            )
            if not (isinstance(created, list) and created and isinstance(created[0], dict) and created[0].get("id")):
                self._tag_ids = None  # relire les étiquettes au prochain usage
                raise ApiError(f"étiquette « {display_tag(name)} » créée mais le site n'a pas renvoyé son identifiant")
            self._tag_ids[norm(name)] = created[0]["id"]
        return self._tag_ids[norm(name)]

    def add_tag(self, card, name, color):
        tag_id = self.tag_id(name, color)
        self._rest("POST", "user_card_tags", json={"user_card_id": card.copy_id, "tag_id": tag_id})
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
        )
        if not removed:  # PostgREST répond OK même si rien n'a été supprimé (droits, mauvais filtre)
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


class SalesState:
    """ventes.json : enchères lancées par le script, pour le bilan (vendue / invendue) et les relances.

    Statuts : open (en cours), unsold (revenue sans acheteur), abandoned (trop d'essais, on ne la vend plus) ;
    les autres (sold, cancelled, relisted, gone) sont terminés et retirés du fichier à l'enregistrement.
    """

    KEPT = ("open", "unsold", "abandoned")

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
            print(f"Attention : {self.path.name} illisible, mis de côté dans {backup.name} : le suivi des ventes repart à zéro.")

    def with_status(self, status):
        return [(aid, e) for aid, e in self.auctions.items() if e.get("status") == status]

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

    def set_status(self, auction_id, status, **extra):
        if auction_id in self.auctions:
            self.auctions[auction_id].update(status=status, **extra)

    def save(self):
        keep = {a: e for a, e in self.auctions.items() if e.get("status") in self.KEPT}
        try:
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".ventes.", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"auctions": keep}, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except OSError as e:
            print(f"Attention : suivi des ventes non enregistré ({type(e).__name__}) : le bilan et les relances seront incomplets.")


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
    collection_ids: set = field(default_factory=set)  # tous les exemplaires de la collection, lignes groupées comprises
    on_sale: set = field(default_factory=set)  # exemplaires dont l'enchère est confirmée en cours


# --- Commandes --------------------------------------------------------------


FAILED = object()
FINAL_ACTIONS = ("discard", "auction")


def new_summary():
    counters = ("tag_add", "tag_remove", "discard", "auction", "relist", "sold", "unsold", "no_slot", "error",
                "deferred")
    # Ensembles d'exemplaires : une carte vue dans plusieurs phases de « tout » n'est comptée qu'une fois.
    sets = ("protect", "skip", "up_to_date", "read_error")
    return {**{k: 0 for k in counters}, **{k: set() for k in sets}}


def read_values(client, cards, cfg, cache, use_cache=True):
    """Prix moyen de chaque carte (par card_id) ; FAILED si illisible. Lecture seule.

    use_cache=False relit le prix sur le site (garde-fou avant une action irréversible).
    """
    safety = cfg["safety"]
    values, failures_in_a_row = {}, 0
    distinct = list(dict.fromkeys(c.card_id for c in cards))
    if not distinct:
        return values
    fresh = "" if use_cache else ", prix actuels relus sur le site"
    print(f"  lecture des prix de {len(distinct)} carte(s) (aucune modification{fresh})…")
    for card in cards:
        if card.card_id in values:
            continue
        if len(values) and len(values) % 50 == 0:
            print(f"  … {len(values)}/{len(distinct)}")
        cached = cache.get(card) if use_cache else PriceCache.MISSING
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
            time.sleep(random.uniform(delay * 0.8, delay * 1.5))
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


def filter_candidates(ctx, cards, tags, other_tags, what, require_existing=False):
    """Exemplaires portant une des étiquettes `tags`, hors protections et étiquettes contradictoires.

    require_existing : l'étiquette devait déjà être sur le site au début du passage (pas posée à l'instant par
    « analyser » dans « tout ») : vous avez ainsi le temps de la vérifier avant une action irréversible.
    """
    out = []
    for card in cards:
        if not card.has_any_tag(tags):
            continue
        if require_existing and not card.has_any_tag(tags, initial_only=True):
            ctx.summary["skip"].add(card.copy_id)
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
            time.sleep(ctx.cfg["safety"]["read_delay_seconds"])
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
    candidates = filter_candidates(ctx, cards, sell["tags"], cfg["discard"]["tags"], "à vendre",
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
    values = read_values(ctx.client, candidates, cfg, ctx.cache, use_cache=not sell["fresh_price"])
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
    """Défausses : cartes étiquetées à défausser, après vérification du prix actuel."""
    cfg, summary = ctx.cfg, ctx.summary
    disc = cfg["discard"]
    candidates = filter_candidates(ctx, cards, disc["tags"], cfg["sell"]["tags"], "à défausser",
                                   disc["require_existing_tag"])
    values = read_values(ctx.client, candidates, cfg, ctx.cache, use_cache=not disc["fresh_price"])
    count_read_errors(candidates, values, summary)
    steps = []
    for card in candidates:
        value = values.get(card.card_id)
        if value is FAILED:
            continue
        if value is None and disc["unknown_price"] == "skip":
            summary["skip"].add(card.copy_id)
            print(f"  ignorée   {label(card, value)} : jamais vendue, prix inconnu (discard.unknown_price)")
            continue
        if value is not None and disc["max_value"] is not None and value >= disc["max_value"]:
            summary["skip"].add(card.copy_id)
            print(f"  ignorée   {label(card, value)} : vaut maintenant {value:g} (>= {disc['max_value']:g}), "
                  "relancez analyser")
            continue
        steps.append((card, "discard", None, value))
    steps.sort(key=lambda s: -1 if s[3] is None else s[3])  # les moins chères d'abord
    if len(steps) > disc["max_per_run"]:
        summary["deferred"] += len(steps) - disc["max_per_run"]
        print(f"  {len(steps)} défausses prévues : {disc['max_per_run']} au plus par passage (discard.max_per_run)")
        steps = steps[:disc["max_per_run"]]
    return steps


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
                    self.errors_in_a_row += 1
                    failed.add(card.copy_id)  # on n'enchaîne pas les autres actions prévues pour cette carte
                    print(f"     ÉCHEC : {e}")
                    if self.errors_in_a_row >= self.cfg["safety"]["max_consecutive_errors"]:
                        raise ApiError(f"{self.errors_in_a_row} erreurs consécutives", fatal=True)
                    continue
            self._apply(card, kind, arg)
            # Compté tout de suite : un Ctrl+C pendant la pause ne doit pas faire disparaître l'action du résumé.
            self.summary[{"add_tag": "tag_add", "remove_tag": "tag_remove"}.get(kind, kind)] += 1
            if kind == "auction" and arg.attempt > 1:
                self.summary["relist"] += 1
            self.actions += 1
            if self.execute:
                self.errors_in_a_row = 0
                print(f"     {result}")
                time.sleep(self.cfg["safety"]["delay_seconds"])
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


def run_command(ctx, command):
    cards, warning = ctx.client.list_cards()
    ctx.collection_ids = {c.copy_id for c in cards}
    ctx.on_sale = set()  # relu par le bilan à chaque passage (sinon --loop ne relancerait jamais une carte)
    print(f"{len(cards)} exemplaire(s) dans la collection.")
    if warning:
        print(f"Attention : {warning}")
    # Une ligne ×2, ×3… : on ne sait pas à quel exemplaire s'appliquent étiquettes et défausse, on n'y touche pas.
    grouped = [c for c in cards if c.count > 1]
    if grouped:
        print(f"  {len(grouped)} ligne(s) à plusieurs exemplaires laissée(s) de côté (le script n'y touche pas).")
        if ctx.verbose:
            for card in grouped:
                print(f"  groupée   {card.name} [{card.rarity}] ×{card.count}")
        cards = [c for c in cards if c.count <= 1]

    runner = Runner(ctx)
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
            steps = plan(ctx, cards)
            if not steps:
                print("  rien à faire")
            elif not runner.run(steps):
                skipped = [t for t, _ in phases[n + 1:]]
                if skipped:
                    print(f"  Étape(s) non lancée(s) pour cette raison : {', '.join(skipped)}.")
                return
            cards = [c for c in cards if c.copy_id not in runner.consumed and c.copy_id not in runner.failed]
    finally:
        ctx.cache.save()  # même en dry-run : un --execute juste après ne relit pas tous les prix
        if ctx.execute:
            ctx.state.save()


LOGIN_STEPS = """Connexion unique du script.
  1. Ouvrez une fenêtre de NAVIGATION PRIVÉE et connectez-vous sur https://www.wiki-masters.com.
  2. Outils développeur (Cmd+Option+I ou F12) > onglet Network.
  3. Allez sur la page Collection, tapez my-collection dans le filtre, puis rechargez la page.
  4. Cliquez sur la requête my-collection (GET, www.wiki-masters.com).
  5. Dans Request Headers, clic droit sur « cookie » > Copy value.
  6. Collez ci-dessous puis Entrée (rien ne s'affiche, c'est normal).
  7. Fermez ensuite la fenêtre privée SANS vous déconnecter.
"""


def cmd_login(cfg, store):
    print(LOGIN_STEPS)
    raw = read_secret("Cookie : ")
    ref = urlparse(cfg["site"]["supabase_url"]).netloc.split(".", 1)[0]
    try:
        session = supabase_session(parse_cookie_header(normalize_cookie_header(raw)), ref)
        jwt_claims(session["access_token"])
    except ValueError as e:
        sys.exit(f"Cookie invalide : {e}. Recommencez en copiant toute la valeur de l'en-tête cookie.")
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
    print(f"Session enregistrée pour {client.username} dans {store.path} ({slots} place(s) d'enchère libre(s)).")
    print("Vous pouvez maintenant lancer : python wikimasters.py analyser")


def load_secrets(path):
    """secrets.yaml : identifiants personnels (Telegram), à côté de config.yaml et jamais dans git. {} s'il est absent."""
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        return {}
    except OSError as e:
        sys.exit(f"Impossible de lire {path} ({type(e).__name__}).")
    except yaml.YAMLError:
        sys.exit(f"{path} est mal écrit (indentation, guillemets, deux-points ?).")
    if data is None:
        return {}
    if not isinstance(data, dict):
        sys.exit(f"{path} est mal écrit : il doit contenir une section telegram (voir le README).")
    return data


def telegram_settings(cfg, secrets_path):
    """Réglages Telegram avec le jeton lu dans secrets.yaml, ou None si les notifications sont désactivées."""
    tg = cfg.get("telegram") or {}
    if not tg.get("enabled"):
        return None
    secret = load_secrets(secrets_path).get("telegram")
    secret = secret if isinstance(secret, dict) else {}
    token, chat_id = secret.get("bot_token"), secret.get("chat_id")
    if not isinstance(token, str) or not token.strip() or not isinstance(chat_id, (str, int)) or not str(chat_id).strip():
        print(f"Attention : telegram.enabled vaut true, mais {secrets_path.name} n'a pas telegram.bot_token et "
              "telegram.chat_id : pas de notification (voir le README).")
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
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("login",) + COMMANDS, help="ce que le script doit faire")
    parser.add_argument("--config", default="config.yaml", help="fichier de règles (par défaut config.yaml)")
    parser.add_argument("--execute", action="store_true", help="appliquer réellement les actions (sinon dry-run)")
    parser.add_argument("--verbose", action="store_true", help="tout afficher (cartes protégées, en attente…)")
    parser.add_argument("--fresh", action="store_true",
                        help="relire tous les prix sur le site au lieu du cache (pour actualiser les étiquettes)")
    parser.add_argument("--loop", type=loop_minutes, metavar="MINUTES", help="relancer la commande toutes les N minutes")
    args = parser.parse_args()

    cfg = load_config(args.config)
    check_config(cfg)
    store = SessionStore(config_path(args.config, cfg["site"]["session_file"]))
    if args.command == "login":
        try:
            cmd_login(cfg, store)
        except KeyboardInterrupt:
            sys.exit("\nConnexion annulée.")
        return

    client = WikiMastersClient(cfg["site"], store, page_delay=cfg["safety"]["read_delay_seconds"])
    telegram = telegram_settings(cfg, config_path(args.config, SECRETS_FILE))
    journal_cfg = cfg["journal"]
    ctx = Context(
        client=client,
        cfg=cfg,
        execute=args.execute,
        summary=new_summary(),
        cache=PriceCache(config_path(args.config, cfg["price"]["cache_file"]), cfg["price"]["cache_hours"]),
        verbose=args.verbose or cfg.get("display", {}).get("verbose", False),
        # Le journal n'enregistre que les actions réelles : rien en dry-run.
        journal=Journal(config_path(args.config, journal_cfg["file"]), journal_cfg["delimiter"],
                        journal_cfg["enabled"] and args.execute, client.username, args.command),
        state=SalesState(config_path(args.config, cfg["sell"]["state_file"])),
    )
    mode = "EXÉCUTION RÉELLE" if args.execute else "DRY-RUN (aucune modification)"
    lock = RunLock(config_path(args.config, cfg["sell"]["state_file"]).with_name(".wikimasters.lock"))
    if args.execute:
        lock.acquire()

    outcome = ("ok", None)
    try:
        while True:
            # Un bilan par cycle de --loop.
            ctx.summary = new_summary()
            ctx.journal.lost = ctx.journal.diverted = 0
            ctx.journal.merge_backup()
            print(f"\n=== [{time.strftime('%H:%M:%S')}] {args.command} — {mode} — compte {client.username} ===")
            if args.fresh:
                ctx.cache.not_before = time.time()
                print("Option --fresh : tous les prix sont relus sur le site (cache ignoré).")
            outcome = ("ok", None)
            try:
                run_command(ctx, args.command)
            except ApiError as e:
                outcome = ("error", str(e))
                print(f"\nARRÊT : {e}")
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

            if client.session_lost:
                if client.session_lost != outcome[1]:  # sinon déjà affiché par « ARRÊT »
                    print(f"\nAttention : {client.session_lost}")
                sys.exit(1)
            if outcome[0] == "error" and not args.loop:
                sys.exit(1)
            if not args.loop:
                break
            print(f"\nProchain passage dans {args.loop} minute(s)… (Ctrl+C pour quitter)")
            time.sleep(args.loop * 60)
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
    others = [
        f"{len(summary['protect'])} protégée(s)",
        f"{len(summary['skip'] - summary['protect'])} ignorée(s)",
        f"{summary['no_slot']} en attente d'une place d'enchère",
        f"{summary['error']} échec(s)",
    ]
    if summary["sold"] or summary["unsold"]:
        others.insert(0, f"ventes passées : {summary['sold']} vendue(s), {summary['unsold']} invendue(s)")
    if summary["up_to_date"]:
        others.insert(0, f"{len(summary['up_to_date'])} déjà à jour")
    if summary["read_error"]:
        others.append(f"{len(summary['read_error'])} prix illisible(s)")
    if summary["deferred"]:
        others.append(f"{summary['deferred']} action(s) reportée(s) au prochain passage")
    summary_text = f"Résumé ({verb}) : {', '.join(parts)} | {', '.join(others)}"
    print(f"\n{summary_text}")
    return summary_text


if __name__ == "__main__":
    main()
