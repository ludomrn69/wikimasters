# Bot WikiMasters

Ce script trie automatiquement les cartes de **votre** compte WikiMasters, en s'appuyant sur les **étiquettes** du site :

1. **`analyser`** lit le prix moyen de vente de chaque carte et pose une étiquette de prix : `defausse` (moins de 10 wikicoins), `+10`, `+100`, `+500` ou `+1000` ;
2. vous vérifiez ces étiquettes sur le site. **Pour garder une carte, ajoutez-lui une étiquette de protection** (par exemple `garder`). Retirer `defausse` ne suffit pas : le prochain `analyser` la remettrait ;
3. **`vendre`** fait le bilan des enchères précédentes (vendue / invendue), **relance les invendus moins cher** (mise précédente × 0,8), puis met aux enchères des cartes étiquetées à vendre, **tirées au hasard** (le tirage est le même toute la journée : le dry-run montre exactement les cartes que `--execute` vendra), à 75 % de leur prix moyen et dans la limite des 5 enchères simultanées du site ;
4. **`defausser`** défausse les cartes étiquetées `defausse` (une défausse rapporte 1 wikicoin), après avoir relu leur prix sur le site.

Chaque action réelle est inscrite dans **`journal.csv`** (date, carte, prix, résultat, solde), qui s'ouvre directement dans Excel ou Numbers. Les ventes terminées y apparaissent avec leur prix final : de quoi suivre vos gains. S'il est ouvert dans Excel pendant un passage, les nouvelles lignes vont dans `journal_secours.csv` et sont recopiées dans `journal.csv` au passage suivant : rien n'est perdu.

`tout` enchaîne les trois. Par sécurité, une carte qu'`analyser` vient d'étiqueter `defausse` n'est défaussée qu'au passage **suivant** : vous avez le temps de vérifier. Le script **ne touche jamais** aux cartes protégées, qu'elles le soient par une étiquette (`lyon`, `garder`…), une étoile, l'état shiny, la rareté ou un échange en cours.

Toutes les règles se trouvent dans `config.yaml` : tranches de prix, étiquettes, prix de vente, durée, protections, limites…

## Installation

Il faut Python 3.10 ou plus récent.

**Mac / Linux**

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**Windows (PowerShell)**

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Si PowerShell refuse d'activer l'environnement, lancez une fois `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`. Vous pouvez aussi vous passer de l'activation et appeler directement `.venv\Scripts\python wikimasters.py …`.

Sous Windows, utilisez PowerShell ou l'invite de commandes pour `login`, pas Git Bash.

## Connexion (une seule fois)

Le script n'utilise pas votre mot de passe. Il garde sa propre session dans `session.json` et la renouvelle tout seul, comme le fait le navigateur.

1. Ouvrez une **fenêtre de navigation privée** et connectez-vous sur https://www.wiki-masters.com.
2. Ouvrez les outils développeur (`Cmd+Option+I` sur Mac, `F12` sur Windows), puis l'onglet **Network** (Réseau).
3. Allez sur la page **Collection**, tapez `my-collection` dans le filtre, puis rechargez la page.
4. Cliquez sur la requête `my-collection` (méthode GET, domaine www.wiki-masters.com).
5. Dans **Request Headers**, faites un clic droit sur la ligne **cookie**, puis choisissez **Copy value**.
6. Dans le terminal, lancez la commande ci-dessous et collez quand elle le demande. Rien ne s'affiche pendant le collage, c'est normal.

   ```bash
   python wikimasters.py login
   ```

7. **Fermez la fenêtre privée sans vous déconnecter.**

Pourquoi une fenêtre privée ? Le script doit avoir une session à lui. S'il partageait celle de votre navigateur habituel, ils renouvelleraient la même session chacun de leur côté, et le site finirait par les déconnecter tous les deux.

Si le script affiche « session révoquée », par exemple après une déconnexion depuis le site, refaites simplement `python wikimasters.py login`.

> ⚠️ `session.json` donne accès à votre compte. **Ne l'envoyez à personne et ne le committez jamais** (il est déjà exclu par `.gitignore`). Ne collez jamais votre cookie dans un chat, un message ou une issue.

## Utilisation

Chaque commande est d'abord un **dry-run** : elle affiche ce qu'elle ferait, sans rien modifier. Ajoutez `--execute` pour agir réellement.

```bash
python wikimasters.py analyser              # voir les étiquettes de prix qui seraient posées
python wikimasters.py analyser --execute    # les poser
python wikimasters.py vendre                # voir les ventes prévues
python wikimasters.py vendre --execute      # lancer les enchères (jusqu'à 5 places libres)
python wikimasters.py defausser             # voir les défausses prévues
python wikimasters.py defausser --execute   # défausser
python wikimasters.py tout --execute        # les trois à la suite
python wikimasters.py analyser --fresh      # relire tous les prix sur le site (voir plus bas)
python wikimasters.py tout --execute --loop 30   # recommencer toutes les 30 minutes (Ctrl+C pour arrêter)
```

Avec beaucoup de cartes, la première lecture des prix prend du temps. Les prix lus sont gardés 6 heures dans `prix_cache.json` (`price.cache_hours`), même après un dry-run, pour qu'un `--execute` lancé juste après ne relise pas tout : `vendre` réutilise ceux lus par `analyser` (sauf `sell.fresh_price: true`). `defausser` relit toujours les prix sur le site avant de défausser (`discard.fresh_price`). Un dry-run ne modifie rien sur le site et n'écrit que ce cache.

### Actualiser toutes les étiquettes de prix

