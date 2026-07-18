# Intrigues & Couronne

Jeu de société politique et semi-coopératif, jouable en ligne, en temps réel, à 3–6 joueurs. Application **Flask + Flask-SocketIO** tenant dans un seul fichier serveur (`app.py`) et quatre templates Jinja2.

Un Roi arbitre une Cour de ministres aux rôles fixes et asymétriques. Les ministres gèrent leur robinet (niveau de générosité vs. corruption), la Couronne traverse des crises, des Grandes Entreprises, des Décrets votés, des procès et des tractations secrètes — jusqu'à la Ruine du Royaume ou la victoire au bout de 8 cycles.

---

## Sommaire

- [Concept du jeu](#concept-du-jeu)
- [Architecture technique](#architecture-technique)
- [Installation et lancement local](#installation-et-lancement-local)
- [Variables d'environnement](#variables-denvironnement)
- [Déploiement (Railway)](#déploiement-railway)
- [Structure du projet](#structure-du-projet)
- [Déroulement d'une partie](#déroulement-dune-partie)
- [Les cinq ministères](#les-cinq-ministères)
- [Systèmes de jeu](#systèmes-de-jeu)
- [Référence des événements SocketIO](#référence-des-événements-socketio)
- [Persistance des données](#persistance-des-données)
- [Notes techniques et pièges connus](#notes-techniques-et-pièges-connus)

---

## Concept du jeu

Chaque partie oppose :

- **Un Roi** — sans robinet, sans Or personnel, arbitre suprême. Il peut convoquer des Audiences Royales privées, lancer des Grandes Entreprises, initier un Tribunal, et dispose d'un droit de révocation **limité** (2 révocations pour toute la partie).
- **2 à 5 ministres**, chacun titulaire d'un des cinq ministères (tirés au sort selon le nombre de joueurs), qui choisissent à chaque cycle un **niveau de robinet** (Fermé / Restreint / Normal / Surchauffe) arbitrant leur propre enrichissement contre l'intérêt du Royaume.

Le Royaume est suivi par deux jauges publiques :

- **Stabilité** (0–100) — la paix sociale.
- **Caisses de l'État** (Or public) — les finances publiques.

Si l'une des deux jauges s'effondre (Stabilité ≤ 0 ou Or public < 0), **tous les joueurs perdent** immédiatement. Si le Royaume survit **8 cycles**, la partie se termine sur un décompte de **Puissance Politique** (Or personnel + Influence + Prestige de Cour + bonus de Statut + bonus de Stabilité finale) : le score le plus élevé l'emporte. Le Roi ne participe jamais à ce calcul.

---

## Architecture technique

| Composant | Rôle |
|---|---|
| `app.py` | Serveur Flask + Flask-SocketIO. Contient tout : modèle de données (`GameState`, `PlayerState`), moteur de jeu, persistance, routes HTTP et handlers SocketIO. |
| `templates/base.html` | Layout commun, variables CSS (thème parchemin/Versailles), styles partagés. |
| `templates/index.html` | Page d'accueil : créer ou rejoindre un salon. |
| `templates/room.html` | L'intégralité de l'interface de jeu (rendu côté client en JS vanilla piloté par l'état reçu via SocketIO). |
| `templates/regles.html` | Livre des règles complet, affiché en jeu. |

**Modèle réseau** : le serveur est la seule source de vérité. À chaque action d'un joueur (`socket.emit(...)`), le serveur valide, mute l'objet `GameState` correspondant au salon, persiste l'état, puis renvoie à **chaque joueur individuellement** un `state_update` (filtré pour ce joueur — voir plus bas), plus des notifications ciblées (`personal_log`, `action_error`, etc.). Le client ne fait aucun calcul de règles : il ne fait que refléter l'état reçu et émettre des intentions.

**Concurrence** : un seul worker gunicorn est nécessaire — l'état des parties vit en mémoire (`_rooms`, protégé par un verrou) en plus d'être sauvegardé en base à chaque coup. Plusieurs workers verraient des états différents pour un même salon.

**Boucle de fond** : un thread démon (`timer_loop`) décrémente chaque seconde le chronomètre de discussion de chaque salon actif et déclenche la résolution automatique (décrets, tribunal expiré, passage en phase de décision) quand il arrive à zéro.

---

## Installation et lancement local

```bash
pip install -r requirements.txt
python3 app.py
```

L'application écoute sur `http://localhost:5000`. Sans variable `DATABASE_URL`, elle utilise automatiquement un fichier SQLite local (`game.db`) créé à côté de `app.py`.

### Dépendances (`requirements.txt`)

```
flask
flask-socketio
gevent
gevent-websocket
psycopg2-binary
psycogreen
gunicorn
```

---

## Variables d'environnement

| Variable | Obligatoire | Description |
|---|---|---|
| `SECRET_KEY` | Recommandé en production | Clé secrète Flask (sessions/signature). Une valeur par défaut de développement est utilisée sinon. |
| `DATABASE_URL` | Non | URL de connexion PostgreSQL. Si absente, l'app bascule automatiquement sur SQLite local. |
| `PGSSLMODE` | Non | Mode SSL pour la connexion Postgres (`require` par défaut si `DATABASE_URL` est présente). |
| `SQLITE_PATH` | Non | Emplacement du fichier SQLite (par défaut : `game.db` à côté de `app.py`). Utile pour les tests. |
| `PORT` | Non | Port d'écoute HTTP (par défaut `5000`). |

---

## Déploiement (Railway)

1. Poussez ce dossier sur un dépôt Git, créez un projet Railway dessus.
2. Ajoutez un service **PostgreSQL** — Railway injecte automatiquement `DATABASE_URL`, ce que `app.py` détecte pour basculer de SQLite vers Postgres sans aucune modification de code.
3. Dans les *Settings* du service web (builder par défaut : Railpack), définissez :
   - `RAILPACK_PYTHON_VERSION = 3.12`
   - `RAILPACK_START_CMD = gunicorn -k eventlet -w 1 --timeout 120 app:app`
   - `SECRET_KEY = <valeur aléatoire>`
4. Déployez : Railway fournit une URL publique (`*.up.railway.app`).

> ⚠️ Gardez `-w 1` (un seul worker) : l'état des parties étant tenu en mémoire process, plusieurs workers désynchroniseraient les salons.

---

## Structure du projet

```
intrigues-et-couronne/
├── app.py                  # Serveur : modèle, moteur de jeu, routes, SocketIO
├── requirements.txt
└── templates/
    ├── base.html            # Layout + thème visuel
    ├── index.html            # Accueil (créer / rejoindre un salon)
    ├── room.html              # Interface de jeu complète
    └── regles.html             # Livre des règles
```

---

## Déroulement d'une partie

```
LOBBY
  → assignation des rôles (tirage aléatoire selon l'effectif)
DISCUSSION  (chronométrée, TIMER_DISCUSSION_SECONDS)
  → négociations, Pouvoirs de Cour, Décrets, Dossiers, Audiences, Tribunal
DÉCISION
  → chaque ministre choisit un niveau de robinet (0 à 3)
RAPPORT
  → le Ministre de l'Intérieur reçoit la carte de crise, peut la falsifier
RÉSOLUTION
  → tous les effets s'appliquent (robinets, carte, décret en attente, dossiers en cours, missions urgentes)
  → vérification de la Ruine (Stabilité ≤ 0 ou Or public < 0)
  → cycle suivant, ou décompte final après 8 cycles
```

Un `TRIBUNAL` peut interrompre le déroulement normal si le Roi accuse un ministre (coûte 10 Stabilité à convoquer) ; la table vote alors sa culpabilité.

---

## Les cinq ministères

Chaque ministère a un **niveau de robinet** à 4 paliers (Fermé → Restreint → Normal → Surchauffe), arbitrant Or personnel contre bien public, et un **Pouvoir de Cour** unique.

| Ministère | Statut | Pouvoir de Cour |
|---|:-:|---|
| 📜 Ministre de l'Intérieur | 2 | **La Falsification de Rapport** — peut modifier les chiffres du rapport de crise présenté au Roi (risque de démasquage). |
| 💰 Surintendant des Finances | 3 | **Le Pot-de-Vin Institutionnel** — injecte de l'Or personnel dans un Décret pour acheter secrètement le vote d'un autre ministre. |
| ⛪ Grand Aumônier | 2 | **Les Canaux Clandestins** — seul rôle pouvant créer des chats privés secrets entre joueurs (usage illimité). |
| 🌾 Grand Maître des Subsistances | 1 | **Le Veto Populaire** — annule un Décret en cours, une fois par partie. |
| 🛡️ Connétable | 1 | **L'Arrestation Préventive** — isole un ministre (banni de tous les chats privés) pour un temps donné, une fois par partie. |

Le nombre de ministères actifs dépend de l'effectif total (Roi compris) : de 2 (à 3 joueurs) à 5 (à 6 joueurs et plus), tirés aléatoirement.

---

## Systèmes de jeu

- **Missions Urgentes** (`TASK_CATALOG`) — à chaque carte de crise d'un certain type, un ministre peut se voir proposer une mission ponctuelle, parfois conditionnée à l'aide (payante) d'un autre ministère.
- **Grandes Entreprises Royales** (`ENTERPRISE_CATALOG`) — projets nécessitant l'investissement conjoint de plusieurs ministères pour réussir (ex. faire la guerre nécessite Connétable + Finances).
- **Dossiers Royaux** (`DOSSIERS_CATALOG`) — projets de long terme (plusieurs cycles), financés collectivement, avec récompense ou pénalité à l'échéance.
- **Décrets** — un joueur propose un décret avec effets publics (et, potentiellement, un effet secret ciblé) ; la table vote ; le Surintendant peut corrompre discrètement un vote via son Pouvoir de Cour.
- **Tribunal** — le Roi accuse un ministre (coût : 10 Stabilité) ; la table vote sa culpabilité ; une sentence s'applique.
- **Audience Royale Privée** — le Roi convoque un ministre en tête-à-tête chronométré (`DUREE_AUDIENCE`) ; les autres joueurs sont notifiés qu'une audience a lieu, sans en voir le contenu.
- **Canaux Clandestins** — chats privés créés par l'Aumônier entre joueurs choisis (le Roi ne peut jamais y être invité). Le contenu n'est envoyé qu'aux membres du chat.

---

## Référence des événements SocketIO

### Client → Serveur

| Événement | Description |
|---|---|
| `create_room` / `join_room_event` | Créer / rejoindre un salon. |
| `start_game` | Démarre la partie (hôte uniquement), assigne les rôles. |
| `submit_decision` | Soumet le niveau de robinet choisi pour son ministère. |
| `falsify_report` / `confirm_report` | L'Intérieur falsifie ou confirme le rapport de crise. |
| `resign` / `revoke` | Démission d'un ministre / révocation royale. |
| `use_pouvoir` | Active le Pouvoir de Cour de son ministère. |
| `launch_enterprise` / `investir_entreprise` | Lance / investit dans une Grande Entreprise. |
| `launch_dossier` / `contribuer_dossier` | Lance / contribue à un Dossier Royal. |
| `proposer_decret` / `voter_decret` / `offrir_bribe` | Cycle de vie d'un Décret. |
| `initier_tribunal` / `voter_tribunal` | Cycle de vie d'un Tribunal. |
| `convoquer_audience` / `envoyer_audience` | Cycle de vie d'une Audience Royale. |
| `creer_chat_prive` / `envoyer_message_prive` | Cycle de vie d'un Canal Clandestin. |
| `aider_tache` / `accomplir_tache` | Répondre à une Mission Urgente. |
| `send_chat` | Message dans le salon commun. |
| `next_cycle` | Passe au cycle suivant après résolution. |

### Serveur → Client

| Événement | Description |
|---|---|
| `state_update` | État complet du jeu, **personnalisé par destinataire** (voir ci-dessous). |
| `personal_log` | Journal privé du joueur (effets discrets de ses propres actions). |
| `room_joined` / `join_error` / `action_error` | Confirmations et erreurs. |
| `chat_message` | Message du salon commun. |
| `message_prive` / `chat_prive_cree` | Notifications d'un Canal Clandestin. |
| `audience_debute` / `audience_notification` / `message_audience` / `audience_terminée` | Cycle de vie d'une Audience Royale. |
| `timer_update` / `timer_finished` | Chronomètre de la phase de discussion. |
| `crisis_drawn` / `crisis_attente` | Carte de crise tirée / en attente. |

> ⚠️ `state_update` **n'est pas un broadcast unique** : le serveur envoie à chaque joueur connecté sa propre version de l'état (`public_state(gs, viewer_uid=...)`), pour que les chats privés d'un joueur ne fuient jamais vers les autres. Toute nouvelle donnée sensible ajoutée à `GameState` doit être explicitement filtrée dans `public_state()` avant diffusion — ne jamais supposer qu'un seul `socketio.emit(..., room=gs.room_code)` est sans risque.

---

## Persistance des données

- **SQLite** par défaut (`game.db`), **PostgreSQL** automatiquement si `DATABASE_URL` est définie — même schéma (`games(room_code TEXT PRIMARY KEY, state_json TEXT, updated_at)`), sérialisation JSON complète de `GameState.to_dict()`.
- Un cache mémoire (`_rooms`) évite de retaper la base à chaque action ; chaque mutation d'état est réécrite en base via `persist()`.
- Un salon rechargé après redémarrage du serveur est restauré depuis la base via `GameState.from_dict()`.

---

## Notes techniques et pièges connus

- **Toujours utiliser `socketio.emit(...)`** (jamais `emit(...)` nu) dans tout code exécuté **hors du contexte d'une requête SocketIO active** — typiquement les callbacks de `threading.Timer` (ex. `_fin_audience`) ou la boucle `timer_loop`. `emit()` nu suppose un contexte de requête Flask et échoue silencieusement (`RuntimeError`) dans un thread d'arrière-plan.
- Après toute mutation de `GameState` déclenchée en dehors d'un handler SocketIO standard (timers, threads), pensez à appeler `broadcast_state(gs)` vous-même : rien d'autre ne le fera à votre place.
- `public_state(gs, viewer_uid)` est le seul point de filtrage des données sensibles (chats privés notamment). Toute nouvelle fonctionnalité impliquant des données visibles par un sous-ensemble seulement des joueurs doit passer par ce filtre plutôt que par un broadcast room-wide.
- Les identifiants de rôle (`role_id`, ex. `"finances"`) et les identifiants de joueur (`player_uid`, UUID) ne doivent jamais être confondus dans les payloads socket — plusieurs handlers acceptent l'un ou l'autre selon le contexte (vérifier la signature de la méthode `GameState` correspondante avant d'ajouter un nouvel appel).
