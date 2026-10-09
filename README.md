# Bot WikiMasters

Ce script trie automatiquement les cartes de **votre** compte [WikiMasters](https://www.wiki-masters.com) :

- il lit le prix moyen de vente de chaque carte et lui pose une **étiquette de prix** (`defausse`, `+5`, `+10`, `+100`, `+500`, `+1000`, ou `inconnu` si la carte ne s'est jamais vendue) ;
- il **met aux enchères** les cartes qui valent 5 wikicoins ou plus, et relance moins cher celles qui ne trouvent pas preneur ;
- il **défausse** les cartes qui valent moins de 5, ainsi que les cartes jamais vendues de rareté C, PC ou R (une défausse rapporte 1 wikicoin).

Il ne touche **jamais** aux cartes protégées : étiquette `garder` ou `favori`, étoile, shiny, rareté SR ou L, échange en cours.

## Démarrage rapide

**1. Installer Python** (3.10 ou plus récent) depuis [python.org](https://www.python.org/downloads/). Sous Windows, cochez **« Add python.exe to PATH »** pendant l'installation.

**2. Récupérer le projet**, avec git :

```bash
git clone https://github.com/ludomrn69/wikimasters.git
cd wikimasters
```

ou avec le bouton vert **Code > Download ZIP** sur GitHub, puis ouvrez un terminal dans le dossier décompressé.

**3. Ajouter votre compte** (le premier lancement installe ce qu'il faut, environ une minute) :

| Mac / Linux | Windows (PowerShell) |
|---|---|
| `./wm login` | `.\wm login` |

Suivez les étapes affichées (détail plus bas, dans [Connexion](#connexion)).

**4. Lancer le tri :**

```bash
./wm tout                    # simulation : montre ce qui serait fait, sans rien modifier
./wm tout --execute          # le fait pour de vrai
./wm tout --execute --loop   # recommence toutes les 15 minutes, jusqu'à Ctrl+C
```

Sous Windows, remplacez `./wm` par `.\wm` dans PowerShell (ou `wm` dans l'invite de commandes).

Commencez **toujours** par une simulation pour vérifier ce que le script compte faire : une défausse est définitive.

Le premier passage est long : le script lit le prix de chaque carte, avec une pause d'environ 2 secondes entre deux lectures (compter environ 2 h pour 3000 cartes). Les prix sont ensuite gardés 24 h, donc les passages suivants vont beaucoup plus vite. Le script empêche l'ordinateur de se mettre en veille pendant qu'il tourne.

## Les commandes

| Commande | Ce qu'elle fait |
|---|---|
| `./wm tout` | Les trois étapes ci-dessous, à la suite |
| `./wm analyser` | Pose les étiquettes de prix |
| `./wm vendre` | Met des cartes aux enchères (5 en même temps au plus, c'est la limite du site) |
| `./wm defausser` | Défausse les cartes `defausse` et les `inconnu` de rareté C, PC ou R |
| `./wm login` | Ajoute un compte, ou le reconnecte |
| `./wm comptes` | Liste vos comptes enregistrés |

Options, à ajouter à `tout`, `analyser`, `vendre` ou `defausser` :

| Option | Effet |
|---|---|
| `--execute` | Agir pour de vrai. Sans cette option, c'est une simulation. |
| `--loop` | Recommencer toutes les 15 minutes (`--loop 30` : toutes les 30). Indispensable pour vendre une grosse collection, 5 enchères à la fois. |
| `--compte NOM` | Choisir le compte, si vous en avez plusieurs. |
| `--fresh` | Relire tous les prix sur le site au lieu de ceux gardés depuis moins de 24 h. |
| `--verbose` | Tout afficher, y compris les cartes protégées. |

## Ce que fait `tout`, étape par étape

**1. Analyser.** Chaque carte non protégée reçoit l'étiquette de sa tranche de prix moyen :

| Prix moyen | Étiquette | Ensuite |
|---|---|---|
| moins de 5 | `defausse` | défaussée |
| 5 à 9 | `+5` | vendue |
| 10 à 99 | `+10` | vendue |
| 100 à 499 | `+100` | vendue |
| 500 à 999 | `+500` | vendue |
| 1000 et plus | `+1000` | vendue |
| jamais vendue | `inconnu` | défaussée si de rareté C, PC ou R, gardée sinon |

Si le prix d'une carte change de tranche, l'ancienne étiquette est remplacée.

**2. Vendre.** Le script fait d'abord le bilan des enchères précédentes (vendue ou invendue). Il relance ensuite les invendus à 80 % de la mise précédente (3 essais au plus), puis remplit les places libres avec des cartes tirées au hasard, à 75 % de leur prix moyen, pour 30 minutes.

**3. Défausser.** Le prix est relu juste avant : une carte qui vaut maintenant 5 ou plus est épargnée. Le script fait au plus 200 défausses par passage ; la suite au passage suivant.

**Pour garder une carte**, ajoutez-lui l'étiquette `garder` sur le site. Retirer `defausse` ne suffit pas : le passage suivant la remettrait.

## Connexion

Le script n'utilise pas votre mot de passe. Il garde sa propre session et la renouvelle tout seul, comme le fait le navigateur.

1. Ouvrez une **fenêtre de navigation privée** et connectez-vous sur https://www.wiki-masters.com.
2. Ouvrez les outils développeur (`Cmd+Option+I` sur Mac, `F12` sur Windows), puis l'onglet **Network** (Réseau).
3. Allez sur la page **Collection**, tapez `my-collection` dans le filtre, puis rechargez la page.
4. Cliquez sur la requête `my-collection` (méthode GET, domaine www.wiki-masters.com).
5. Dans **Request Headers**, faites un clic droit sur la ligne **cookie**, puis **Copy value**.
6. Lancez `./wm login` et collez quand c'est demandé. Rien ne s'affiche pendant le collage, c'est normal.
7. **Fermez la fenêtre privée sans vous déconnecter.**

Pourquoi une fenêtre privée ? Le script doit avoir une session à lui. S'il partageait celle de votre navigateur habituel, ils la renouvelleraient chacun de leur côté et le site finirait par les déconnecter tous les deux. Une déconnexion depuis le site peut aussi déconnecter le script : refaites alors `./wm login`.

> ⚠️ Le dossier `comptes/` donne accès à vos comptes. Ne l'envoyez à personne. Ne collez jamais votre cookie dans un chat ou un message.

## Plusieurs comptes

Faites `./wm login` une fois par compte : chacun a son dossier dans `comptes/`, avec sa session et le suivi de ses ventes. Choisissez ensuite le compte à chaque commande :

```bash
./wm comptes                                    # liste les comptes
./wm tout --execute --loop --compte monpseudo   # un compte précis
```

Deux comptes peuvent tourner en même temps, dans deux terminaux.

## Vos réglages : `perso.yaml`

`config.yaml` contient les règles communes : ne le modifiez pas, sinon vos changements entreraient en conflit avec les mises à jour. Mettez vos réglages dans **`perso.yaml`** : copiez `perso.exemple.yaml` sous ce nom, puis décommentez ce que vous voulez changer. Il suffit d'y écrire ce qui change ; tout le reste vient de `config.yaml`. `perso.yaml` n'est jamais envoyé sur GitHub.

Par exemple, pour protéger aussi les cartes étiquetées `lyon` et ne défausser aucune carte jamais vendue :

```yaml
protection:
  tags: ["lyon"]        # s'ajoute à garder et favori
discard:
  unknown_rarities: []  # remplace la liste de config.yaml
```

Une liste écrite dans `perso.yaml` remplace celle de `config.yaml`, sauf dans `protection` : là, elle s'y ajoute. Une protection de `config.yaml` ne peut donc pas être retirée par un oubli dans `perso.yaml`.

Chaque réglage possible est expliqué en commentaire dans `config.yaml`. Le script refuse de démarrer si un réglage est mal orthographié ou incohérent, et il explique pourquoi.

## Notifications Telegram (facultatif)

À la fin de chaque passage `--execute`, le script peut vous envoyer le résumé sur Telegram : bilan, arrêt sur erreur, interruption ou plantage.

1. Créez un bot avec [@BotFather](https://t.me/BotFather) et notez son jeton. Obtenez votre identifiant avec [@userinfobot](https://t.me/userinfobot).
2. Ajoutez dans `perso.yaml` :

   ```yaml
   telegram:
     bot_token: "123456789:AAH..."
     chat_id: "123456789"
     notify_on_dry_run: false   # true = aussi pour les simulations
   ```

Le jeton ne va **jamais** dans `config.yaml` : le script refuse de démarrer s'il l'y trouve. Si votre jeton a fuité, régénérez-le avec `/revoke` dans BotFather.

## En cas de problème

| Message | Que faire |
|---|---|
| `session révoquée` | Refaites `./wm login` pour ce compte. |
| `erreur réseau` ou `renouvellement de session impossible` | Vérifiez la connexion, puis relancez la même commande **sans `--fresh`** : les prix déjà lus sont gardés et le passage reprend où il en était. Avec `--loop`, il reprend tout seul. |
| `Plusieurs comptes enregistrés` | Ajoutez `--compte NOM` (voir `./wm comptes`). |
| `Un autre passage --execute est actif` | Un autre terminal fait déjà tourner ce compte. Attendez qu'il finisse, ou arrêtez-le. |
| `Configuration invalide` | Le message dit quel réglage corriger dans `perso.yaml`. |
| `permission denied: ./wm` | Lancez `sh wm login`, ou une fois `chmod +x wm`. |

## Le journal

Chaque action réelle est inscrite dans **`journal.csv`** (date, compte, carte, prix, résultat, solde), qui s'ouvre directement dans Excel ou Numbers. Les ventes terminées y apparaissent avec leur prix final. Si le fichier est ouvert dans Excel pendant un passage, les nouvelles lignes vont dans `journal_secours.csv` et sont recopiées au passage suivant.

## Garde-fous

- Chaque commande est une simulation tant que vous n'ajoutez pas `--execute`.
- Le prix d'une carte est relu sur le site juste avant de la défausser.
- Une carte qui porte à la fois une étiquette de vente et `defausse` est laissée de côté.
- Une ligne de la collection qui regroupe plusieurs exemplaires (×2, ×3…) n'est jamais touchée.
- Une carte déjà en vente n'est pas remise en vente. Une enchère que vous annulez vous-même n'est pas relancée.
- Deux passages `--execute` ne peuvent pas tourner en même temps sur le même compte.
- Le script fait une pause entre chaque action et chaque lecture, et il s'arrête après plusieurs échecs d'affilée.
- Un résumé s'affiche toujours à la fin, même après une interruption.

## À savoir

Ce script automatise un compte de jeu. Le site a une protection anti-robot et peut suspendre les comptes qui en utilisent. Lisez ses conditions d'utilisation : vous l'utilisez à vos risques.

## Pour les développeurs

Sans le lanceur : `python wikimasters.py tout` (après `pip install -r requirements.txt`). Les tests :

```bash
.venv/bin/python -m unittest discover -s tests
```