`analyser` réévalue à chaque passage **toutes** les cartes non protégées, y compris celles qui portent déjà `defausse`, `+10`, `+100`… Si une carte a changé de tranche, l'ancienne étiquette est retirée et la nouvelle posée ; sinon rien ne change. Les cartes protégées (`lyon`, `garder`, étoile, shiny, raretés protégées, échange en cours) ne sont jamais touchées.

Par défaut, les prix viennent du cache (6 h). Pour tenir compte de leur évolution, ajoutez `--fresh` : tous les prix sont relus sur le site.

```bash
python wikimasters.py analyser --fresh      # voir les étiquettes qui changeraient
python wikimasters.py analyser --execute    # les changer (réutilise les prix que --fresh vient de lire)
```

### Relancer automatiquement

`--loop 30` recommence la commande toutes les 30 minutes, jusqu'à Ctrl+C. Un passage qui échoue (réseau, site indisponible) est retenté au cycle suivant ; une session révoquée arrête la boucle.

Ajoutez `--verbose` pour tout voir, y compris les cartes protégées et toutes celles qui attendent une place d'enchère.

Garde-fous :
- `defausser` relit le prix sur le site juste avant de défausser : une carte étiquetée `defausse` dont le prix moyen est remonté à 10 ou plus n'est pas défaussée. `vendre` ne vend pas une carte étiquetée `+10` qui vaut moins de 10 (prix du cache, sauf `sell.fresh_price: true`). Dans les deux cas, relancez `analyser --fresh` ;
- une carte déjà en vente n'est pas remise en vente ; s'il n'y a aucune place d'enchère libre, `vendre` ne lit même pas les prix ;
- une enchère que vous annulez vous-même sur le site n'est pas relancée ; une relance est toujours moins chère que la précédente, et jamais plus chère que la mise normale au prix actuel ;
- deux passages `--execute` ne peuvent pas tourner en même temps (fichier `.wikimasters.lock`, repris automatiquement si le passage qui le tenait a disparu) ;
- une carte qui porte à la fois une étiquette de vente et `defausse` est laissée de côté ;
- le script s'arrête après `safety.max_actions_per_run` actions et indique combien il en reste ; relancez-le pour continuer ;
- il fait une pause entre chaque action et chaque lecture de prix ;
- si une action échoue, la carte concernée est sautée et le passage continue ; après plusieurs échecs d'affilée, il s'arrête ;
- un résumé s'affiche toujours à la fin, même après une interruption.

Pour utiliser un autre fichier de règles : `python wikimasters.py vendre --config autre.yaml`.

## Modifier les règles

Commencez par adapter `config.yaml` à votre compte, en particulier `protection.tags`. Chaque paramètre y est expliqué en commentaire. Les principaux :

| Section | Rôle |
|---|---|
| `protection` | Étiquettes, raretés, mots dans le nom, étoile et shiny qui protègent une carte |
| `price_tags.bands` | Tranches de prix et étiquette de chacune (`defausse`, `+10`, `+100`…) |
| `price_tags.remove_outdated` | Retirer l'ancienne étiquette de prix quand une carte change de tranche |
| `auto_tags` | Étiquettes en plus selon le nom, la rareté, l'état shiny ou la valeur |
| `discard` | Étiquettes à défausser, prix maximal pour défausser, limite par passage |
| `sell` | Étiquettes à vendre, choix des cartes (hasard, plus chères, ordre des étiquettes), mise de départ (× 0,75 arrondi à l'inférieur), durée (10 min à 12 h), nombre d'enchères |
| `sell.relist` | Relance des invendus : réduction (× 0,8), mise minimale, nombre d'essais |
| `journal` | Fichier CSV des actions, séparateur (`;` pour Excel en français) |
| `safety` | Nombre maximal d'actions, pauses, arrêt après N erreurs |
| `telegram` | Notification à la fin de chaque passage (le jeton va dans `secrets.yaml`, voir plus bas) |

Les noms d'étiquettes ignorent les majuscules, les accents, les espaces et le `#` affiché par le site. Si une étiquette n'existe pas encore sur votre compte, le script la crée (le dry-run indique lesquelles).

`secrets.yaml`, `journal.csv`, `ventes.json` et `prix_cache.json` sont des fichiers personnels, exclus de git.

Le script refuse de démarrer si la config est incohérente : une liste écrite comme un simple texte, des tranches de prix qui se chevauchent, une même étiquette à la fois protégée et à défausser, plus de 5 enchères, un jeton Telegram écrit dans `config.yaml`…

## Notifications Telegram (facultatif)

À la fin de chaque passage `--execute` (et des dry-runs si `telegram.notify_on_dry_run: true`), le script envoie le résumé sur Telegram, avec le vrai résultat : bilan, arrêt sur erreur, interruption ou plantage.

1. Créez un bot avec [@BotFather](https://t.me/BotFather) et notez son jeton ; obtenez votre identifiant avec [@userinfobot](https://t.me/userinfobot).
2. Créez `secrets.yaml` à côté de `config.yaml` :

   ```yaml
   telegram:
     bot_token: "123456789:ABC..."
     chat_id: "123456789"
   ```

3. Mettez `telegram.enabled: true` dans `config.yaml`.

> ⚠️ Le jeton ne va **jamais** dans `config.yaml` : le script refuse de démarrer s'il l'y trouve. `secrets.yaml` est exclu par `.gitignore`. Ne le partagez pas : quiconque a le jeton peut se servir de votre bot. S'il a fuité, régénérez-le avec `/revoke` dans BotFather.

## À savoir

- Ce script automatise un compte de jeu. Le site a une protection anti-robot et peut suspendre les comptes qui l'utilisent. Lisez ses conditions d'utilisation : vous l'utilisez à vos risques.
- Si une ligne de la collection regroupe plusieurs exemplaires (×2, ×3…), le script n'y touche pas.

## Tests

```bash
python -m unittest discover -s tests
```
