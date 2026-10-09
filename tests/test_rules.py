import base64
import copy
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unicodedata
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import requests
import yaml

import wikimasters as w

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
REF = "cyrxjeppjqsxxjayfrur"
USER_ID = "00000000-0000-4000-8000-000000000001"
FOREST, CHACANA, FACE, AMPHIBIA, HOTEL = (
    "00000000-0000-4000-8000-000000000101",
    "00000000-0000-4000-8000-000000000102",
    "00000000-0000-4000-8000-000000000103",
    "00000000-0000-4000-8000-000000000104",
    "00000000-0000-4000-8000-000000000105",
)
LYON_TAG = "00000000-0000-4000-8000-000000000201"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def b64(data):
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")


def make_session(exp_offset=3600, refresh="r0", now=None):
    exp = int((now or time.time()) + exp_offset)
    claims = {"sub": USER_ID, "exp": exp, "user_metadata": {"username": "testeur"}}
    token = f"{b64({'alg': 'HS256'})}.{b64(claims)}.sig-{refresh}"
    return {"access_token": token, "refresh_token": refresh, "expires_at": exp, "user": {"id": USER_ID}}


def fake_cookie(exp_offset=3600, chunked=True, session=None):
    value = "base64-" + b64(session or make_session(exp_offset))
    if not chunked:
        return f"other=1; sb-{REF}-auth-token={value}"
    half = len(value) // 2
    return f"sb-{REF}-auth-token.1={value[half:]}; other=1; sb-{REF}-auth-token.0={value[:half]}"


class FakeResponse:
    def __init__(self, status, payload, content_type="application/json", set_cookies=None):
        self.status_code, self._payload = status, payload
        self.ok = 200 <= status < 300
        self.headers = {"content-type": content_type}
        self.cookies = requests.cookies.RequestsCookieJar()
        for name, value in (set_cookies or {}).items():
            self.cookies.set(name, value)
        if content_type == "application/json":
            self.text = "" if payload is None else json.dumps(payload)
        else:
            self.text = payload
        self.content = self.text.encode()

    def json(self):
        return self._payload


class Clock:
    def __init__(self):
        self.now = time.time()

    def __call__(self):
        return self.now


class FakeSite:
    """Simule le site et Supabase. overrides : (méthode, suffixe d'URL) -> réponse, exception ou liste."""

    def __init__(self, pages, values, selling=0):
        self.pages, self.values = pages, values
        self.selling = selling if isinstance(selling, list) else [selling]
        self.calls, self.overrides, self.refreshes = [], {}, 0
        self.account_tags = {"lyon": LYON_TAG}
        self.auctions = {}  # auction_id -> réponse de GET /api/marketplace/<id>
        self.delete_returns_rows = True
        self.auction_count = 0
        self.on_post = None
        self.telegram = []  # (url, corps) des notifications

    def __call__(self, method, url, timeout=None, params=None, json=None, headers=None, allow_redirects=True,
                 **kwargs):
        if "api.telegram.org" in url:
            self.telegram.append((url, json))
            return FakeResponse(200, {"ok": True})
        self.calls.append((method, url, json, headers or {}, params or {}))
        for (m, suffix), result in self.overrides.items():
            if m == method and url.split("?")[0].endswith(suffix):
                if isinstance(result, list):
                    result = result.pop(0) if len(result) > 1 else result[0]
                if isinstance(result, BaseException):
                    raise result
                if result is not None:
                    return result
        if method == "POST" and self.on_post and "/auth/" not in url:
            self.on_post()
        path = url.split(".com", 1)[1] if ".com" in url else url.split(".co", 1)[1]
        if path == "/auth/v1/token":
            self.refreshes += 1
            return FakeResponse(200, make_session(3600, refresh=f"r{self.refreshes}", now=time.time()))
        if path == "/api/my-collection":
            page = params["page"]
            return FakeResponse(200, self.pages[page] if page < len(self.pages) else {"collection": []})
        if path.startswith("/api/marketplace/cards/"):
            card_id = path.split("/")[4]
            items = [i for p in self.pages for i in p["collection"]]
            rarity = next(i["card"]["rarity"] for i in items if i["card_id"] == card_id)
            value = self.values.get(card_id)
            return FakeResponse(200, {"summary": {} if value is None else {rarity: {"average": value}}})
        if path == "/api/marketplace/mine":
            selling = self.selling.pop(0) if len(self.selling) > 1 else self.selling[0]
            return FakeResponse(200, {"sellingCount": selling, "maxConcurrentAuctions": 5})
        if path == "/api/marketplace" and method == "POST":
            self.auction_count += 1
            suffix = "" if self.auction_count <= 2 else f"-{self.auction_count}"
            return FakeResponse(201, {"auction_id": f"auction-{json['card_id'][-3:]}{suffix}"})
        if path.endswith("/discard"):
            return FakeResponse(200, {"balance": 100})
        if path == "/rest/v1/tags" and method == "GET":
            return FakeResponse(200, [{"id": i, "name": n} for n, i in self.account_tags.items()])
        if path == "/rest/v1/tags" and method == "POST":
            tag_id = f"t-{json['name']}"
            self.account_tags[json["name"]] = tag_id
            return FakeResponse(201, [{"id": tag_id, "name": json["name"]}])
        if path == "/rest/v1/user_card_tags":
            if method == "POST":
                return FakeResponse(201, None)
            rows = [{"user_card_id": params["user_card_id"][3:], "tag_id": params["tag_id"][3:]}]
            return FakeResponse(200, rows if self.delete_returns_rows else [])
        if path.startswith("/api/marketplace/auction-") and method == "GET":
            return FakeResponse(200, self.auctions.get(path.split("/")[-1], {"status": "active"}))
        raise AssertionError(f"requête inattendue {method} {url}")

    def mutations(self):
        return [(m, u, b) for m, u, b, _, _ in self.calls if m != "GET" and "/auth/" not in u]

    def discards(self):
        return [u.split("/")[-2] for m, u, _ in self.mutations() if u.endswith("/discard")]

    def sells(self):
        return [b["card_id"] for m, u, b in self.mutations() if u.endswith("/api/marketplace")]

    def sell_bodies(self):
        return [b for m, u, b in self.mutations() if u.endswith("/api/marketplace")]

    def tag_links(self):
        return [(b["user_card_id"], b["tag_id"]) for m, u, b in self.mutations() if u.endswith("/user_card_tags")
                and m == "POST"]

    def tag_unlinks(self):
        return [(p["user_card_id"], p["tag_id"]) for m, u, _, _, p in self.calls if m == "DELETE"]

    def gets(self, fragment):
        return [u for m, u, _, _, _ in self.calls if m == "GET" and fragment in u]


def load_cfg():
    """Config de référence des tests : vos réglages dans config.yaml ne changent pas les résultats."""
    return yaml.safe_load((FIXTURES / "config.yaml").read_text(encoding="utf-8"))


def standard_site(selling=0, tags=None):
    """Forest Hills (étiquette « lyon ») vaut 3, Chacana 5, Face visible 12, Amphibia 60, Hotel California : jamais vendue.

    tags : {copy_id: [noms]} ajoutés aux exemplaires (comme posés par « analyser » ou à la main).
    """
    data = load("collection.json")
    ids = [i["card_id"] for i in data["collection"]]
    for item in data["collection"]:
        for name in (tags or {}).get(item["id"], []):
            item["tags"].append({"id": f"t-{name}", "name": name, "color": "#000"})
    site = FakeSite([data], {ids[0]: 3, ids[1]: 5, ids[2]: 12, ids[3]: 60}, selling)
    for names in (tags or {}).values():
        for name in names:
            site.account_tags.setdefault(name, f"t-{name}")
    return site


