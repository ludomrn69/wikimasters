# Bot WikiMasters

Ce script trie automatiquement les cartes de **votre** compte [WikiMasters](https://www.wiki-masters.com) :

- il lit le prix moyen de vente de chaque carte et lui pose une **étiquette de prix** (`defausse`, `+5`, `+10`, `+100`, `+500`, `+1000`, ou `inconnu` si la carte ne s'est jamais vendue) ;
- il **met aux enchères** les cartes qui valent 5 wikicoins ou plus, et relance moins cher celles qui ne trouvent pas preneur, sauf la nuit (de 23 h à 8 h), quand presque personne n'achète ;
- il **défausse** les cartes qui valent moins de 5, ainsi que les cartes jamais vendues de rareté C, PC ou R (une défausse rapporte 1 wikicoin) ;
- sur demande, il **ouvre vos boosters** et **achète aux enchères** les cartes qui vous intéressent (mots-clés), en misant à la dernière seconde sans dépasser leur prix moyen (voir [Boosters et achats](#boosters-et-achats-aux-enchères)).

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

Le premier passage est long : le script lit le prix de chaque carte, avec une pause d'environ 2 secondes entre deux lectures (compter environ 2 h pour 3000 cartes). Les prix sont ensuite gardés 24 h, donc les passages suivants vont beaucoup plus vite.

Le script empêche l'ordinateur de se mettre en veille tout seul, mais **pas quand on ferme le capot sur batterie, ni quand la batterie se vide** : l'ordinateur s'endort, et le script reste figé jusqu'au réveil (ni boosters, ni ventes, ni défausses, ni achats). Pour le laisser tourner la nuit, **branchez l'ordinateur et laissez le capot ouvert** (l'écran peut s'éteindre). Si une veille a eu lieu, le résumé du passage (et Telegram) le signale.

## Les commandes

| Commande | Ce qu'elle fait |
|---|---|
| `./wm tout` | Les trois étapes ci-dessous (vente et défausse des cartes déjà étiquetées, analyse, puis vente et défausse des nouvelles) |
| `./wm analyser` | Pose les étiquettes de prix |
| `./wm vendre` | Met des cartes aux enchères (5 en même temps au plus, c'est la limite du site) |
| `./wm defausser` | Défausse les cartes `defausse` et les `inconnu` de rareté C, PC ou R |
| `./wm boosters` | Ouvre les boosters disponibles |
| `./wm acheter` | Mise à la dernière seconde sur les enchères du marché qui correspondent à vos mots-clés |
| `./wm login` | Ajoute un compte, ou le reconnecte |
| `./wm comptes` | Liste vos comptes enregistrés |

Options, à ajouter à `tout`, `analyser`, `vendre`, `defausser`, `boosters` ou `acheter` :

| Option | Effet |
|---|---|
| `--execute` | Agir pour de vrai. Sans cette option, c'est une simulation. |
| `--loop` | Recommencer toutes les 15 minutes (`--loop 30` : toutes les 30). Indispensable pour vendre une grosse collection, 5 enchères à la fois. |
| `--compte NOM` | Choisir le compte, si vous en avez plusieurs. |
| `--fresh` | Relire tous les prix sur le site au lieu de ceux gardés depuis moins de 24 h. |
| `--verbose` | Tout afficher, y compris les cartes protégées. |

## Ce que fait `tout`, étape par étape

**0. Avant le tri** (seulement si vous les avez activés) : `tout` ouvre les boosters disponibles (`packs.in_tout: true`), puis pose l'étiquette de protection sur les cartes gagnées aux enchères depuis le passage précédent.

**L'ordre du passage.** `tout` vend (étape 2) et défausse (étape 3) d'abord les cartes **déjà étiquetées** par les passages précédents, puis analyse (étape 1) la collection, puis vend et défausse les cartes qui viennent de recevoir leur étiquette. Les ventes et les défausses commencent donc tout de suite, même quand l'analyse doit relire le prix de toute la collection (une heure ou plus quand les prix gardés ont expiré). Les limites (5 ventes, 200 défausses par passage) valent pour le passage entier. Une carte qu'une règle `auto_tags` va protéger n'est jamais vendue ni défaussée, même avant l'analyse.

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

**2. Vendre.** Le script fait d'abord le bilan des enchères précédentes (vendue ou invendue). Il relance ensuite les invendus à 80 % de la mise précédente (3 essais au plus), puis remplit les places libres avec des cartes tirées au hasard, à 75 % de leur prix moyen, pour 30 minutes. Seul le prix des cartes tirées est lu : la vente n'attend pas la relecture des centaines d'autres.

**La nuit, il ne vend rien.** De 23 h à 8 h (heure de l'ordinateur), il ne met aucune carte en vente et ne relance aucun invendu, et il ne lance pas d'enchère qui se terminerait dans ce créneau (donc plus rien après 22 h 30 avec des enchères de 30 minutes). Il continue d'ouvrir les boosters, de poser les étiquettes, de défausser et d'acheter. Avec `--loop`, dès qu'un passage de nuit n'a plus rien fait (ni défausse, ni étiquette), le passage suivant n'a lieu qu'à la fin de la nuit au lieu de revenir toutes les 15 minutes ; les achats, eux, continuent toute la nuit, et une carte gagnée reçoit sa protection au début de ce passage, avant tout tri. Pour changer les heures, dans `perso.yaml` :

```yaml
sell:
  night: { start: "00:00", end: "07:00" }   # heures entre guillemets ; null = vendre à toute heure
```

**Il peut aussi défausser les invendues bon marché** au lieu de les relancer : relancée, une carte qui vaut 5 partirait à 1 ou 2 wikicoins et occuperait une place d'enchère pour à peu près ce que rapporte une défausse. Ce sont les cartes mises aux enchères sans trouver d'acheteur (même plusieurs fois) qui valent moins de `unsold_max_value` (prix relu sur le site juste avant), de jour comme de nuit. Les cartes jamais mises en vente ne sont pas touchées : elles attendent leur tour. Désactivé par défaut ; dans `perso.yaml` :

```yaml
discard:
  unsold_max_value: 10   # défausser au lieu de relancer les invendues qui valent moins de 10 (null = toujours relancer)
```

**3. Défausser.** Par lots de 10 cartes : le prix des 10 cartes est relu sur le site, puis elles sont défaussées, et ainsi de suite. Une carte qui vaut maintenant 5 ou plus est épargnée. Le script fait au plus 200 défausses par passage ; la suite au passage suivant. Le site répond lentement (environ 7 secondes par prix) : comptez environ 40 minutes pour 200 défausses.

**Pour garder une carte**, ajoutez-lui l'étiquette `garder` sur le site. Retirer `defausse` ne suffit pas : le passage suivant la remettrait.

## Boosters et achats aux enchères

Ces deux fonctions sont désactivées par défaut. Activez-les dans `perso.yaml` :

```yaml
packs:
  in_tout: true          # « tout » ouvre d'abord les boosters disponibles

buy:
  keywords: ["lyon"]     # cartes dont le titre ou la catégorie contient « lyon »
  tag: "lyon"            # étiquette posée sur les cartes gagnées (doit être dans protection.tags)

protection:
  tags: ["lyon"]
```

**Boosters.** `./wm boosters --execute` ouvre les boosters disponibles, 10 au plus, espacés d'environ 12 secondes. Si le site répond « Plus de paquets disponibles » (il en annonce parfois plus qu'il n'en accorde), le script arrête d'ouvrir des boosters et continue le passage. Si le site demande la vérification anti-robot, le script s'arrête et attend 1 h avant de réessayer (puis 2 h, 4 h…). Passez la vérification à la main sur le site pour reprendre plus tôt.

**Achats.** Toutes les 5 minutes, le script cherche sur le marché les annonces qui se terminent bientôt et dont le titre ou la catégorie contient un mot commençant par un de vos mots-clés (« lyon » trouve « lyonnais » ou « To-Lyon », pas « Elyon »). Pour chacune :

- il ignore les cartes que vous avez déjà, celles déjà trop chères, et celles qui ne se sont jamais vendues (prix inconnu), sauf avec `buy.buy_unknown: true` ;
- **20 secondes avant la fin**, il mise le minimum accepté par le site (mise actuelle + 10 %, + 1) ;
- **6 secondes avant la fin**, il revérifie : si quelqu'un l'a dépassé, il remise, toujours sous le plafond ;
- si sa mise est refusée parce qu'un autre joueur vient de miser au même instant (« Mise trop basse »), il remise **aussitôt** le nouveau minimum donné par le site (2 fois au plus), toujours sous le plafond ;
- une mise de dernière minute fait prolonger l'enchère par le site : le script la suit jusqu'à sa vraie fin, et remise à chaque fois que quelqu'un le dépasse ;
- le plafond est le **prix moyen** de la carte multiplié par `buy.price_factor` (1 par défaut, 5 pour aller jusqu'à 5 fois le prix moyen), et `buy.max_price` peut ajouter une limite absolue. Avec `price_factor: null`, il n'y a plus de plafond lié au prix ;
- une carte gagnée reçoit l'étiquette `buy.tag` au passage suivant, avant toute analyse : elle n'est donc jamais revendue ni défaussée. Tant que le résultat d'une mise n'est pas connu, la carte n'est pas touchée. Ce résultat est lu au début de chaque passage `--execute` (même sans `--loop`), et même pendant une pause anti-robot des achats.
- une même carte a souvent plusieurs annonces : le script n'en suit qu'une, et si elle devient trop chère ou qu'il la perd, il passe à une autre annonce de la même carte.

**Des étiquettes selon la carte.** `buy.keyword_tags` donne une ou plusieurs étiquettes (à mettre aussi dans `protection.tags`) aux cartes dont le titre ou la catégorie contient un texte ; les autres reçoivent `buy.tag`. La recherche porte sur le titre et la courte description de la carte : « pnl » trouve « PNL (groupe) », mais aussi « Au DD (chanson de PNL) » ou « Deux Frères (album de PNL) ». Avec `"olympique lyonnais": ["lyon", "OL"]`, une « Saison 2019-2020 de l'Olympique lyonnais » trouvée par « lyon » reçoit les deux.

**Des cartes par leur nom.** Un footballeur est décrit seulement comme « footballeur français », sans son club : pour acheter les joueurs de l'OL, il faut donner leurs noms dans `buy.names` ; ils reçoivent `buy.names_tags` (sinon `buy.tag`). Chaque recherche prend parfois 30 secondes : chaque nom est cherché à son tour, environ toutes les `buy.names_minutes` (60 par défaut), et toutes ses annonces jusqu'à son tour suivant sont suivies.

**Répartir les achats entre vos comptes.** Tous les comptes de `buy.accounts` achètent, et une même annonce n'est suivie que par l'un d'eux. Pour qu'ils ne fassent pas les mêmes recherches, `buy.by_account` remplace les mots-clés et les noms d'un compte : moins de recherches et de mises par compte, donc moins de vérifications anti-robot.

```yaml
buy:
  accounts: ["compte1", "compte2"]
  keywords: ["lyon", "pnl", "plk"]        # compte1
  by_account:
    compte2: { keywords: [], names: ["Rayan Cherki", "Maxence Caqueret"] }
```

**Les cartes que vous avez déjà** reçoivent les mêmes étiquettes avec `auto_tags` (posées par l'analyse, même sur une carte déjà protégée). En YAML, `&ol` nomme la liste et `*ol` la reprend, pour ne pas l'écrire deux fois :

```yaml
buy:
  keywords: ["lyon", "pnl", "plk"]
  tag: "lyon"
  keyword_tags: { pnl: "pnl", plk: "plk", "olympique lyonnais": ["lyon", "OL"] }
  names: &ol ["Rayan Cherki", "Maxence Caqueret"]
  names_tags: ["lyon", "OL"]

auto_tags:
  - { tag: "OL", when: { name_contains: *ol } }
  - { tag: "lyon", when: { name_contains: *ol } }

protection:
  tags: ["garder", "favori", "lyon", "OL", "pnl", "plk"]
```

Pour miser à la seconde près, le script doit tourner en continu :

```bash
./wm tout --execute --loop     # tri + achats entre deux passages (le plus simple)
./wm acheter --execute --loop  # achats seulement
./wm acheter                   # simulation : montre sur quoi il miserait, sans miser
```

Sans `--loop`, `acheter` suit les enchères qui se terminent dans les 10 prochaines minutes, puis s'arrête. `tout` sans `--loop` n'achète rien (il lit seulement le résultat des mises déjà posées). Les mises et les achats sont suivis dans `comptes/<nom>/achats.json`.

**Un seul compte acheteur.** Avec plusieurs comptes, `buy.accounts` choisit ceux qui achètent ; les autres ne lisent même pas le marché (leurs mises déjà posées sont quand même suivies jusqu'au résultat, pour protéger une carte gagnée) :

```yaml
buy:
  accounts: ["moncompte"]   # noms affichés par ./wm comptes ; sans cette ligne, tous vos comptes achètent
```

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

Deux comptes peuvent tourner en même temps, dans deux terminaux. Ils sollicitent alors deux fois plus le site : si les erreurs 503 ou 525 reviennent souvent, faites-les tourner l'un après l'autre.

Pour qu'un seul d'entre eux achète, voir `buy.accounts` dans [Boosters et achats](#boosters-et-achats-aux-enchères). S'ils font tous les deux des achats, ils se partagent les annonces : le premier qui repère une enchère la suit, l'autre la laisse (« suivie par votre compte … »). Ils ne misent donc jamais l'un contre l'autre. Ce partage passe par `comptes/achats_en_cours.json` : les comptes doivent tourner sur le même ordinateur. Une simulation ne réserve rien.

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
| `nouvel essai dans … s` | Le site ne répond pas ou est surchargé (erreurs 500, 503, 525, délai dépassé). Le script attend, réessaie tout seul et ralentit un moment. Rien à faire. Une erreur isolée corrigée au premier nouvel essai n'est pas affichée : elle est seulement comptée dans le résumé (`requête(s) renvoyée(s)`). |
| `PLANTAGE` | Un bug, ou une réponse du site que le script ne sait pas lire. Avec `--loop`, seul le passage en cours (ou l'enchère concernée, pendant les achats) est abandonné : le script continue et réessaie au passage suivant. Sans `--loop`, il s'arrête. Copiez le message (il dit où ça a planté) pour le faire corriger. |
| `places d'enchère illisibles` | Le site, surchargé, n'a pas donné le nombre de places d'enchère libres. Les ventes attendent le passage suivant, le reste continue. Si ça dure plusieurs heures, le site a peut-être changé : signalez-le. |
| `enchère retirée du site` | Le site supprime les enchères terminées au bout de quelques minutes. Le résultat est alors déduit de la collection : carte revenue = invendue (achat : carte arrivée = gagnée), sinon vendue (achat : perdue). Le prix final n'est plus connu. |
| `le site demande une vérification anti-robot` | Le site soupçonne un robot. Le script met ce type d'action en pause (1 h, puis 2 h, 4 h, 8 h si le site insiste) et continue le reste. Pour reprendre plus tôt : ouvrez le site avec ce compte, faites l'action à la main (passez la vérification si elle s'affiche), puis relancez le script. |
| `permission denied: ./wm` | Lancez `sh wm login`, ou une fois `chmod +x wm`. |
| `l'ordinateur s'est mis en veille` | Le script était figé pendant la veille (capot fermé sur batterie, batterie vide) : rien n'a été fait pendant ce temps. Branchez l'ordinateur et laissez le capot ouvert. |
| `Le compte … n'achète pas` ou `buy.accounts : compte(s) inconnu(s)` | Ce compte n'est pas dans `buy.accounts`, ou un nom y est mal écrit : comparez avec `./wm comptes`. |

## Le journal

Chaque action réelle est inscrite dans **`journal.csv`** (date, compte, carte, prix, résultat, solde), qui s'ouvre directement dans Excel ou Numbers. Les ventes terminées y apparaissent avec leur prix final, les cartes obtenues dans un booster avec l'action `booster`, les mises sur le marché avec `mise` et les enchères gagnées avec `achat`. Si le fichier est ouvert dans Excel pendant un passage, les nouvelles lignes vont dans `journal_secours.csv` et sont recopiées au passage suivant.

## Garde-fous

- Chaque commande est une simulation tant que vous n'ajoutez pas `--execute`.
- Le prix d'une carte est relu sur le site juste avant de la défausser.
- Une carte « inconnu » (jamais vendue) qui a trouvé preneur depuis n'est défaussée que si son prix reste sous `discard.max_value` ; avec `max_value: null`, elle est gardée.
- Une carte qui porte à la fois une étiquette de vente et `defausse` est laissée de côté.
- Une ligne de la collection qui regroupe plusieurs exemplaires (×2, ×3…) n'est jamais touchée.
- Une carte déjà en vente n'est pas remise en vente. Une enchère que vous annulez vous-même n'est pas relancée.
- Deux passages `--execute` ne peuvent pas tourner en même temps sur le même compte.
- Le script fait une pause entre chaque action et chaque lecture, et il s'arrête après plusieurs échecs d'affilée.
- Aucune mise en vente la nuit (`sell.night`, de 23 h à 8 h), et aucune enchère ne s'y termine.
- Les mises en vente sont espacées de 45 secondes (`safety.auction_delay_seconds`) : enchaînées en quelques secondes, elles déclenchent la vérification anti-robot du site.
- Si le site demande une vérification anti-robot, le script n'insiste pas : ce type d'action attend, le reste continue.
- Si le serveur du site peine, le script attend, réessaie et ralentit. Une défausse ou une vente n'est renvoyée que si le serveur n'a certainement rien fait. Pendant ces attentes, les achats continuent de miser ; une mise, elle, n'attend que quelques secondes.
- Une mise sur le marché ne dépasse jamais le prix moyen de la carte × `buy.price_factor` (ni `buy.max_price`). Le script mise toujours le minimum accepté, 20 s puis 6 s avant la fin, et aussitôt si sa mise est refusée parce qu'un autre joueur vient de miser.
- Une carte gagnée aux enchères reçoit son étiquette de protection avant d'être analysée, et une carte dont le résultat n'est pas encore connu n'est pas touchée : le script ne revend pas ce qu'il vient d'acheter.
- Une simulation lancée pendant qu'un `--execute` tourne sur le même compte reprend la session qu'il a renouvelée, au lieu de la renouveler une seconde fois (ce qui la ferait révoquer).
- Un résumé s'affiche toujours à la fin, même après une interruption.

## À savoir

Ce script automatise un compte de jeu. Le site a une protection anti-robot et peut suspendre les comptes qui en utilisent. Lisez ses conditions d'utilisation : vous l'utilisez à vos risques.

## Pour les développeurs

Sans le lanceur : `python wikimasters.py tout` (après `pip install -r requirements.txt`). Les tests :

```bash
.venv/bin/python -m unittest discover -s tests
```