SORTED = {CHACANA: ["defausse"], FACE: ["+10"], AMPHIBIA: ["+10"]}


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.cfg = load_cfg()

    def card(self, **kw):
        base = dict(copy_id="copy", card_id="c1", name="Carte", rarity="C")
        base.update(kw)
        return w.Card(**base)

    def test_parse_collection_uses_copy_id_tag_names_and_ids(self):
        forest = w.parse_collection_page(load("collection.json"))[0]
        self.assertEqual((forest.copy_id, forest.card_id), (FOREST, "5c7df689-bbae-45bf-8d26-9d126ad01be2"))
        self.assertEqual((forest.tags, forest.tag_ids, forest.count), (["lyon"], {"lyon": LYON_TAG}, 1))

    def test_pending_trade_is_protected(self):
        data = load("collection.json")
        data["pendingTradeCardIds"] = [FOREST]
        self.assertEqual(w.protection_reason(w.parse_collection_page(data)[0], self.cfg["protection"]),
                         "échange en cours")

    def test_value_from_sales(self):
        self.assertEqual(w.value_from_sales(load("sales.json"), self.card(rarity="R")), 6.0)
        self.assertIsNone(w.value_from_sales(load("sales.json"), self.card(rarity="SR")))
        self.assertIsNone(w.value_from_sales({"summary": {}}, self.card()))

    def test_protection_reasons(self):
        prot = dict(self.cfg["protection"], name_contains=["Olympique"])
        cases = {
            "étiquette lyon": self.card(tags=["#Lyon "]),
            "favori": self.card(starred=True),
            "shiny": self.card(is_shiny=True),
            "rareté L": self.card(rarity="L"),
            "rareté SR": self.card(rarity="SR"),
            "nom contient « Olympique »": self.card(name="Olympique lyonnais"),
        }
        for reason, card in cases.items():
            self.assertEqual(w.protection_reason(card, prot), reason)
        self.assertIsNone(w.protection_reason(self.card(tags=["+10"]), prot))

    def test_protected_tag_tolerates_spaces_unicode_and_hash(self):
        nfd = unicodedata.normalize("NFD", "à garder")
        for tag in ["à garder ", " À GARDER", "à\xa0garder", "#à  garder", nfd]:
            prot = dict(self.cfg["protection"], tags=["à garder"])
            self.assertIsNotNone(w.protection_reason(self.card(tags=[tag]), prot), repr(tag))
        for protected in [" À Garder ", nfd, "#à garder"]:
            prot = dict(self.cfg["protection"], tags=[protected])
            self.assertIsNotNone(w.protection_reason(self.card(tags=["à garder"]), prot), repr(protected))

    def test_bands_edges(self):
        bands = self.cfg["price_tags"]["bands"]
        for value, tag in ((0, "defausse"), (9.99, "defausse"), (10, "+10"), (99.99, "+10"), (100, "+100"),
                           (499, "+100"), (500, "+500"), (999.5, "+500"), (1000, "+1000"), (10 ** 6, "+1000")):
            self.assertEqual(w.band_for(value, bands)["tag"], tag, value)

    def test_tag_changes(self):
        self.assertEqual(w.tag_changes(self.card(), 5, self.cfg), (["defausse"], []))
        self.assertEqual(w.tag_changes(self.card(tags=["defausse"]), 5, self.cfg), ([], []))
        self.assertEqual(w.tag_changes(self.card(tags=["#+100", "perso"]), 12, self.cfg), (["+10"], ["#+100"]))
        # Prix inconnu : aucune étiquette, et l'ancienne n'est pas retirée.
        self.assertEqual(w.tag_changes(self.card(tags=["+10"]), None, self.cfg), ([], []))
        cfg = copy.deepcopy(self.cfg)
        cfg["price_tags"].update(unknown_tag="inconnu", remove_outdated=False)
        self.assertEqual(w.tag_changes(self.card(tags=["+10"]), None, cfg), (["inconnu"], []))
        self.assertEqual(w.tag_changes(self.card(tags=["+10"]), 600, cfg), (["+500"], []))
        cfg["auto_tags"] = [{"tag": "à surveiller", "when": {"min_value": 50, "rarity_in": ["C"]}}]
        self.assertEqual(w.tag_changes(self.card(), 60, cfg), (["+10", "à surveiller"], []))

    def test_start_price(self):
        sell = self.cfg["sell"]
        self.assertEqual(w.start_price(60, sell), 45)
        self.assertEqual(w.start_price(41, sell), 30)
        self.assertEqual(w.start_price(1, sell), 1)
        self.assertEqual(w.start_price(100, dict(sell, price_factor=0.29)), 29)  # pas 28 à cause des flottants
        self.assertEqual(w.start_price(41, dict(sell, rounding="ceil")), 31)
        self.assertEqual(w.start_price(41, dict(sell, rounding="round")), 31)
        self.assertEqual(w.start_price(10000, dict(sell, max_start_price=500)), 500)

    def test_accents_are_ignored_in_tag_names(self):
        for a, b in (("a garder", "À garder"), ("defausse", "#Défausse"), ("equipe", "Équipe ")):
            self.assertEqual(w.norm(a), w.norm(b))
        prot = dict(self.cfg["protection"], tags=["a garder"])
        self.assertIsNotNone(w.protection_reason(self.card(tags=["à garder"]), prot))
        self.assertEqual(w.display_tag(" #+10 "), "+10")

    def test_protecting_auto_tag_replaces_price_tag(self):
        cfg = copy.deepcopy(self.cfg)
        cfg["auto_tags"] = [{"tag": "garder", "when": {"name_contains": "Lyon"}},
                            {"tag": "info", "when": {"max_value": 5}}]
        self.assertEqual(w.tag_changes(self.card(name="Gare de Lyon"), 3, cfg), (["garder", "info"], []))
        self.assertEqual(w.tag_changes(self.card(name="Paris"), 3, cfg), (["defausse", "info"], []))

    def test_round_is_half_up_and_nan_is_unknown(self):
        sell = dict(self.cfg["sell"], rounding="round", price_factor=0.25)
        self.assertEqual((w.start_price(10, sell), w.start_price(14, sell)), (3, 4))  # 2,5 → 3 ; 3,5 → 4
        for bad in (float("nan"), float("inf"), -3):
            self.assertIsNone(w.value_from_sales({"summary": {"C": {"average": bad}}}, self.card()))


class ConfigTest(unittest.TestCase):
    def assert_rejected(self, mutate, fragment=None):
        cfg = load_cfg()
        mutate(cfg)
        with self.assertRaises(SystemExit) as ctx:
            w.check_config(cfg)
        if fragment:
            self.assertIn(fragment, str(ctx.exception.code))

    def test_shipped_config_is_valid(self):
        w.check_config(load_cfg())
        w.check_config(yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8")))  # la vôtre aussi

    def test_all_site_durations_are_accepted(self):
        for minutes in (10, 30, 60, 180, 360, 720):
            cfg = load_cfg()
            cfg["sell"]["duration_minutes"] = minutes
            w.check_config(cfg)

    def test_scalar_instead_of_list_is_rejected(self):
        self.assert_rejected(lambda c: c["protection"].update(tags="garder"), "protection.tags")
        self.assert_rejected(lambda c: c["discard"].update(tags="defausse"), "discard.tags")
        self.assert_rejected(lambda c: c.update(auto_tags=[{"tag": "x", "when": {"rarity_in": "SR"}}]), "rarity_in")

    def test_bands_must_follow_each_other(self):
        self.assert_rejected(lambda c: c["price_tags"]["bands"][1].update({"from": 20}), "ni trou")
        self.assert_rejected(lambda c: c["price_tags"]["bands"][1].update({"from": 5}), "ni trou")
        self.assert_rejected(lambda c: c["price_tags"]["bands"][2].pop("from"), "from manquant")
        self.assert_rejected(lambda c: c["price_tags"]["bands"][2].update(tag="+10"), "différente")
        self.assert_rejected(lambda c: c["price_tags"]["bands"][0].update(colour="x"), "inconnue")

    def test_contradictory_tags_are_rejected(self):
        self.assert_rejected(lambda c: c["discard"]["tags"].append("Lyon"), "en commun")
        self.assert_rejected(lambda c: c["sell"]["tags"].append("defausse"), "en commun")

    def test_other_invalid_values_are_rejected(self):
        self.assert_rejected(lambda c: c["sell"].update(duration_minutes=1440))
        self.assert_rejected(lambda c: c["sell"].update(max_auctions=6), "limite du site")
        self.assert_rejected(lambda c: c["sell"].update(price_factor=0))
        self.assert_rejected(lambda c: c["sell"].update(rounding="bas"))
        self.assert_rejected(lambda c: c["sell"].update(min_start_price=0))
        self.assert_rejected(lambda c: c["discard"].update(unknown_rarities="C"))
        self.assert_rejected(lambda c: c.update(auto_tags=[{"tag": "x", "when": {"rarete": ["SR"]}}]), "inconnue")
        self.assert_rejected(lambda c: c.pop("safety"), "safety")
        self.assert_rejected(lambda c: c["safety"].update(max_consecutive_errors=0))
        self.assert_rejected(lambda c: c["price"].update(cache_hours=-1))

    def test_auto_tags_must_be_well_formed(self):
        self.assert_rejected(lambda c: c.update(auto_tags=[{"tag": "x", "whne": {"max_value": 2}}]), "inconnue")
        self.assert_rejected(lambda c: c.update(auto_tags=[{"tag": "x", "max_value": 2}]), "inconnue")
        self.assert_rejected(lambda c: c.update(auto_tags=[{"tag": "x"}]), "au moins une condition")
        self.assert_rejected(lambda c: c.update(auto_tags=[{"tag": "+100", "when": {"min_value": 1}}]), "étiquette de prix")
        self.assert_rejected(lambda c: (c["discard"]["tags"].append("poubelle"),
                                        c.update(auto_tags=[{"tag": "poubelle", "when": {"max_value": 2}}])),
                             "défausse ne peut pas")

    def test_guards_must_match_bands(self):
        self.assert_rejected(lambda c: c["price_tags"]["bands"][0].update(below=20) or
                             c["price_tags"]["bands"][1].update({"from": 20}), "jamais défaussées")
        self.assert_rejected(lambda c: c["sell"].update(min_value=50), "jamais vendues")
        self.assert_rejected(lambda c: c["sell"].update(min_start_price=50, max_start_price=20), "max_start_price")
        self.assert_rejected(lambda c: (c["price_tags"].update(unknown_tag="poubelle"), c["discard"]["tags"].append("poubelle")),
                             "unknown_tag")
        self.assert_rejected(lambda c: c["sell"]["relist"].update(factor=1.2), "relance")
        self.assert_rejected(lambda c: c["journal"].update(delimiter="|"), "journal.delimiter")

    def test_perso_yaml_completes_config_and_holds_the_telegram_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, path = load_cfg(), pathlib.Path(tmp) / "config.yaml"
            cfg["telegram"]["bot_token"] = "123:abc"
            path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
            with self.assertRaises(SystemExit) as ctx:  # le jeton ne va jamais dans le fichier partagé
                w.load_config(path)
            self.assertIn("perso.yaml", str(ctx.exception.code))
            del cfg["telegram"]["bot_token"]
            path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
            (pathlib.Path(tmp) / "perso.yaml").write_text(
                "telegram:\n  bot_token: '123:abc'\n  chat_id: 42\nprotection:\n  tags: [lyon]\n", encoding="utf-8")
            merged = w.load_config(path)
            w.check_config(merged)
            self.assertEqual((merged["telegram"]["bot_token"], merged["protection"]["tags"]), ("123:abc", ["lyon"]))
            self.assertTrue(merged["protection"]["starred"])  # le reste de la section vient de config.yaml
            (pathlib.Path(tmp) / "perso.yaml").write_text("- a\n", encoding="utf-8")
            with self.assertRaises(SystemExit) as ctx:
                w.load_config(path)
            self.assertIn("perso.exemple.yaml", str(ctx.exception.code))
        self.assert_rejected(lambda c: c["telegram"].update(chat="42"), "inconnue")
        self.assert_rejected(lambda c: c["telegram"].update(bot_token=123), "bot_token")

    def test_unknown_rarities_need_the_unknown_tag_and_unprotected_rarities(self):
        self.assert_rejected(lambda c: c["discard"].update(unknown_rarities=["C"]), "unknown_tag")
        self.assert_rejected(lambda c: (c["price_tags"].update(unknown_tag="inconnu"),
                                        c["discard"].update(unknown_rarities=["C", "SR"])), "protégée")

    def test_broken_yaml_gives_friendly_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            for content, fragment in (("", "vide"), ("- a\n- b", "vide"), ("site:\n  a: [1,\n", "mal écrit")):
                path = pathlib.Path(tmp) / "c.yaml"
                path.write_text(content, encoding="utf-8")
                with self.assertRaises(SystemExit) as ctx:
                    w.load_config(path)
                self.assertIn(fragment, str(ctx.exception.code))
            with self.assertRaises(SystemExit):
                w.load_config(pathlib.Path(tmp) / "absent.yaml")


class CookieTest(unittest.TestCase):
    def session_of(self, header):
        return w.supabase_session(w.parse_cookie_header(w.normalize_cookie_header(header)), REF)

    def test_chunked_and_plain(self):
        for chunked in (True, False):
            self.assertEqual(w.jwt_claims(self.session_of(fake_cookie(chunked=chunked))["access_token"])["sub"], USER_ID)

    def test_messy_paste(self):
        raw = fake_cookie()
        cut = raw.index("; ") + 2
        messy = "Cookie: " + raw[:cut] + "\n" + raw[cut:] + "\r\n"
        self.assertEqual(self.session_of(messy)["refresh_token"], "r0")

    def test_missing_chunk_garbage_or_no_refresh_token(self):
        only_second = "; ".join(p for p in fake_cookie().split("; ") if "token.0=" not in p)
        no_refresh = fake_cookie(session={"access_token": make_session()["access_token"]})
        for header in (only_second, f"sb-{REF}-auth-token=base64-!!!", "a=1", no_refresh):
            with self.assertRaises(ValueError):
                self.session_of(header)

    def test_session_cookies_round_trip_and_chunking(self):
        session = make_session()
        session["user"]["padding"] = "x" * 8000
        cookies = w.session_cookies(session, REF)
        self.assertGreater(len(cookies), 1)
        self.assertTrue(all(len(v) <= w.COOKIE_CHUNK_SIZE for v in cookies.values()))
        self.assertEqual(w.supabase_session(cookies, REF), session)
        small = w.session_cookies(make_session(), REF)
        self.assertEqual(list(small), [f"sb-{REF}-auth-token"])


class SessionStoreTest(unittest.TestCase):
    def test_save_is_private_and_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = w.SessionStore(pathlib.Path(tmp) / "session.json")
            self.assertIsNone(store.load())
            store.save(make_session())
            self.assertEqual(os.stat(store.path).st_mode & 0o777, 0o600)
            self.assertEqual(store.load()["refresh_token"], "r0")
            store.path.write_text("{oops")
            with self.assertRaises(SystemExit):
                store.load()


class ReadSecretTest(unittest.TestCase):
    def test_reads_piped_stdin(self):
        with mock.patch("sys.stdin", io.StringIO("abc\n")):
            self.assertEqual(w.read_secret("x"), "abc\n")

    @unittest.skipUnless(hasattr(os, "openpty"), "pas de pseudo-terminal")
    def test_reads_more_than_1024_chars_from_a_terminal(self):
        # Le mode canonique de macOS tronque à 1024 caractères : read_secret doit le contourner.
        master, slave = os.openpty()
        code = "import sys, wikimasters as w; s = w.read_secret('>'); sys.stderr.write(str(len(s.strip())))"
        proc = subprocess.Popen([sys.executable, "-c", code], cwd=ROOT, stdin=slave, stdout=slave, stderr=subprocess.PIPE)
        os.close(slave)
        buf = b""
        while b">" not in buf:
            buf += os.read(master, 100)
        time.sleep(0.2)
        payload = b"a" * 5000
        for i in range(0, len(payload), 512):
            os.write(master, payload[i:i + 512])
        os.write(master, b"\n")
        _, err = proc.communicate(timeout=10)
        os.close(master)
        self.assertEqual(err.decode().strip()[-4:], "5000")


class EndToEndTest(unittest.TestCase):
    """Fait tourner main() contre un faux site, avec session et cache dans un dossier temporaire."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = pathlib.Path(self.tmp.name)
        self.acc = self.dir / "comptes" / "testeur"  # dossier du compte du jeton de test
        self.acc.mkdir(parents=True)
        self.store = w.SessionStore(self.acc / "session.json")

    def run_main(self, site, command, execute=False, session=True, mutate=None, stdin=None, clock=None, sleep=None,
                 args=(), perso=None):
        cfg = load_cfg()
        cfg["price"]["cache_file"] = str(self.dir / "prix.json")
        cfg["price"]["cache_hours"] = 0
        cfg["sell"]["order"] = "value"
        cfg["journal"]["file"] = str(self.dir / "journal.csv")
        if mutate:
            mutate(cfg)
        cfg_path = self.dir / "config.yaml"
        cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
        if session is True:
            self.store.save(make_session())
        elif session:
            self.store.save(session)
        if perso is not None:
            (self.dir / "perso.yaml").write_text(yaml.safe_dump(perso, allow_unicode=True), encoding="utf-8")
        argv = ["wikimasters.py", command, "--config", str(cfg_path)] + (["--execute"] if execute else []) + list(args)
        out, code = io.StringIO(), 0
        patches = [
            mock.patch.object(sys, "argv", argv),
            mock.patch("requests.Session.request", side_effect=site),
            mock.patch("time.sleep", side_effect=sleep),
            mock.patch.object(w, "keep_awake"),
            redirect_stdout(out),
        ]
        if clock:
            patches.append(mock.patch("time.time", side_effect=clock))
        if stdin is not None:
            patches.append(mock.patch("sys.stdin", io.StringIO(stdin)))
        try:
            for p in patches:
                p.__enter__()
            try:
                w.main()
            except SystemExit as e:
                code = e.code
        finally:
            for p in reversed(patches):
                p.__exit__(None, None, None)
        return out.getvalue(), code

    # --- analyser ---

    def test_analyse_dry_run_sends_nothing_and_shows_spread(self):
        site = standard_site()
        output, code = self.run_main(site, "analyser")
        self.assertEqual((site.mutations(), code), ([], 0))
        self.assertIn("répartition par prix moyen : defausse : 1, +10 : 2, jamais vendue : 1", output)
        self.assertIn("3 étiquette(s) posée(s)", output)
        self.assertIn("1 protégée(s)", output)
        self.assertEqual(site.gets("5c7df689"), [])  # carte protégée (lyon) : prix même pas lu

    def test_analyse_execute_creates_and_links_tags(self):
        site = standard_site()
        self.run_main(site, "analyser", execute=True)
        self.assertEqual(sorted(site.tag_links()), sorted([
            (CHACANA, "t-defausse"), (FACE, "t-+10"), (AMPHIBIA, "t-+10")]))
        created = [b["name"] for m, u, b in site.mutations() if u.endswith("/rest/v1/tags")]
        self.assertEqual(created, ["defausse", "+10"])  # chaque étiquette créée une seule fois
        self.assertEqual((site.discards(), site.sells()), ([], []))

    def test_analyse_replaces_outdated_price_tag(self):
        site = standard_site(tags={FACE: ["+100"]})
        self.run_main(site, "analyser", execute=True)
        self.assertEqual(site.tag_unlinks(), [(f"eq.{FACE}", "eq.t-+100")])
        self.assertIn((FACE, "t-+10"), site.tag_links())

    def test_analyse_can_tag_protected_cards_without_unprotecting_them(self):
        site = standard_site()
        self.run_main(site, "tout", execute=True, mutate=lambda c: c["price_tags"].update(tag_protected=True))
        self.assertIn((FOREST, "t-defausse"), site.tag_links())
        self.assertNotIn(FOREST, site.discards())

    # --- vendre ---

    def test_sell_most_valuable_first_at_75_percent(self):
        site = standard_site(tags=SORTED)
        self.run_main(site, "vendre", execute=True)
        self.assertEqual(site.sell_bodies(), [
            {"card_id": AMPHIBIA, "base_amount": 45, "duration_minutes": 30},
            {"card_id": FACE, "base_amount": 9, "duration_minutes": 30},
        ])
        self.assertEqual(site.discards(), [])

    def test_sell_respects_free_slots(self):
        site = standard_site(selling=4, tags=SORTED)
        output, _ = self.run_main(site, "vendre", execute=True)
        self.assertEqual(site.sells(), [AMPHIBIA])
        self.assertIn("en attente d'une place : Face visible de la Lune", output)

    def test_sell_slots_read_again_after_prices(self):
        site = standard_site(selling=[0, 4], tags=SORTED)
        self.run_main(site, "vendre", execute=True)
        urls = [c[1] for c in site.calls]
        last_mine = max(i for i, u in enumerate(urls) if u.endswith("/mine"))
        self.assertGreater(last_mine, max(i for i, u in enumerate(urls) if "/sales" in u))
        self.assertEqual(len(site.sells()), 1)  # 4 enchères en cours au moment de vendre : 1 place

    def test_sell_without_free_slot_reads_no_price(self):
        site = standard_site(selling=5, tags=SORTED)
        output, _ = self.run_main(site, "vendre", execute=True)
        self.assertEqual((site.gets("/sales"), site.sells()), ([], []))
        self.assertIn("aucune place d'enchère libre", output)

    def test_sell_random_order(self):
        site = standard_site(tags=SORTED)
        # Mélange simulé qui met la moins chère en premier : prouve que l'ordre ne suit plus le prix.
        with mock.patch("random.Random.shuffle", side_effect=lambda items: items.sort(key=lambda o: o[1])) as shuffle:
            self.run_main(site, "vendre", execute=True, mutate=lambda c: c["sell"].update(order="random"))
        self.assertTrue(shuffle.called)
        self.assertEqual(site.sells(), [FACE, AMPHIBIA])

    def test_sell_order_by_tags(self):
        site = standard_site(tags={FACE: ["+100"], AMPHIBIA: ["+10"]})
        self.run_main(site, "vendre", execute=True, mutate=lambda c: c["sell"].update(order="tags", max_auctions=1))
        self.assertEqual(site.sells(), [FACE])

    def test_sell_guards(self):
        site = standard_site(tags={CHACANA: ["+10"], FOREST: ["+10"], FACE: ["+10", "defausse"], HOTEL: ["+10"]})
        output, _ = self.run_main(site, "vendre", execute=True)
        self.assertEqual(site.sells(), [])
        self.assertIn("Chacana [R] valeur=5 : vaut moins de 10", output)
        self.assertIn("PROTÉGÉE  Forest Hills (Queens) [R] (étiquette lyon)", output)
        self.assertIn("étiquettes contradictoires", output)
        self.assertIn("jamais vendue, prix inconnu", output)

    def test_sell_duration_and_limits_from_config(self):
        site = standard_site(tags=SORTED)
        self.run_main(site, "vendre", execute=True,
                      mutate=lambda c: c["sell"].update(duration_minutes=10, max_auctions=1, price_factor=1))
        self.assertEqual(site.sell_bodies(), [{"card_id": AMPHIBIA, "base_amount": 60, "duration_minutes": 10}])

    # --- defausser ---

    def test_discard_only_tagged_and_still_cheap(self):
        site = standard_site(tags={CHACANA: ["defausse"], AMPHIBIA: ["defausse"], FOREST: ["defausse"]})
        output, _ = self.run_main(site, "defausser", execute=True)
        self.assertEqual(site.discards(), [CHACANA])
        self.assertIn("vaut maintenant 60", output)
        self.assertIn("PROTÉGÉE  Forest Hills", output)

    def test_unknown_price_discarded_only_for_chosen_rarities(self):
        site = standard_site(tags={HOTEL: ["defausse"]})
        output, _ = self.run_main(site, "defausser", execute=True)  # unknown_rarities : []
        self.assertEqual(site.discards(), [])
        self.assertIn("jamais vendue, prix inconnu (discard.unknown_rarities)", output)
        site = standard_site(tags={HOTEL: ["defausse"]})
        self.run_main(site, "defausser", execute=True, mutate=lambda c: (
            c["price_tags"].update(unknown_tag="inconnu"), c["discard"].update(unknown_rarities=["R"])))
        self.assertEqual(site.discards(), [HOTEL])

    def test_everything_discards_defausse_and_unknown_cards_in_first_pass(self):
        def rule(rarities):
            return lambda c: (c["price_tags"].update(unknown_tag="inconnu"),
                              c["discard"].update(unknown_rarities=rarities, require_existing_tag=False))

        site = standard_site()
        self.run_main(site, "tout", execute=True, mutate=rule(["R"]))
        # Chacana (5, « defausse ») et Hotel California (jamais vendue, R, « inconnu ») : dès ce passage.
        self.assertEqual(sorted(site.discards()), sorted([CHACANA, HOTEL]))
        self.assertNotIn(FOREST, site.discards())  # protégée (lyon)

        site = standard_site(tags={HOTEL: ["inconnu"]})
        self.run_main(site, "defausser", execute=True, mutate=rule(["C"]))
        self.assertEqual(site.discards(), [])  # « inconnu » d'une autre rareté : ni défaussée, ni même relue
        self.assertEqual(site.gets("041905b9"), [])

    def test_discard_reads_only_the_prices_it_needs(self):
        site = standard_site()
        base = site.pages[0]["collection"][1]
        for n in range(6):
            item = copy.deepcopy(base)
            item.update(id=f"cheap-{n}", card_id=f"cheap-card-{n}")
            item["tags"] = [{"id": "t-defausse", "name": "defausse"}]
            site.pages[0]["collection"].append(item)
            site.values[f"cheap-card-{n}"] = 2
        output, _ = self.run_main(site, "defausser", execute=True, mutate=lambda c: c["discard"].update(max_per_run=2))
        self.assertEqual(len(site.discards()), 2)
        self.assertEqual(len(site.gets("/sales")), 2)  # pas les 6 prix : la suite attend le prochain passage
        self.assertIn("4 autre(s) carte(s) à défausser", output)
        self.assertIn("4 action(s) reportée(s)", output)

    def test_discard_max_per_run(self):
        site = standard_site(tags={CHACANA: ["defausse"]})
        output, _ = self.run_main(site, "defausser", execute=True, mutate=lambda c: c["discard"].update(max_per_run=0))
        self.assertEqual(site.discards(), [])
        self.assertIn("1 action(s) reportée(s)", output)

    # --- tout ---

    def test_everything_dry_run_uses_planned_tags(self):
        site = standard_site()
        output, code = self.run_main(site, "tout")
        self.assertEqual((site.mutations(), code), ([], 0))
        self.assertIn("Résumé (prévue(s)) : 2 enchère(s), 0 défausse(s), 3 étiquette(s) posée(s)", output)

    def test_everything_execute_in_order_and_new_discard_tag_waits(self):
        site = standard_site()
        output, _ = self.run_main(site, "tout", execute=True)
        self.assertIn("1 carte(s) étiquetée(s) à défausser pendant ce passage : traitée(s) au prochain", output)
        kinds = []
        for m, u, b in site.mutations():
            kinds.append("tag" if "/rest/v1/" in u else "sell" if u.endswith("/api/marketplace") else "discard")
        self.assertEqual(kinds, ["tag"] * 5 + ["sell", "sell"])
        # « defausse » vient d'être posée : la défausse attend le passage suivant (vérification possible).
        self.assertEqual((site.sells(), site.discards()), ([AMPHIBIA, FACE], []))
        site.pages[0]["collection"][1]["tags"].append({"id": "t-defausse", "name": "defausse"})
        self.run_main(site, "tout", execute=True)
        self.assertEqual(site.discards(), [CHACANA])

    def test_everything_can_discard_in_same_run_if_configured(self):
        site = standard_site()
        self.run_main(site, "tout", execute=True, mutate=lambda c: c["discard"].update(require_existing_tag=False))
        self.assertEqual(site.discards(), [CHACANA])

    def test_max_actions_counts_across_phases(self):
        site = standard_site()
        output, _ = self.run_main(site, "tout", execute=True, mutate=lambda c: c["safety"].update(max_actions_per_run=4))
        self.assertEqual(len(site.sells()), 1)
        self.assertEqual(site.discards(), [])
        self.assertIn("action(s) reportée(s)", output)

    def test_failed_tag_means_no_sale_in_same_run(self):
        site = standard_site()
        site.overrides[("POST", "/rest/v1/user_card_tags")] = [FakeResponse(201, None), FakeResponse(201, None),
                                                              FakeResponse(409, {"code": "23505"})]
        self.run_main(site, "tout", execute=True)
        self.assertNotIn(AMPHIBIA, site.sells())

    # --- journal, bilan des ventes et relances ---

    def read_journal(self):
        import csv as _csv
        path = self.dir / "journal.csv"
        if not path.exists():
            return []
        with open(path, encoding="utf-8-sig", newline="") as f:
            return list(_csv.DictReader(f, delimiter=";"))

    def test_journal_records_real_actions_only(self):
        site = standard_site(tags=SORTED)
        self.run_main(site, "vendre")
        self.assertEqual(self.read_journal(), [])
        self.run_main(site, "tout", execute=True, mutate=lambda c: c["discard"].update(require_existing_tag=False))
        rows = self.read_journal()
        self.assertTrue((self.dir / "journal.csv").read_bytes().startswith(b"\xef\xbb\xbf"))  # BOM pour Excel
        by_action = {}
        for row in rows:
            by_action.setdefault(row["action"], []).append(row)
        self.assertEqual([r["prix"] for r in by_action["enchere"]], ["45", "9"])
        self.assertEqual(by_action["enchere"][0]["duree_min"], "30")
        self.assertEqual(by_action["defausse"][0]["solde"], "100")
        self.assertEqual(by_action["defausse"][0]["carte"], "Chacana")
        self.assertEqual({r["compte"] for r in rows}, {"testeur"})

    def test_finished_sales_are_settled_and_unsold_relisted_cheaper(self):
        site = standard_site(tags=SORTED)
        self.run_main(site, "vendre", execute=True)
        state = json.loads((self.acc / "ventes.json").read_text())["auctions"]
        self.assertEqual(sorted(state), ["auction-103", "auction-104"])
        # Amphibia vendue 70, Face visible invendue (elle revient dans la collection).
        site.auctions["auction-104"] = {"status": "settled", "winner_id": "x", "final_price": 70}
        site.auctions["auction-103"] = {"status": "expired", "winner_id": None}
        site.pages[0]["collection"] = [i for i in site.pages[0]["collection"] if i["id"] != AMPHIBIA]
        output, _ = self.run_main(site, "vendre", execute=True)
        self.assertIn("VENDUE    Amphibia", output)
        self.assertIn("INVENDUE  Face visible", output)
        self.assertEqual(site.sell_bodies()[-1], {"card_id": FACE, "base_amount": 7, "duration_minutes": 30})  # 9 × 0,8
        self.assertIn("relance 2", output)
        rows = self.read_journal()
        self.assertEqual([(r["action"], r["prix"]) for r in rows if r["action"] in ("vendue", "invendue", "relance")],
                         [("vendue", "70"), ("invendue", "9"), ("relance", "7")])
        state = json.loads((self.acc / "ventes.json").read_text())["auctions"]
        self.assertEqual(list(state), ["auction-103-3"])
        self.assertEqual((state["auction-103-3"]["attempt"], state["auction-103-3"]["status"]), (2, "open"))

    def test_relist_with_reused_auction_id_keeps_tracking(self):
        site = standard_site(tags={FACE: ["+10"]})
        (self.acc / "ventes.json").write_text(json.dumps({"auctions": {"auction-103": {
            "copy_id": FACE, "name": "Face visible de la Lune", "rarity": "R", "price": 9, "attempt": 1,
            "status": "unsold"}}}))
        self.run_main(site, "vendre", execute=True)  # le faux site redonne « auction-103 »
        state = json.loads((self.acc / "ventes.json").read_text())["auctions"]
        self.assertEqual((state["auction-103"]["status"], state["auction-103"]["attempt"]), ("open", 2))

    def test_relist_stops_after_max_attempts(self):
        site = standard_site(tags={FACE: ["+10"]})
        (self.acc / "ventes.json").write_text(json.dumps({"auctions": {"old": {
            "copy_id": FACE, "card_id": "x", "name": "Face visible de la Lune", "rarity": "R", "price": 5,
            "attempt": 3, "status": "unsold"}}}))
        output, _ = self.run_main(site, "vendre", execute=True)
        self.assertEqual(site.sells(), [])
        self.assertIn("invendue 3 fois", output)

    def test_card_confirmed_on_sale_is_not_listed_again(self):
        site = standard_site(tags=SORTED)
        (self.acc / "ventes.json").write_text(json.dumps({"auctions": {"auction-104": {
            "copy_id": AMPHIBIA, "name": "Amphibia", "price": 45, "attempt": 1, "status": "open"}}}))
        output, _ = self.run_main(site, "vendre", execute=True)  # le faux site répond « active »
        self.assertEqual(site.sells(), [FACE])
        self.assertIn("déjà en vente", output)

    def test_unreadable_auction_with_card_back_counts_as_unsold(self):
        site = standard_site(tags={FACE: ["+10"]})
        (self.acc / "ventes.json").write_text(json.dumps({"auctions": {"auction-103": {
            "copy_id": FACE, "name": "Face visible de la Lune", "rarity": "R", "price": 9, "attempt": 1,
            "listed_at": 0, "duration": 30, "status": "open"}}}))
        site.overrides[("GET", "/api/marketplace/auction-103")] = FakeResponse(404, {"error": "introuvable"})
        output, _ = self.run_main(site, "vendre", execute=True)
        self.assertIn("INVENDUE  Face visible", output)
        self.assertEqual(site.sell_bodies(), [{"card_id": FACE, "base_amount": 7, "duration_minutes": 30}])

    def test_unsettled_or_unknown_status_waits(self):
        for auction in ({"status": "ended", "winner_id": None, "settled_at": None}, {"status": "pending"}, [1, 2]):
            site = standard_site(tags={AMPHIBIA: ["+10"]})
            site.pages[0]["collection"] = [i for i in site.pages[0]["collection"] if i["id"] != AMPHIBIA]
            (self.acc / "ventes.json").write_text(json.dumps({"auctions": {"auction-104": {
                "copy_id": AMPHIBIA, "name": "Amphibia", "price": 45, "attempt": 1, "listed_at": time.time(),
                "duration": 30, "status": "open"}}}))
            site.auctions["auction-104"] = auction
            output, _ = self.run_main(site, "vendre", execute=True)
            self.assertNotIn("INVENDUE", output, auction)
            state = json.loads((self.acc / "ventes.json").read_text())["auctions"]
            self.assertEqual(state["auction-104"]["status"], "open", auction)

    def test_cancelled_auction_is_not_relisted(self):
        site = standard_site(tags={FACE: ["+10"]})
        (self.acc / "ventes.json").write_text(json.dumps({"auctions": {"auction-103": {
            "copy_id": FACE, "name": "Face visible de la Lune", "price": 9, "attempt": 1, "status": "open"}}}))
        site.auctions["auction-103"] = {"status": "cancelled"}
        output, _ = self.run_main(site, "vendre", execute=True, mutate=lambda c: c["sell"].update(tags=["+100"]))
        self.assertIn("ANNULÉE", output)
        self.assertEqual(site.sells(), [])
        self.assertEqual(json.loads((self.acc / "ventes.json").read_text())["auctions"], {})

    def test_relist_price_is_capped_and_always_lower(self):
        sell = load_cfg()["sell"]
        self.assertEqual(w.relist_price(750, 12, sell), 9)          # plafonnée à la mise normale au prix actuel
        self.assertEqual(w.relist_price(4, 100, dict(sell, rounding="ceil")), 3)  # baisse même avec ceil
        self.assertEqual(w.relist_price(1, 100, sell), 1)           # jamais sous le minimum
        self.assertEqual(w.relist_price(300, 1000, dict(sell, max_start_price=40)), 40)

    def test_dry_run_writes_only_the_price_cache(self):
        site = standard_site(tags=SORTED)
        mutate = lambda c: c["price"].update(cache_hours=6)  # noqa: E731
        self.run_main(site, "tout", mutate=mutate)  # crée la config, la session de test et le cache des prix
        def files():
            return {str(p.relative_to(self.dir)): p.stat().st_mtime_ns for p in self.dir.rglob("*")
                    if p.is_file() and p.name != "session.json"}

        before = files()
        self.run_main(site, "tout", mutate=mutate)
        after = files()
        self.assertEqual(sorted(after), ["config.yaml", "prix.json"])  # ni journal, ni ventes.json, ni verrou
        self.assertEqual(set(after), set(before))

    def test_random_draw_is_the_same_for_dry_run_and_execute(self):
        data = load("collection.json")
        extra = [dict(copy.deepcopy(data["collection"][3]), id=f"copy-{n}") for n in range(8)]
        for n, item in enumerate(extra):
            item["tags"].append({"id": "t-+10", "name": "+10"})
            item["card"] = dict(item["card"], wikipedia_title=f"Carte {n}")
        site = standard_site()
        site.pages[0]["collection"] += extra
        out_dry, _ = self.run_main(site, "vendre", mutate=lambda c: c["sell"].update(order="random"))
        self.run_main(site, "vendre", execute=True, mutate=lambda c: c["sell"].update(order="random"))
        planned = sorted(line.split("Carte ")[1].split(" ")[0] for line in out_dry.splitlines()
                         if "ENCHÈRE" in line and "Carte " in line)
        sold = sorted(s.split("-")[1] for s in site.sells() if s.startswith("copy-"))
        self.assertEqual(len(site.sells()), 5)
        self.assertEqual(planned, sold)  # exactement les cartes montrées par le dry-run

    def test_second_execute_run_is_refused_while_locked(self):
        (self.acc / ".wikimasters.lock").write_text(f"{os.getpid()} 2026-01-01 00:00:00")
        output, code = self.run_main(standard_site(tags=SORTED), "vendre", execute=True)
        self.assertIn("Un autre passage", str(code))
        self.assertTrue((self.acc / ".wikimasters.lock").exists())  # le verrou d'un autre passage n'est pas supprimé

    def test_journal_neutralises_formulas_and_keeps_numbers_readable(self):
        journal = w.Journal(self.dir / "j.csv", ";", True, "=moi", "vendre")
        journal.write("enchere", name="+44 (groupe)", value=1234567.891, price=12)
        text = (self.dir / "j.csv").read_text(encoding="utf-8-sig")
        self.assertIn("'+44 (groupe)", text)
        self.assertIn("'=moi", text)
        self.assertIn("1234567,89", text)
        self.assertNotIn("e+", text)

    def test_old_journal_format_is_set_aside(self):
        old = self.dir / "j.csv"
        old.write_text("date,ancien\n1,2\n", encoding="utf-8")
        w.Journal(old, ";", True, "moi", "vendre").write("enchere", name="x")
        self.assertTrue(any(p.name.startswith("j-ancien-") for p in self.dir.iterdir()))
        self.assertTrue(old.read_text(encoding="utf-8-sig").startswith(";".join(w.JOURNAL_FIELDS)))

    def test_locked_journal_goes_to_backup_then_is_merged(self):
        site = standard_site(tags=SORTED)
        self.run_main(site, "vendre", execute=True)
        site.auctions["auction-104"] = {"status": "settled", "winner_id": "x", "final_price": 70}
        real_open = open

        def locked(path, *a, **kw):
            if str(path).endswith("journal.csv") and a and a[0] == "a":
                raise PermissionError(13, "verrouillé par Excel")
            return real_open(path, *a, **kw)

        with mock.patch("builtins.open", side_effect=locked):
            output, _ = self.run_main(site, "vendre", execute=True)
        self.assertEqual(output.count("est verrouillé"), 1)  # un seul avertissement, pas un par ligne
        self.assertIn("écrite(s) dans journal_secours.csv", output)
        self.assertNotIn("non écrite", output)
        self.assertNotIn("vendue", [r["action"] for r in self.read_journal()])
        # Rien n'est perdu : la vente est réglée une seule fois, la ligne attend dans le fichier de secours.
        self.assertNotIn("auction-104", json.loads((self.acc / "ventes.json").read_text())["auctions"])
        backup = self.dir / "journal_secours.csv"
        self.assertIn("vendue", backup.read_text(encoding="utf-8-sig"))

        output, _ = self.run_main(site, "vendre", execute=True)  # Excel fermé
        self.assertIn("recopiée(s) dans journal.csv", output)
        self.assertFalse(backup.exists())
        actions = [r["action"] for r in self.read_journal()]
        self.assertEqual(actions.count("vendue"), 1)
        self.assertEqual(self.read_journal()[0]["date"][:2], "20")  # en-tête unique, lignes bien formées

    def test_dry_run_does_not_touch_sales_state(self):
        site = standard_site(tags=SORTED)
        self.run_main(site, "vendre", execute=True)
        before = (self.acc / "ventes.json").read_text()
        site.auctions["auction-104"] = {"status": "settled", "winner_id": "x", "final_price": 70}
        journal_before = self.read_journal()
        output, _ = self.run_main(site, "vendre")
        self.assertIn("VENDUE", output)
        self.assertEqual((self.acc / "ventes.json").read_text(), before)
        self.assertEqual(self.read_journal(), journal_before)  # rien n'est journalisé en dry-run

    # --- boucle, verrou, --fresh et Telegram ---

    def test_loop_relists_card_seen_on_sale_in_a_previous_cycle(self):
        site = standard_site(tags={AMPHIBIA: ["+10"]})
        (self.acc / "ventes.json").write_text(json.dumps({"auctions": {"auction-104": {
            "copy_id": AMPHIBIA, "card_id": "x", "name": "Amphibia", "rarity": "R", "price": 45, "attempt": 1,
            "listed_at": time.time(), "duration": 30, "status": "open"}}}))
        pauses = []

        def sleep(seconds):
            if seconds == 60:  # pause de --loop 1
                pauses.append(seconds)
                if len(pauses) == 2:
                    raise KeyboardInterrupt
                site.auctions["auction-104"] = {"status": "expired", "winner_id": None}  # finie sans acheteur

        output, code = self.run_main(site, "vendre", execute=True, args=["--loop", "1"], sleep=sleep)
        self.assertIn("déjà en vente", output)  # 1er cycle : encore en cours
        self.assertEqual(site.sells(), [AMPHIBIA])  # 2e cycle : relancée, pas « déjà en vente » pour toujours
        self.assertIn("relance 2", output)
        self.assertEqual(code, 0)
        self.assertFalse((self.acc / ".wikimasters.lock").exists())

    def test_revoked_session_stops_the_loop(self):
        site = standard_site(tags=SORTED)
        site.overrides[("POST", "/auth/v1/token")] = FakeResponse(400, {"error": "invalid_grant"})
        pauses = []
        output, code = self.run_main(site, "tout", execute=True, session=make_session(exp_offset=-10),
                                     args=["--loop", "1"], sleep=pauses.append)
        self.assertEqual((code, site.mutations()), (1, []))
        self.assertNotIn(60, pauses)  # pas de nouveau cycle
        self.assertEqual(output.count("session révoquée"), 1)

    def test_loop_needs_a_positive_number_of_minutes(self):
        for bad in ("0", "-5", "abc"):
            with redirect_stderr(io.StringIO()):
                _, code = self.run_main(standard_site(), "analyser", args=["--loop", bad])
            self.assertEqual(code, 2, bad)

    def test_lock_of_a_dead_or_silent_run_is_taken_over(self):
        lock = self.acc / ".wikimasters.lock"
        lock.write_text("4242 2026-01-01 00:00:00")
        with mock.patch.object(w, "pid_alive", return_value=False):
            _, code = self.run_main(standard_site(tags=SORTED), "vendre", execute=True)
        self.assertEqual(code, 0)
        lock.write_text(f"{os.getpid()} 2026-01-01 00:00:00")
        old = time.time() - w.RunLock.MAX_AGE - 60
        os.utime(lock, (old, old))  # processus vivant (PID réutilisé ?) mais aucun signe de vie depuis 1 h
        _, code = self.run_main(standard_site(tags=SORTED), "vendre", execute=True)
        self.assertEqual(code, 0)
        self.assertFalse(lock.exists())

    def test_pid_alive(self):
        self.assertTrue(w.pid_alive(os.getpid()))
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        self.assertFalse(w.pid_alive(proc.pid))

    def test_held_lock_shows_signs_of_life(self):
        lock = w.RunLock(self.acc / ".wikimasters.lock")
        with mock.patch.object(w.RunLock, "HEARTBEAT", 0.01):
            lock.acquire()
            try:
                old = time.time() - 7200
                os.utime(lock.path, (old, old))
                deadline = time.time() + 2
                while time.time() - lock.path.stat().st_mtime > 60 and time.time() < deadline:
                    time.sleep(0.01)
                self.assertLess(time.time() - lock.path.stat().st_mtime, 60)
            finally:
                lock.release()
        self.assertFalse(lock.path.exists())

    def test_fresh_rereads_prices_and_updates_existing_tags(self):
        site = standard_site(tags={FACE: ["+10"], CHACANA: ["defausse"]})
        chacana_id, face_id = (site.pages[0]["collection"][i]["card_id"] for i in (1, 2))
        mutate = lambda c: c["price"].update(cache_hours=6)  # noqa: E731
        self.run_main(site, "analyser", mutate=mutate)  # prix mis en cache : Chacana 5, Face visible 12
        site.values.update({chacana_id: 20, face_id: 150})  # les prix ont évolué depuis

        def changes(output, name):
            return [line.split("->")[1].split("  ")[0].strip() for line in output.splitlines()
                    if "->" in line and name in line]

        output, _ = self.run_main(site, "analyser", mutate=mutate)
        self.assertEqual((changes(output, "Chacana"), changes(output, "Face visible")), ([], []))  # cache
        reads = len(site.gets(face_id))
        output, _ = self.run_main(site, "analyser", execute=True, mutate=mutate, args=["--fresh"])
        self.assertIn("Option --fresh", output)
        self.assertEqual(len(site.gets(face_id)), reads + 1)  # relu sur le site
        self.assertEqual(changes(output, "Chacana"), ["- étiquette « defausse »", "+ étiquette « +10 »"])
        self.assertEqual(changes(output, "Face visible"), ["- étiquette « +10 »", "+ étiquette « +100 »"])
        self.assertIn((CHACANA, "t-+10"), site.tag_links())

    def test_telegram_message_escapes_html(self):
        text = w.telegram_message("<moi & co>", "vendre", ("error", "erreur 500 : <html>"), "Résumé : a < b")
        self.assertIn("&lt;moi &amp; co&gt;", text)
        self.assertIn("&lt;html&gt;", text)
        self.assertIn("a &lt; b", text)

    def test_telegram_token_from_perso_yaml_and_the_real_outcome(self):
        site = standard_site()
        output, _ = self.run_main(site, "analyser", execute=True)  # pas de perso.yaml : pas de notification
        self.assertEqual(site.telegram, [])
        self.assertNotIn("Telegram", output)

        output, _ = self.run_main(site, "analyser", execute=True, perso={"telegram": {"bot_token": "123:secret"}})
        self.assertIn("pas de notification", output)  # chat_id manquant
        self.assertEqual(site.telegram, [])

        self.run_main(site, "analyser", perso={"telegram": {"bot_token": "123:secret", "chat_id": 42}})
        self.assertEqual(site.telegram, [])  # simulation : notify_on_dry_run vaut false
        self.run_main(site, "analyser", execute=True)
        url, body = site.telegram[-1]
        self.assertIn("/bot123:secret/sendMessage", url)
        self.assertEqual((body["chat_id"], body["parse_mode"]), ("42", "HTML"))
        self.assertIn("✅", body["text"])

        site = standard_site()

        def interrupt(seconds):
            if seconds == load_cfg()["safety"]["delay_seconds"]:
                raise KeyboardInterrupt

        self.run_main(site, "analyser", execute=True, sleep=interrupt)
        self.assertIn("Interrompu", site.telegram[-1][1]["text"])
        self.assertNotIn("✅", site.telegram[-1][1]["text"])

        site = standard_site(tags=SORTED)
        site.overrides[("GET", "/api/marketplace/mine")] = FakeResponse(200, {})  # réponse inattendue du site
        with self.assertRaises(KeyError):
            self.run_main(site, "vendre", execute=True)
        self.assertIn("Plantage", site.telegram[-1][1]["text"])
        self.assertIn("KeyError", site.telegram[-1][1]["text"])

    # --- comptes, perso.yaml, boucle ---

    def test_accounts_are_separate_and_chosen_with_compte(self):
        other = self.dir / "comptes" / "Autre"
        other.mkdir()
        w.SessionStore(other / "session.json").save(make_session())
        output, code = self.run_main(standard_site(tags=SORTED), "vendre", execute=True)
        self.assertIn("Plusieurs comptes", str(code))
        self.assertIn("--compte Autre", str(code))
        output, code = self.run_main(standard_site(tags=SORTED), "vendre", execute=True, args=["--compte", "inconnu"])
        self.assertIn("Compte « inconnu » inconnu", str(code))

        site = standard_site(tags=SORTED)
        self.run_main(site, "vendre", execute=True, args=["--compte", "autre"])  # majuscules ignorées
        self.assertEqual(len(site.sells()), 2)
        self.assertTrue((other / "ventes.json").exists())
        self.assertFalse((self.acc / "ventes.json").exists())  # le suivi des ventes est propre à chaque compte

        output, code = self.run_main(site, "comptes")
        self.assertIn("Autre", output)
        self.assertIn("2 enchère(s) suivie(s)", output)

    def test_login_creates_the_account_folder_from_the_site_name(self):
        claims = {"sub": USER_ID, "exp": int(time.time()) + 3600, "user_metadata": {"username": "Bob/Ü 2"}}
        session = {"access_token": f"{b64({'alg': 'HS256'})}.{b64(claims)}.sig", "refresh_token": "r0",
                   "expires_at": claims["exp"]}
        output, code = self.run_main(standard_site(), "login", session=None, stdin=fake_cookie(session=session) + "\n")
        self.assertEqual(code, 0)
        self.assertTrue((self.dir / "comptes" / "Bob_Ü_2" / "session.json").exists())
        self.assertIn("Compte Bob/Ü 2 enregistré", output)

    def test_perso_yaml_overrides_shared_settings(self):
        site = standard_site(tags={CHACANA: ["defausse"]})
        output, _ = self.run_main(site, "defausser", execute=True, perso={"discard": {"max_per_run": 0}})
        self.assertEqual(site.discards(), [])
        self.assertIn("1 action(s) reportée(s)", output)

    def test_loop_defaults_to_15_minutes_and_fresh_rereads_only_once(self):
        waits = []

        def sleep(seconds):
            if seconds == 15 * 60:
                waits.append(seconds)
                if len(waits) == 2:
                    raise KeyboardInterrupt

        site = standard_site()
        output, code = self.run_main(site, "analyser", sleep=sleep, args=["--fresh", "--loop"],
                                     mutate=lambda c: c["price"].update(cache_hours=6))
        self.assertEqual((code, waits), (0, [900, 900]))
        self.assertEqual(output.count("=== ["), 2)
        self.assertEqual(len(site.gets("/sales")), 4)  # 2e cycle : prix relus au 1er cycle réutilisés
        self.assertEqual(output.count("Option --fresh"), 1)

    def test_price_cache_merges_runs_and_forgets_expired_prices(self):
        path = self.dir / "prix.json"
        path.write_text(json.dumps({"vieux:C": {"value": 1, "at": time.time() - 7 * 3600}}), encoding="utf-8")
        a, b = w.PriceCache(path, 6), w.PriceCache(path, 6)
        a.set(w.Card(copy_id="1", card_id="c1", name="A", rarity="C"), 3)
        a.save()
        b.set(w.Card(copy_id="2", card_id="c2", name="B", rarity="R"), None)
        b.save()  # un autre compte en parallèle : il n'efface pas le prix lu par le premier
        self.assertEqual(sorted(json.loads(path.read_text())), ["c1:C", "c2:R"])

    def token_posts(self, site):
        return [u for m, u, *_ in site.calls if m == "POST" and u.endswith("/auth/v1/token")]

    def test_network_blip_during_refresh_is_retried(self):
        site = standard_site()
        site.overrides[("POST", "/auth/v1/token")] = [requests.ConnectionError(), None]
        output, code = self.run_main(site, "analyser", execute=True, session=make_session(exp_offset=-10))
        self.assertEqual((code, site.refreshes, len(self.token_posts(site))), (0, 1, 2))
        self.assertEqual(self.store.load()["refresh_token"], "r1")
        self.assertEqual(len(site.tag_links()), 3)

    def test_refresh_is_postponed_while_the_token_is_still_valid(self):
        site = standard_site()
        site.overrides[("POST", "/auth/v1/token")] = requests.ConnectionError()
        output, code = self.run_main(site, "analyser", execute=True, session=make_session(exp_offset=200))
        self.assertEqual(code, 0)
        self.assertIn("renouvellement de session reporté (réseau : ConnectionError)", output)
        self.assertEqual(len(self.token_posts(site)), 1 + len(w.REFRESH_RETRY_DELAYS))  # pas à chaque requête
        self.assertEqual(len(site.tag_links()), 3)

    def test_refresh_impossible_with_expired_token_stops_and_tells_how_to_resume(self):
        site = standard_site()
        site.overrides[("POST", "/auth/v1/token")] = FakeResponse(503, {"error": "indisponible"})
        output, code = self.run_main(site, "analyser", execute=True, session=make_session(exp_offset=-10),
                                     mutate=lambda c: c["price"].update(cache_hours=6), args=["--fresh"])
        self.assertEqual((code, site.mutations()), (1, []))
        self.assertIn("renouvellement de session impossible (erreur 503)", output)
        self.assertIn("(sans --fresh)", output)

    # --- corrections de la quatrième revue ---

    def test_discard_rechecks_price_on_site_not_cache(self):
        site = standard_site(tags={CHACANA: ["defausse"]})
        keep = lambda c: c["price"].update(cache_hours=6)  # noqa: E731
        self.run_main(site, "analyser", mutate=keep)
        site.values[site.pages[0]["collection"][1]["card_id"]] = 50  # le prix a monté depuis
        output, _ = self.run_main(site, "defausser", execute=True, mutate=keep)
        self.assertEqual(site.discards(), [])
        self.assertIn("vaut maintenant 50", output)

    def test_failed_protecting_tag_blocks_discard_later_in_run(self):
        site = standard_site(tags={CHACANA: ["defausse"]})
        # Seule la pose de « garder » sur Chacana échoue ; les autres étiquettes passent.
        site.overrides[("POST", "/rest/v1/user_card_tags")] = [FakeResponse(500, {"error": "x"}), None]
        mutate = lambda c: c.update(auto_tags=[{"tag": "garder", "when": {"name_contains": "Chacana"}}])  # noqa: E731
        output, _ = self.run_main(site, "tout", execute=True, mutate=mutate)
        self.assertIn("== Défausses ==", output)
        self.assertEqual(site.discards(), [])

    def test_tag_refused_by_access_rule_skips_card_and_continues(self):
        # Carte sortie de la collection pendant le passage : Supabase refuse la ligne (403, code 42501).
        site = standard_site()
        rls = {"code": "42501", "details": None, "hint": None,
               "message": 'new row violates row-level security policy for table "user_card_tags"'}
        site.overrides[("POST", "/rest/v1/user_card_tags")] = [FakeResponse(403, rls), None]
        output, code = self.run_main(site, "analyser", execute=True)
        self.assertEqual(code, 0)
        self.assertIn("plus dans votre collection", output)
        self.assertNotIn("relancez « python wikimasters.py login »", output)
        self.assertIn("1 échec(s)", output)
        self.assertGreater(len(site.tag_links()), 1)  # les cartes suivantes sont bien étiquetées

    def test_403_without_access_rule_code_is_still_fatal(self):
        site = standard_site()
        site.overrides[("POST", "/rest/v1/user_card_tags")] = FakeResponse(403, {"message": "forbidden"})
        output, code = self.run_main(site, "analyser", execute=True)
        self.assertEqual((code, len(site.tag_links())), (1, 1))  # arrêt dès la première tentative
        self.assertIn("relancez « python wikimasters.py login »", output)

    def test_tag_removal_not_confirmed_is_a_failure(self):
        site = standard_site(tags={FACE: ["+100"]})
        site.delete_returns_rows = False
        output, _ = self.run_main(site, "analyser", execute=True)
        self.assertIn("non retirée", output)
        self.assertNotIn((FACE, "t-+10"), site.tag_links())

    def test_tag_creation_without_id_is_a_clean_failure(self):
        site = standard_site()
        site.overrides[("POST", "/rest/v1/tags")] = FakeResponse(201, [])
        output, code = self.run_main(site, "analyser", execute=True)
        self.assertIn("n'a pas renvoyé son identifiant", output)
        self.assertEqual(site.tag_links(), [])
        self.assertEqual(code, 1)  # 3 échecs d'affilée

    def test_created_tag_name_has_no_hash(self):
        site = standard_site()
        self.run_main(site, "analyser", execute=True,
                      mutate=lambda c: c["price_tags"]["bands"][0].update(tag="#defausse") or
                      c["discard"].update(tags=["#defausse"]))
        created = [b["name"] for m, u, b in site.mutations() if u.endswith("/rest/v1/tags")]
        self.assertIn("defausse", created)

    def test_summary_counts_each_card_once_in_everything(self):
        site = standard_site(tags={FOREST: ["defausse"]})
        output, _ = self.run_main(site, "tout")
        self.assertIn("1 protégée(s)", output)

    def test_skipped_phases_are_announced(self):
        site = standard_site()
        output, _ = self.run_main(site, "tout", execute=True, mutate=lambda c: c["safety"].update(max_actions_per_run=1))
        self.assertIn("Étape(s) non lancée(s) pour cette raison : Ventes aux enchères, Défausses", output)

    def test_value_outside_bands_is_not_called_never_sold(self):
        site = standard_site()
        mutate = lambda c: c["price_tags"].update(bands=[{"tag": "defausse", "below": 10}, {"tag": "+10", "from": 10, "below": 50}])  # noqa: E501,E731
        output, _ = self.run_main(site, "analyser", mutate=lambda c: (mutate(c), c["sell"].update(tags=["+10"])))
        self.assertIn("hors tranches : 1, jamais vendue : 1", output)


    # --- collection ---

    def test_pending_trades_from_page_zero_protect_later_pages(self):
        data = load("collection.json")
        filler = [dict(copy.deepcopy(data["collection"][4]), id=f"filler-{n}") for n in range(w.PAGE_SIZE)]
        target = data["collection"][1]
        target["tags"].append({"id": "t-defausse", "name": "defausse"})
        site = FakeSite([{"collection": filler, "pendingTradeCardIds": [target["id"]]},
                         {"collection": [target], "pendingTradeCardIds": []}], {target["card_id"]: 5})
        output, _ = self.run_main(site, "defausser", execute=True)
        self.assertEqual(site.discards(), [])
        self.assertIn("(échange en cours)", output)

    def test_grouped_copies_are_not_touched(self):
        site = standard_site(tags={CHACANA: ["defausse"]})
        site.pages[0]["collection"][1]["count"] = 3
        output, _ = self.run_main(site, "tout", execute=True)
        self.assertNotIn(CHACANA, site.discards())
        self.assertIn("1 ligne(s) à plusieurs exemplaires", output)

    def test_pagination_not_advancing_is_reported(self):
        data = load("collection.json")
        full = {"collection": [dict(data["collection"][4], id=f"c{n}") for n in range(w.PAGE_SIZE)]}
        output, _ = self.run_main(FakeSite([full, full], {}), "analyser")
        self.assertIn("la pagination n'avance pas", output)

    def test_duplicate_copy_on_same_page_is_acted_on_once(self):
        site = standard_site(tags={CHACANA: ["defausse"]})
        site.pages[0]["collection"].append(copy.deepcopy(site.pages[0]["collection"][1]))
        self.run_main(site, "defausser", execute=True)
        self.assertEqual(site.discards(), [CHACANA])

    # --- lecture des prix ---

    def test_transient_read_error_is_retried(self):
        site = standard_site()
        site.overrides[("GET", "/sales")] = [FakeResponse(500, {"error": "x"}), None]
        output, _ = self.run_main(site, "analyser")
        self.assertIn("defausse : 1, +10 : 2", output)
        self.assertNotIn("ÉCHEC", output)

    def test_unknown_card_404_is_treated_as_never_sold(self):
        site = standard_site()
        site.overrides[("GET", "/sales")] = FakeResponse(404, {"error": "Carte introuvable"})
        output, _ = self.run_main(site, "analyser")
        self.assertIn("jamais vendue : 4", output)
        self.assertNotIn("ÉCHEC", output)

    def test_failed_lookup_is_read_once_per_card_id(self):
        data = load("collection.json")
        twin = dict(copy.deepcopy(data["collection"][1]), id="twin-copy")
        data["collection"].append(twin)
        site = FakeSite([data], {})
        site.overrides[("GET", f"/cards/{twin['card_id']}/sales")] = FakeResponse(500, {"error": "Erreur serveur"})
        output, _ = self.run_main(site, "analyser")
        self.assertEqual(len(site.gets(f"/cards/{twin['card_id']}/sales")), 1 + len(w.GET_RETRY_DELAYS))
        self.assertIn("2 prix illisible(s)", output)

    def test_too_many_read_failures_stop_without_action(self):
        site = standard_site()
        site.overrides[("GET", "/sales")] = FakeResponse(500, {"error": "x"})
        data = site.pages[0]
        data["collection"] += [dict(copy.deepcopy(data["collection"][1]), id=f"x{n}", card_id=f"k{n}",
                                    card=dict(data["collection"][1]["card"], id=f"k{n}")) for n in range(5)]
        output, code = self.run_main(site, "tout", execute=True)
        self.assertEqual((code, site.mutations()), (1, []))
        self.assertIn("lectures de prix échouées d'affilée", output)

    def test_price_cache_shared_between_commands(self):
        site = standard_site(tags=SORTED)
        keep = lambda c: c["price"].update(cache_hours=6)  # noqa: E731
        self.run_main(site, "analyser", execute=True, mutate=keep)
        first = len(site.gets("/sales"))
        self.run_main(site, "vendre", mutate=keep)
        self.assertGreater(first, 0)
        self.assertEqual(len(site.gets("/sales")), first)

    # --- erreurs pendant l'exécution ---

    def test_non_fatal_error_skips_card_and_continues(self):
        site = standard_site(tags=SORTED)
        site.overrides[("POST", "/api/marketplace")] = [FakeResponse(409, {"error": "locked"}), None]
        output, code = self.run_main(site, "vendre", execute=True)
        self.assertIn("1 échec(s)", output)
        self.assertEqual((site.sells(), code), ([AMPHIBIA, FACE], 0))
        self.assertIn("ECHEC", (self.dir / "journal.csv").read_text(encoding="utf-8-sig"))
        self.assertIn("Résumé (effectuée(s)) : 1 enchère(s)", output)

    def test_network_error_on_post_stops_with_summary_and_no_secret(self):
        site = standard_site(tags=SORTED)
        site.overrides[("POST", f"{CHACANA}/discard")] = requests.exceptions.ReadTimeout(f"boom {fake_cookie()}")
        output, code = self.run_main(site, "defausser", execute=True)
        self.assertEqual(code, 1)
        self.assertIn("peut-être été effectuée", output)
        self.assertIn("Résumé", output)
        self.assertNotIn("base64-", output)

    def test_html_response_is_fatal(self):
        site = standard_site(tags=SORTED)
        site.overrides[("GET", "/api/marketplace/mine")] = FakeResponse(200, "<html>Just a moment</html>", "text/html")
        output, code = self.run_main(site, "vendre", execute=True)
        self.assertEqual((code, site.mutations()), (1, []))
        self.assertIn("non JSON", output)

    def test_ctrl_c_during_post_warns_action_may_be_done(self):
        site = standard_site(tags=SORTED)
        site.overrides[("POST", f"{CHACANA}/discard")] = KeyboardInterrupt()
        output, code = self.run_main(site, "defausser", execute=True)
        self.assertEqual(code, 1)
        self.assertIn("Interrompu pendant l'envoi", output)

    def test_ctrl_c_during_pause_keeps_action_in_summary(self):
        def sleep(seconds):
            if seconds == load_cfg()["safety"]["delay_seconds"]:
                raise KeyboardInterrupt

        site = standard_site(tags=SORTED)
        output, code = self.run_main(site, "defausser", execute=True, sleep=sleep)
        self.assertEqual(code, 1)
        self.assertIn("Résumé (effectuée(s)) : 0 enchère(s), 1 défausse(s)", output)

    # --- session ---

    def test_no_session_asks_for_login(self):
        output, code = self.run_main(standard_site(), "analyser", session=None)
        self.assertIn("login", str(code))

    def test_expired_access_token_is_refreshed_before_anything(self):
        site = standard_site(tags=SORTED)
        output, code = self.run_main(site, "defausser", execute=True, session=make_session(exp_offset=-10))
        self.assertEqual((site.refreshes, code), (1, 0))
        self.assertEqual(self.store.load()["refresh_token"], "r1")
        first_api = next(h for m, u, b, h, p in site.calls if "/api/" in u)
        self.assertIn(w.session_cookies(self.store.load(), REF)[f"sb-{REF}-auth-token"], first_api["Cookie"])
        self.assertEqual(site.discards(), [CHACANA])

    def test_revoked_session_stops_before_any_action(self):
        site = standard_site(tags=SORTED)
        site.overrides[("POST", "/auth/v1/token")] = FakeResponse(400, {"error": "invalid_grant"})
        output, code = self.run_main(site, "tout", execute=True, session=make_session(exp_offset=-10))
        self.assertEqual((code, site.mutations()), (1, []))
        self.assertIn("relancez « python wikimasters.py login »", output)

    def test_token_expiring_during_execution_is_refreshed(self):
        site, clock = standard_site(), Clock()

        def jump():
            clock.now += 3500

        site.on_post = jump
        output, code = self.run_main(site, "tout", execute=True, session=make_session(now=clock.now), clock=clock,
                                     mutate=lambda c: c["discard"].update(require_existing_tag=False))
        self.assertEqual(code, 0)
        self.assertGreaterEqual(site.refreshes, 1)
        self.assertEqual((site.sells(), site.discards()), ([AMPHIBIA, FACE], [CHACANA]))

    def test_server_side_refresh_is_adopted(self):
        site = standard_site(tags=SORTED)
        new = make_session(refresh="server")
        site.overrides[("POST", "/api/marketplace")] = FakeResponse(
            201, {"auction_id": "a1"}, set_cookies=w.session_cookies(new, REF))
        output, code = self.run_main(site, "vendre", execute=True)
        self.assertEqual(code, 0)
        self.assertEqual(self.store.load()["refresh_token"], "server")
        self.assertEqual(len(site.sells()), 2)

    def test_unreadable_server_session_stops_before_next_write(self):
        site = standard_site(tags=SORTED)
        site.overrides[("POST", "/api/marketplace")] = FakeResponse(
            201, {"auction_id": "a1"}, set_cookies={f"sb-{REF}-auth-token.0": "base64-!!!"})
        output, code = self.run_main(site, "vendre", execute=True)
        self.assertEqual((code, len(site.sells())), (1, 1))

    def test_lost_server_session_is_reported_in_dry_run(self):
        site = standard_site(tags=SORTED)
        site.overrides[("GET", "/api/marketplace/mine")] = FakeResponse(
            200, {"sellingCount": 0, "maxConcurrentAuctions": 5}, set_cookies={f"sb-{REF}-auth-token.0": "base64-!!!"})
        output, code = self.run_main(site, "vendre")
        self.assertEqual(code, 1)
        self.assertIn("login", output)

    def test_login_saves_session_from_messy_paste(self):
        site = standard_site()
        output, code = self.run_main(site, "login", session=None, stdin="Cookie: " + fake_cookie() + "\n")
        self.assertEqual(code, 0)
        self.assertIn("Compte testeur enregistré", output)
        self.assertEqual(self.store.load()["refresh_token"], "r0")
        self.assertEqual(os.stat(self.store.path).st_mode & 0o777, 0o600)
        self.assertEqual(len(site.gets("/api/marketplace/mine")), 1)

    def test_login_rejects_bad_cookie(self):
        output, code = self.run_main(standard_site(), "login", session=None, stdin="a=1\n")
        self.assertIn("Cookie invalide", str(code))
        self.assertIsNone(self.store.load())

    def test_login_rejects_cookie_without_account_id(self):
        session = {**make_session(), "access_token": f"a.{b64({'exp': 1})}.c"}
        output, code = self.run_main(standard_site(), "login", session=None, stdin=fake_cookie(session=session) + "\n")
        self.assertIn("Cookie invalide", str(code))

    def test_login_ctrl_c_is_clean(self):
        with mock.patch("wikimasters.read_secret", side_effect=KeyboardInterrupt):
            output, code = self.run_main(standard_site(), "login", session=None, stdin="")
        self.assertIn("Connexion annulée", str(code))


class PtyMixin:
    def run_secret_in_pty(self, chunks, settle=0.2):
        """Lance read_secret dans un vrai pseudo-terminal, colle chunks, renvoie (résultat, reste non lu)."""
        master, slave = os.openpty()
        code = ("import sys, os, json, wikimasters as w; s = w.read_secret('>'); "
                "fd = sys.stdin.fileno(); os.set_blocking(fd, False)\n"
                "try:\n    rest = os.read(fd, 65536).decode()\nexcept BlockingIOError:\n    rest = ''\n"
                "sys.stderr.write(json.dumps([s, rest]))")
        proc = subprocess.Popen([sys.executable, "-c", code], cwd=ROOT, stdin=slave, stdout=slave, stderr=subprocess.PIPE)
        os.close(slave)
        buf = b""
        while b">" not in buf:
            buf += os.read(master, 100)
        time.sleep(settle)
        for chunk in chunks:
            os.write(master, chunk)
            time.sleep(0.05)
        _, err = proc.communicate(timeout=10)
        os.close(master)
        return json.loads(err.decode())


@unittest.skipUnless(hasattr(os, "openpty"), "pas de pseudo-terminal")
class ReadSecretTerminalTest(PtyMixin, unittest.TestCase):
    def test_multi_line_paste_is_fully_consumed(self):
        secret, rest = self.run_secret_in_pty([b"sb-x-auth-token.0=AAAA;\n", b"sb-x-auth-token.1=SECRET\n"])
        self.assertEqual(w.normalize_cookie_header(secret), "sb-x-auth-token.0=AAAA; sb-x-auth-token.1=SECRET")
        self.assertEqual(rest, "")

    def test_bracketed_paste_and_backspace_are_cleaned(self):
        secret, _ = self.run_secret_in_pty([b"\x1b[200~abcx\x7f\x1b[201~\n"])
        self.assertEqual(secret.strip(), "abc")


class SessionRobustnessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = w.SessionStore(pathlib.Path(self.tmp.name) / "session.json")
        self.site_cfg = load_cfg()["site"]

    def test_save_failure_during_refresh_is_a_clear_fatal_error(self):
        site = standard_site()
        client = w.WikiMastersClient(self.site_cfg, self.store, session=make_session(exp_offset=-10))
        with mock.patch("requests.Session.request", side_effect=site), \
                mock.patch("os.replace", side_effect=OSError(28, "No space left on device")), mock.patch("time.sleep"):
            with self.assertRaises(w.ApiError) as ctx:
                client.auction_slots()
        self.assertTrue(ctx.exception.fatal)
        self.assertIn("impossible d'écrire", str(ctx.exception))
        self.assertIn("login", str(ctx.exception))
        self.assertEqual(client.session["refresh_token"], "r0")  # rien adopté sans écriture réussie

    def test_save_retries_windows_lock(self):
        real_replace = os.replace
        calls = []

        def flaky(src, dst):
            calls.append(1)
            if len(calls) < 3:
                raise PermissionError(13, "locked")
            return real_replace(src, dst)

        with mock.patch("os.replace", side_effect=flaky), mock.patch("time.sleep"):
            self.store.save(make_session())
        self.assertEqual((len(calls), self.store.load()["refresh_token"]), (3, "r0"))
        self.assertEqual([p.name for p in self.store.path.parent.iterdir()], ["session.json"])

    def test_shrinking_chunked_server_cookie_is_adopted(self):
        old = make_session()
        old["user"]["padding"] = "x" * 9000
        client = w.WikiMastersClient(self.site_cfg, self.store, session=old)
        self.assertGreaterEqual(len(client.cookies), 3)
        new = make_session(refresh="srv1")
        new["user"]["padding"] = "y" * 3000
        new_cookies = w.session_cookies(new, REF)
        self.assertLess(len(new_cookies), len(client.cookies))
        client._adopt_server_cookies(FakeResponse(200, {}, set_cookies=new_cookies))
        self.assertIsNone(client.session_lost)
        self.assertEqual((client.session["refresh_token"], self.store.load()["refresh_token"]), ("srv1", "srv1"))

    def test_malformed_session_values_give_login_message(self):
        # expires_at mal formé : ignoré, l'expiration du jeton lui-même est utilisée (pas de plantage).
        self.store.save({**make_session(), "expires_at": ["x"]})
        self.assertGreater(w.WikiMastersClient(self.site_cfg, self.store).time_left(), 3000)
        for bad in ({**make_session(), "access_token": f"a.{b64([1, 2])}.c"}, {**make_session(), "access_token": 42}):
            self.store.save(bad)
            with self.assertRaises(SystemExit) as ctx:
                w.WikiMastersClient(self.site_cfg, self.store)
            self.assertIn("login", str(ctx.exception.code))

    def test_clock_skew_uses_relative_expiry_after_refresh(self):
        site = standard_site()
        skewed = make_session(exp_offset=-10)
        client = w.WikiMastersClient(self.site_cfg, self.store, session=skewed)
        fresh = {**make_session(exp_offset=-7200, refresh="r9"), "expires_in": 3600}  # horloge du serveur en retard
        site.overrides[("POST", "/auth/v1/token")] = FakeResponse(200, fresh)
        with mock.patch("requests.Session.request", side_effect=site), mock.patch("time.sleep"):
            client.auction_slots()
            client.auction_slots()
        # Une seule demande de renouvellement malgré l'horloge décalée (pas de boucle).
        self.assertEqual(len([c for c in site.calls if "/auth/" in c[1]]), 1)


if __name__ == "__main__":
    unittest.main()
