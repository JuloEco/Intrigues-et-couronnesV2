"""
Intrigues & Couronne — Jeu politique en ligne
Version complète avec tous les pouvoirs spéciaux, audiences royales et chats privés.
"""

from __future__ import annotations

import os
import json
import random
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
import time

from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, join_room, emit
from gevent import monkey
monkey.patch_all()

# ============================================================================
# CONFIGURATION
# ============================================================================
NB_CYCLES = 8
TIMER_DISCUSSION_SECONDS = 55
STABILITE_MAX = 100
STABILITE_INITIALE = 70
OR_PUBLIC_INITIAL = 100
MIN_TOTAL_PLAYERS = 3
MAX_TOTAL_PLAYERS = 6
REVOCATIONS_ROYALES_INITIALES = 2
DUREE_AUDIENCE = 45  # secondes

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="gevent")

# ============================================================================
# BDD (SQLite / PostgreSQL)
# ============================================================================
_USE_POSTGRES = bool(os.environ.get("DATABASE_URL"))
_db_lock = threading.Lock()

if _USE_POSTGRES:
    import psycopg2

SQLITE_PATH = os.environ.get("SQLITE_PATH", os.path.join(os.path.dirname(__file__), "game.db"))


def get_connection():
    if _USE_POSTGRES:
        return psycopg2.connect(os.environ["DATABASE_URL"], sslmode=os.environ.get("PGSSLMODE", "require"))
    conn = sqlite3.connect(SQLITE_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            ts_type = "TIMESTAMP" if _USE_POSTGRES else "TEXT"
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS games (
                    room_code TEXT PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    updated_at {ts_type} NOT NULL
                )
            """)
            conn.commit()
        finally:
            conn.close()


def save_game(room_code: str, state_dict: dict) -> None:
    payload = json.dumps(state_dict, ensure_ascii=False)
    now = datetime.now(timezone.utc)
    with _db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            if _USE_POSTGRES:
                cur.execute("""
                    INSERT INTO games (room_code, state_json, updated_at)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (room_code) DO UPDATE
                    SET state_json = EXCLUDED.state_json, updated_at = EXCLUDED.updated_at
                """, (room_code, payload, now))
            else:
                cur.execute("""
                    INSERT INTO games (room_code, state_json, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(room_code) DO UPDATE
                    SET state_json = excluded.state_json, updated_at = excluded.updated_at
                """, (room_code, payload, now.isoformat()))
            conn.commit()
        finally:
            conn.close()


def load_game(room_code: str) -> dict | None:
    with _db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            placeholder = "%s" if _USE_POSTGRES else "?"
            cur.execute(f"SELECT state_json FROM games WHERE room_code = {placeholder}", (room_code,))
            row = cur.fetchone()
            return json.loads(row[0]) if row else None
        finally:
            conn.close()


def room_exists_in_db(room_code: str) -> bool:
    with _db_lock:
        conn = get_connection()
        try:
            cur = conn.cursor()
            placeholder = "%s" if _USE_POSTGRES else "?"
            cur.execute(f"SELECT 1 FROM games WHERE room_code = {placeholder}", (room_code,))
            return cur.fetchone() is not None
        finally:
            conn.close()


# ============================================================================
# DÉFINITIONS DES RÔLES
# ============================================================================
@dataclass(frozen=True)
class RoleDef:
    id: str
    name: str
    icon: str
    color: str
    flux: str
    voie_serviteur: str
    voie_fourbe: str
    niveau_desc: list[str]
    statut_ministere: int
    titre_courtisan: str = ""
    pouvoir_nom: str = ""
    pouvoir_desc: str = ""


ROLES: dict[str, RoleDef] = {
    "interieur": RoleDef(
        id="interieur",
        name="Ministre de l'Intérieur",
        icon="📜",
        color="#6b8fd4",
        flux="Gère la sécurité publique et la stabilité.",
        voie_serviteur="Protéger le Royaume des complots...",
        voie_fourbe="Manipuler l'information...",
        niveau_desc=[
            "Fermé : +20 Or perso, peut falsifier le rapport",
            "Restreint : +10 Or perso",
            "Normal : +3 Influence, +1 Stabilité",
            "Surchauffe : +5 Influence, -5 Stabilité"
        ],
        statut_ministere=2,
        titre_courtisan="Le Lieutenant Général de Police",
        pouvoir_nom="La Falsification de Rapport",
        pouvoir_desc="Modifie les chiffres des comptes-rendus de crise présentés au Roi."
    ),
    "finances": RoleDef(
        id="finances",
        name="Surintendant des Finances",
        icon="💰",
        color="#d4b76b",
        flux="Gère le trésor public et l'économie du Royaume.",
        voie_serviteur="Faire fructifier les caisses...",
        voie_fourbe="Corrompre les institutions...",
        niveau_desc=[
            "Fermé : +30 Or perso",
            "Restreint : +15 Or perso",
            "Normal : +5 Or public",
            "Surchauffe : +15 Or public, -5 Stabilité"
        ],
        statut_ministere=3,
        titre_courtisan="Le Surintendant",
        pouvoir_nom="Le Pot-de-Vin Institutionnel",
        pouvoir_desc="Peut injecter son Or Personnel dans les Décrets pour acheter les votes des autres ministres à leur insu."
    ),
    "aumonier": RoleDef(
        id="aumonier",
        name="Grand Aumônier",
        icon="⛪",
        color="#a98fd4",
        flux="Garant de la foi, de la morale et de la légitimité divine.",
        voie_serviteur="Bénir les décrets et calmer les esprits...",
        voie_fourbe="Manipuler les consciences...",
        niveau_desc=[
            "Fermé : +20 Or perso, -5 Stabilité",
            "Restreint : +10 Or perso",
            "Normal : +3 Stabilité",
            "Surchauffe : +8 Stabilité, -10 Or public"
        ],
        statut_ministere=2,
        titre_courtisan="Le Confesseur Royal",
        pouvoir_nom="Les Canaux Clandestins",
        pouvoir_desc="Peut créer, gérer et infiltrer des chats privés secrets entre les joueurs."
    ),
    "subsistances": RoleDef(
        id="subsistances",
        name="Grand Maître des Subsistances",
        icon="🌾",
        color="#7bbf6a",
        flux="Gère les récoltes, les marchés et la colère de la rue.",
        voie_serviteur="Nourrir le peuple et maintenir l'ordre...",
        voie_fourbe="Spéculer sur les pénuries...",
        niveau_desc=[
            "Fermé : +15 Or perso, -4 Stabilité",
            "Restreint : +8 Or perso",
            "Normal : +2 Or public, +1 Stabilité",
            "Surchauffe : +5 Or public, +3 Stabilité, -5 Or perso"
        ],
        statut_ministere=1,
        titre_courtisan="Le Grand Intendant des Vivres",
        pouvoir_nom="Le Veto Populaire",
        pouvoir_desc="Une fois par partie, peut annuler un Décret en cours au nom du peuple."
    ),
    "connetable": RoleDef(
        id="connetable",
        name="Connétable",
        icon="🛡️",
        color="#c46a6a",
        flux="Chef des armées et de la maréchaussée.",
        voie_serviteur="Protéger la Cour et faire respecter la loi...",
        voie_fourbe="Abuser de sa force...",
        niveau_desc=[
            "Fermé : +15 Or perso, -3 Stabilité",
            "Restreint : +8 Or perso",
            "Normal : +3 Stabilité",
            "Surchauffe : +6 Stabilité, -5 Or public"
        ],
        statut_ministere=1,
        titre_courtisan="Le Capitaine des Mousquetaires",
        pouvoir_nom="L'Arrestation Préventive",
        pouvoir_desc="Au début d'une phase de discussion, isole un ministre : il est banni de tous les chats privés."
    ),
}

ROLE_ORDER = ["interieur", "finances", "aumonier", "subsistances", "connetable"]

KING_ROLE = {
    "id": "roi",
    "name": "Le Roi",
    "icon": "👑",
    "color": "#f0c419",
    "titre_courtisan": "Le Roi-Soleil",
    "description": "Arbitre suprême de la partie. Peut convoquer des Audiences Royales Privées."
}

# ============================================================================
# DECK DE CARTES
# ============================================================================
@dataclass(frozen=True)
class CrisisCard:
    id: str
    titre: str
    texte: str
    stabilite_delta: int
    or_public_delta: int
    type: str = "diplomatique"


CRISIS_DECK: list[CrisisCard] = [
    CrisisCard("c01", "Récolte exceptionnelle", "Les greniers débordent.", 4, 15, "agricole"),
    CrisisCard("c02", "Épidémie de fièvre", "La maladie se propage.", -10, -12, "sanitaire"),
    CrisisCard("c03", "Banditisme", "Une caravane est attaquée.", -3, -18, "securite"),
    CrisisCard("c04", "Mariage princier", "La cour exulte.", 8, -10, "mondaine"),
    CrisisCard("c05", "Incendie aux entrepôts", "Les réserves brûlent.", -5, -20, "securite"),
    CrisisCard("c06", "Pèlerinage massif", "Des foules convergent.", 5, 5, "religieuse"),
    CrisisCard("c07", "Sécheresse", "Les puits s'assèchent.", -6, -8, "agricole"),
    CrisisCard("c08", "Gisement d'argent", "Une mine prometteuse.", 2, 25, "financiere"),
    CrisisCard("c09", "Émeute de la faim", "La foule réclame du pain.", -15, -5, "revolte"),
    CrisisCard("c10", "Ambassadeur étranger", "Cadeaux diplomatiques.", 3, -15, "diplomatique"),
    CrisisCard("c11", "Naufrage de la flotte", "Taxes perdues en mer.", -2, -22, "financiere"),
    CrisisCard("c12", "Foire commerciale", "Les marchands affluent.", 3, 18, "financiere"),
    CrisisCard("c13", "Rumeurs de complot", "Murmures de trahison.", -8, 0, "securite"),
    CrisisCard("c14", "Don du clergé", "Distribution de vivres.", 7, -6, "religieuse"),
    CrisisCard("c15", "Inondation", "Les champs sont submergés.", -7, -10, "agricole"),
    CrisisCard("c16", "Tournoi de chevalerie", "Le peuple acclame.", 6, -8, "mondaine"),
    CrisisCard("c17", "Corruption aux douanes", "Un scandale éclate.", -9, -14, "financiere"),
    CrisisCard("c18", "Alliance commerciale", "Un traité avantageux.", 4, 20, "diplomatique"),
    CrisisCard("c19", "Peste du bétail", "Les troupeaux dépérissent.", -8, -15, "agricole"),
    CrisisCard("c20", "Fête des moissons", "Célébration unanime.", 9, 6, "agricole"),
    CrisisCard("c21", "Désertion", "Des soldats impayés.", -6, -9, "militaire"),
    CrisisCard("c22", "Legs d'un noble", "Une fortune inattendue.", 1, 30, "financiere"),
    CrisisCard("c23", "Hiver précoce", "Le froid frappe tôt.", -5, -12, "agricole"),
    CrisisCard("c24", "Réconciliation", "Paix nobiliaire.", 6, 4, "diplomatique"),
    CrisisCard("c25", "Effondrement d'un pont", "Travaux d'urgence.", -4, -25, "securite"),
    CrisisCard("c26", "Bal masqué", "Versailles resplendit.", 9, -18, "mondaine"),
    CrisisCard("c27", "Favorite en disgrâce", "Murmures à la cour.", -7, 0, "mondaine"),
    CrisisCard("c28", "Incendie à l'Opéra", "Le théâtre royal brûle.", -6, -20, "securite"),
    CrisisCard("c29", "Pièce interdite", "Le Tartuffe scandalise.", -4, 3, "religieuse"),
    CrisisCard("c30", "Vol de vaisselle d'or", "Larcin au palais.", -5, -16, "securite"),
    CrisisCard("c31", "Duel interdit", "Gentilshommes s'affrontent.", -6, -2, "securite"),
    CrisisCard("c32", "Comète", "Présages funestes.", -5, -3, "religieuse"),
    CrisisCard("c33", "Ambassade du Siam", "Présents exotiques.", 7, -12, "diplomatique"),
    CrisisCard("c34", "Fronde en province", "Rancunes nobiliaires.", -9, -6, "revolte"),
    CrisisCard("c35", "Favorite influente", "Intrigues de boudoir.", -3, 10, "mondaine"),
    CrisisCard("c36", "Feu d'artifice", "Versailles s'embrase.", 6, -14, "mondaine"),
    CrisisCard("c37", "Chasse royale", "Battue fastueuse.", 4, -9, "mondaine"),
    CrisisCard("c38", "Financier véreux", "Justice royale.", 2, 22, "financiere"),
    CrisisCard("c39", "Sécheresse des canaux", "Fontaines arrêtées.", -6, -11, "agricole"),
]

CARD_BY_ID = {c.id: c for c in CRISIS_DECK}

def build_shuffled_deck(seed: int | None = None) -> list[str]:
    ids = [c.id for c in CRISIS_DECK]
    random.Random(seed).shuffle(ids)
    return ids

# ============================================================================
# TÂCHES, ENTREPRISES ET DOSSIERS
# ============================================================================

TASK_CATALOG: dict[str, list[dict]] = {
    "interieur": [
        {"id": "int1", "titre": "Apaiser l'Émeute", "role_aide": "subsistances", "cout_aide": 10,
         "types": ["revolte"], "gain": {"stabilite": 10}, "perte": {"stabilite": -8, "or_public": -5},
         "description": "Il faut des vivres pour calmer les esprits."},
        {"id": "int2", "titre": "Enquêter sur un Complot", "role_aide": None, "cout_aide": 0,
         "types": ["securite"], "gain": {"stabilite": 5}, "perte": {"stabilite": -6},
         "description": "Démanteler un complot contre le Royaume."},
        {"id": "int3", "titre": "Surveiller les Frontières", "role_aide": "connetable", "cout_aide": 8,
         "types": ["militaire"], "gain": {"stabilite": 8}, "perte": {"stabilite": -5, "or_public": -3},
         "description": "Renforcer la surveillance aux frontières."},
        {"id": "int4", "titre": "Censurer les Rumeurs", "role_aide": None, "cout_aide": 0,
         "types": ["mondaine"], "gain": {"stabilite": 3}, "perte": {"stabilite": -4},
         "description": "Éviter la propagation de fausses informations."},
        {"id": "int5", "titre": "Arrêter un Espion", "role_aide": None, "cout_aide": 0,
         "types": ["securite"], "gain": {"stabilite": 6}, "perte": {"stabilite": -7},
         "description": "Dénicher un espion à la cour."},
    ],
    "finances": [
        {"id": "fin1", "titre": "Renflouer les Caisses", "role_aide": None, "cout_aide": 0,
         "types": ["financiere"], "gain": {"or_public": 15}, "perte": {"or_public": -10},
         "description": "Colmater la brèche financière."},
        {"id": "fin2", "titre": "Collecter les Impôts", "role_aide": None, "cout_aide": 0,
         "types": ["financiere"], "gain": {"or_public": 20}, "perte": {"or_public": -12},
         "description": "Recueillir les taxes impayées."},
        {"id": "fin3", "titre": "Négocier un Emprunt", "role_aide": None, "cout_aide": 0,
         "types": ["financiere"], "gain": {"or_public": 25}, "perte": {"or_public": -15, "stabilite": -3},
         "description": "Obtenir un prêt auprès des banquiers."},
        {"id": "fin4", "titre": "Lutter contre la Fraude", "role_aide": "interieur", "cout_aide": 12,
         "types": ["financiere"], "gain": {"or_public": 18}, "perte": {"or_public": -10},
         "description": "Traquer les fraudeurs du trésor public."},
        {"id": "fin5", "titre": "Subventionner les Marchés", "role_aide": "subsistances", "cout_aide": 15,
         "types": ["agricole"], "gain": {"or_public": 10, "stabilite": 2}, "perte": {"or_public": -8},
         "description": "Soutenir les marchands locaux."},
    ],
    "aumonier": [
        {"id": "aum1", "titre": "Bénir les Récoltes", "role_aide": None, "cout_aide": 0,
         "types": ["agricole"], "gain": {"stabilite": 4}, "perte": {"stabilite": -1},
         "description": "Une cérémonie solennelle rassurerait les paysans."},
        {"id": "aum2", "titre": "Prêcher la Paix", "role_aide": None, "cout_aide": 0,
         "types": ["revolte"], "gain": {"stabilite": 6}, "perte": {"stabilite": -4},
         "description": "Calmer les tensions par la parole divine."},
        {"id": "aum3", "titre": "Organiser un Pèlerinage", "role_aide": None, "cout_aide": 0,
         "types": ["religieuse"], "gain": {"stabilite": 5}, "perte": {"stabilite": -3},
         "description": "Un pèlerinage pour apaiser les esprits."},
        {"id": "aum4", "titre": "Exorciser les Démons", "role_aide": None, "cout_aide": 0,
         "types": ["religieuse"], "gain": {"stabilite": 7}, "perte": {"stabilite": -5},
         "description": "Purifier les lieux maudits."},
        {"id": "aum5", "titre": "Bénir un Mariage Royal", "role_aide": None, "cout_aide": 0,
         "types": ["mondaine"], "gain": {"stabilite": 8}, "perte": {"stabilite": -6},
         "description": "Un mariage béni par l'Église."},
    ],
    "subsistances": [
        {"id": "sub1", "titre": "Ouvrir les Greniers", "role_aide": None, "cout_aide": 0,
         "types": ["revolte", "agricole"], "gain": {"stabilite": 7}, "perte": {"or_public": -8},
         "description": "Distribuer le grain calmerait la foule."},
        {"id": "sub2", "titre": "Distribuer des Vivres", "role_aide": None, "cout_aide": 0,
         "types": ["agricole"], "gain": {"stabilite": 5}, "perte": {"or_public": -6},
         "description": "Nourrir les plus pauvres."},
        {"id": "sub3", "titre": "Acheter du Grain", "role_aide": "finances", "cout_aide": 10,
         "types": ["agricole"], "gain": {"or_public": 12}, "perte": {"or_public": -8},
         "description": "Approvisionner les réserves."},
        {"id": "sub4", "titre": "Organiser un Banquet", "role_aide": None, "cout_aide": 0,
         "types": ["mondaine"], "gain": {"stabilite": 4}, "perte": {"or_public": -5},
         "description": "Un festin pour célébrer l'abondance."},
        {"id": "sub5", "titre": "Stocker les Récoltes", "role_aide": None, "cout_aide": 0,
         "types": ["agricole"], "gain": {"stabilite": 6}, "perte": {"or_public": -4},
         "description": "Préparer les réserves pour l'hiver."},
    ],
    "connetable": [
        {"id": "con1", "titre": "Réprimer l'Émeute", "role_aide": None, "cout_aide": 0,
         "types": ["revolte"], "gain": {"stabilite": 8}, "perte": {"stabilite": -10},
         "description": "La garde doit intervenir."},
        {"id": "con2", "titre": "Protéger le Roi", "role_aide": None, "cout_aide": 0,
         "types": ["securite"], "gain": {"stabilite": 6}, "perte": {"stabilite": -8},
         "description": "Assurer la sécurité du souverain."},
        {"id": "con3", "titre": "Arrêter les Bandits", "role_aide": None, "cout_aide": 0,
         "types": ["securite"], "gain": {"stabilite": 7}, "perte": {"stabilite": -9},
         "description": "Nettoyer les routes des brigands."},
        {"id": "con4", "titre": "Escorter une Caravane", "role_aide": "finances", "cout_aide": 8,
         "types": ["financiere"], "gain": {"or_public": 10}, "perte": {"or_public": -5},
         "description": "Protéger les marchandises en transit."},
        {"id": "con5", "titre": "Renforcer les Gardes", "role_aide": None, "cout_aide": 0,
         "types": ["securite"], "gain": {"stabilite": 5}, "perte": {"stabilite": -6},
         "description": "Recruter de nouveaux soldats."},
    ],
}


ENTERPRISE_CATALOG: list[dict] = [
    {
        "id": "ent1",
        "nom": "Faire la Guerre",
        "roles_requis": ["connetable", "finances"],
        "cout_investissement": 10,
        "gain": {"or_public": 30, "stabilite": 5, "influence_par_ministre": 3},
        "perte": {"stabilite": -12, "or_public": -15},
        "description": "Le Connétable lève les troupes, le Surintendant finance."
    },
    {
        "id": "ent2",
        "nom": "Grand Traité de Paix",
        "roles_requis": ["aumonier", "interieur"],
        "cout_investissement": 8,
        "gain": {"stabilite": 14, "influence_par_ministre": 2},
        "perte": {"stabilite": -6},
        "description": "L'Aumônier bénit l'accord, l'Intérieur en garantit le respect."
    },
    {
        "id": "ent3",
        "nom": "Alliance Dynastique",
        "roles_requis": ["interieur", "finances"],
        "cout_investissement": 12,
        "gain": {"or_public": 25, "stabilite": 8, "influence_par_ministre": 4},
        "perte": {"stabilite": -10, "or_public": -20},
        "description": "Un mariage royal scelle une alliance lucrative."
    },
    {
        "id": "ent4",
        "nom": "Grand Chantier Royal",
        "roles_requis": ["finances", "subsistances"],
        "cout_investissement": 15,
        "gain": {"or_public": 40, "stabilite": 5, "influence_par_ministre": 2},
        "perte": {"stabilite": -15, "or_public": -30},
        "description": "Construction d'un palais ou d'une cathédrale."
    },
    {
        "id": "ent5",
        "nom": "Expédition Coloniale",
        "roles_requis": ["connetable", "subsistances"],
        "cout_investissement": 14,
        "gain": {"or_public": 35, "stabilite": 7, "influence_par_ministre": 3},
        "perte": {"stabilite": -12, "or_public": -25},
        "description": "Conquête de nouvelles terres pour le Royaume."
    },
    {
        "id": "ent6",
        "nom": "Fête des Lumières",
        "roles_requis": ["aumonier", "subsistances"],
        "cout_investissement": 10,
        "gain": {"or_public": 20, "stabilite": 10, "influence_par_ministre": 2},
        "perte": {"stabilite": -8, "or_public": -10},
        "description": "Célébration fastueuse pour le peuple."
    },
    {
        "id": "ent7",
        "nom": "Réforme Fiscale",
        "roles_requis": ["finances"],
        "cout_investissement": 8,
        "gain": {"or_public": 25, "stabilite": 3, "influence_par_ministre": 1},
        "perte": {"stabilite": -5, "or_public": -15},
        "description": "Optimisation des impôts pour remplir les caisses."
    },
    {
        "id": "ent8",
        "nom": "Chasse aux Sorcières",
        "roles_requis": ["aumonier", "connetable"],
        "cout_investissement": 9,
        "gain": {"stabilite": 12, "influence_par_ministre": 2},
        "perte": {"stabilite": -8},
        "description": "Purge des hérétiques pour rassurer la population."
    },
    {
        "id": "ent9",
        "nom": "Festival de la Moisson",
        "roles_requis": ["subsistances"],
        "cout_investissement": 7,
        "gain": {"or_public": 15, "stabilite": 6, "influence_par_ministre": 1},
        "perte": {"stabilite": -5, "or_public": -10},
        "description": "Célébration des récoltes pour booster le moral."
    },
    {
        "id": "ent10",
        "nom": "Diplomatie avec le Vatican",
        "roles_requis": ["aumonier", "interieur"],
        "cout_investissement": 11,
        "gain": {"or_public": 20, "stabilite": 9, "influence_par_ministre": 3},
        "perte": {"stabilite": -10, "or_public": -15},
        "description": "Négociations pour obtenir des fonds et du soutien spirituel."
    },
]
ENTERPRISE_BY_ID = {e["id"]: e for e in ENTERPRISE_CATALOG}

DOSSIERS_CATALOG = [
    {
        "id": "dossier1",
        "nom": "Grande Muraille du Nord",
        "description": "Fortifier la frontière contre les barbares.",
        "ressources_requises": {"or_personnel": 60, "influence": 20},
        "duree_tours": 3,
        "penalite_echec": {"stabilite": -15, "or_public": -20},
        "recompense_reussite": {"stabilite": 20, "or_public": 30, "influence_par_ministre": 5},
    },
    {
        "id": "dossier2",
        "nom": "Université Royale",
        "description": "Fonder une académie des sciences.",
        "ressources_requises": {"or_personnel": 40, "influence": 25},
        "duree_tours": 2,
        "penalite_echec": {"stabilite": -5, "or_public": -10},
        "recompense_reussite": {"or_public": 15, "influence_par_ministre": 8},
    },
    {
        "id": "dossier3",
        "nom": "Expédition en Nouvelle-France",
        "description": "Coloniser de nouveaux territoires.",
        "ressources_requises": {"or_personnel": 50, "stabilite": 10},
        "duree_tours": 4,
        "penalite_echec": {"stabilite": -10, "or_public": -15},
        "recompense_reussite": {"or_public": 40, "stabilite": 15, "influence_par_ministre": 3},
    }
]


# ============================================================================
# CLASSES DE DONNÉES POUR LES NOUVELLES FONCTIONNALITÉS
# ============================================================================

@dataclass
class ChatPrive:
    id: str
    nom: str
    createur_uid: str
    membres: list[str]
    messages: list[dict] = field(default_factory=list)
    actif: bool = True


@dataclass
class AudienceRoyale:
    roi_uid: str
    cible_uid: str
    debut: float
    duree: int = DUREE_AUDIENCE
    actif: bool = True


# ============================================================================
# MOTEUR DE JEU (GameState)
# ============================================================================

class Phase(str, Enum):
    LOBBY = "lobby"
    DISCUSSION = "discussion"
    DECISION = "decision"
    RAPPORT = "rapport"
    RESOLUTION = "resolution"
    TERMINEE_VICTOIRE = "terminee_victoire"
    TERMINEE_RUINE = "terminee_ruine"
    TRIBUNAL = "tribunal"


@dataclass
class PlayerState:
    sid: str
    player_uid: str
    pseudo: str
    role_ids: list[str] = field(default_factory=list)
    is_king: bool = False
    or_personnel: int = 0
    influence: int = 0
    prestige_cour: int = 0
    connected: bool = True
    resigned_roles: list[str] = field(default_factory=list)
    personal_log: list[str] = field(default_factory=list)

    def to_public_dict(self) -> dict:
        d = asdict(self)
        d.pop("personal_log", None)
        return d


@dataclass
class GameState:
    room_code: str
    phase: Phase = Phase.LOBBY
    cycle: int = 1
    stabilite: int = STABILITE_INITIALE
    or_public: int = OR_PUBLIC_INITIAL
    active_role_ids: list[str] = field(default_factory=list)
    players: dict[str, PlayerState] = field(default_factory=dict)
    deck: list[str] = field(default_factory=list)
    deck_position: int = 0
    current_card_id: str | None = None
    current_card_revealed: dict | None = None
    decisions: dict[str, int] = field(default_factory=dict)
    interieur_falsifie: bool = False
    log: list[str] = field(default_factory=list)
    winner_uid: str | None = None
    final_scores: dict | None = None
    host_uid: str | None = None

    timer_seconds: int = 0
    timer_running: bool = False

    revocations_restantes: int = REVOCATIONS_ROYALES_INITIALES
    pouvoirs_utilises: list[str] = field(default_factory=list)
    entreprises_utilisees: list[str] = field(default_factory=list)
    entreprise_active: dict | None = None
    dettes_a_venir: dict[str, int] = field(default_factory=dict)
    tache_active: dict | None = None
    dernier_pouvoir_info: dict | None = None

    dossiers_actifs: list[dict] = field(default_factory=list)
    dossiers_termines: list[str] = field(default_factory=list)

    decrets_actifs: list[dict] = field(default_factory=list)
    decrets_historique: list[str] = field(default_factory=list)

    tribunal_actif: dict | None = None
    tribunal_historique: list[str] = field(default_factory=list)

    chats_prives: list[ChatPrive] = field(default_factory=list)
    audience_active: AudienceRoyale | None = None
    securite_active: dict | None = None

    # ========== Helpers ==========

    def add_player(self, player_uid: str, sid: str, pseudo: str) -> PlayerState:
        if player_uid in self.players:
            p = self.players[player_uid]
            p.sid = sid
            p.connected = True
            return p
        is_first = len(self.players) == 0
        p = PlayerState(sid=sid, player_uid=player_uid, pseudo=pseudo, is_king=False)
        self.players[player_uid] = p
        if is_first:
            self.host_uid = player_uid
        return p

    def assign_roles(self, king_uid: str) -> None:
        total = len(self.players)
        n_ministres = min(5, total - 1)
        chosen_roles = random.sample(ROLE_ORDER, n_ministres)
        self.active_role_ids = chosen_roles

        non_king_uids = [uid for uid in self.players if uid != king_uid]
        random.shuffle(non_king_uids)

        for uid in self.players:
            self.players[uid].is_king = (uid == king_uid)
            self.players[uid].role_ids = []

        for i, uid in enumerate(non_king_uids):
            if i < len(chosen_roles):
                self.players[uid].role_ids = [chosen_roles[i]]

        self.deck = build_shuffled_deck()
        self.deck_position = 0
        self.phase = Phase.DISCUSSION
        self.cycle = 1
        self.revocations_restantes = REVOCATIONS_ROYALES_INITIALES
        self.pouvoirs_utilises = []
        self.entreprises_utilisees = []
        self.entreprise_active = None
        self.dettes_a_venir = {}
        self.tache_active = None
        self.dernier_pouvoir_info = None
        self.dossiers_actifs = []
        self.dossiers_termines = []
        self.decrets_actifs = []
        self.tribunal_actif = None
        self.chats_prives = []
        self.audience_active = None
        self.securite_active = None

        self.log.append(
            f"Rôles distribués : {', '.join(ROLES[r].name for r in chosen_roles)}. "
            f"{self.players[king_uid].pseudo} est désigné Roi."
        )
        self.start_timer()

    def start_timer(self):
        self.timer_seconds = TIMER_DISCUSSION_SECONDS
        self.timer_running = True

    def tick_timer(self) -> bool:
        if self.timer_running and self.phase == Phase.DISCUSSION:
            self.timer_seconds -= 1
            if self.timer_seconds <= 0:
                self.timer_running = False
                if self.decrets_actifs:
                    self._resoudre_decret(self.decrets_actifs[0])
                if self.tribunal_actif:
                    self.tribunal_actif = None
                    self.log.append("⚖️ Le tribunal est dissous par l'expiration du temps.")
                self.start_decision_phase()
                return True
        return False

    def ministre_uid_for_role(self, role_id: str) -> str | None:
        for uid, p in self.players.items():
            if role_id in p.role_ids:
                return uid
        return None

    def uids_libres(self) -> list[str]:
        return [uid for uid, p in self.players.items() if not p.role_ids and not p.is_king]

    def king_uid(self) -> str | None:
        for uid, p in self.players.items():
            if p.is_king:
                return uid
        return None

    # ========== Cycle flow ==========

    def start_decision_phase(self) -> None:
        self.timer_running = False
        self.decisions = {}
        self.interieur_falsifie = False
        self.current_card_id = None
        self.current_card_revealed = None
        self.entreprise_active = None
        self.tache_active = None
        self.dernier_pouvoir_info = None
        self.phase = Phase.DECISION
        self.log.append(f"--- Cycle {self.cycle}/{NB_CYCLES} : les ministres délibèrent ---")

    def start_discussion_phase(self) -> None:
        """Démarre une nouvelle phase de discussion pour le cycle suivant."""
        self.phase = Phase.DISCUSSION
        self.decisions = {}
        self.interieur_falsifie = False
        self.current_card_id = None
        self.current_card_revealed = None
        self.entreprise_active = None
        self.tache_active = None
        self.dernier_pouvoir_info = None
        self.start_timer()
        self.log.append(f"--- Début du cycle {self.cycle}/{NB_CYCLES} : discussion ---")

    def roles_presents(self) -> set[str]:
        return {r for p in self.players.values() for r in p.role_ids}

    def submit_decision(self, role_id: str, niveau: int) -> None:
        assert 0 <= niveau <= 3
        if self.securite_active and self.securite_active.get("cible_uid") == self.ministre_uid_for_role(role_id):
            niveau = 0
        self.decisions[role_id] = niveau

    def all_decisions_in(self) -> bool:
        return all(r in self.decisions for r in self.active_role_ids)

    def draw_crisis_card(self) -> dict | None:
        if self.deck_position >= len(self.deck):
            self.deck = build_shuffled_deck()
            self.deck_position = 0
        card_id = self.deck[self.deck_position]
        self.deck_position += 1
        self.current_card_id = card_id
        card = CARD_BY_ID[card_id]
        self.current_card_revealed = {
            "titre": card.titre,
            "texte": card.texte,
            "stabilite_delta": card.stabilite_delta,
            "or_public_delta": card.or_public_delta,
        }
        self.phase = Phase.RAPPORT
        self._roll_tache(card)
        return self.current_card_revealed

    def _roll_tache(self, card: "CrisisCard") -> None:
        presents = self.roles_presents()
        candidates = []
        for role_id, taches in TASK_CATALOG.items():
            if role_id not in presents:
                continue
            for t in taches:
                if card.type not in t["types"]:
                    continue
                if t["role_aide"] and t["role_aide"] not in presents:
                    continue
                candidates.append((role_id, t))
        if not candidates or random.random() > 0.7:
            self.tache_active = None
            return
        role_id, t = random.choice(candidates)
        self.tache_active = {
            "tache_id": t["id"],
            "role_id": role_id,
            "titre": t["titre"],
            "description": t["description"],
            "role_aide": t["role_aide"],
            "cout_aide": t["cout_aide"],
            "gain": t["gain"],
            "perte": t["perte"],
            "aide_obtenue": t["role_aide"] is None,
            "accomplie": False,
        }
        aide_txt = f" (nécessite l'aide de {ROLES[t['role_aide']].name})" if t["role_aide"] else ""
        self.log.append(f"❗ Mission Urgente pour {ROLES[role_id].name} : « {t['titre']} »{aide_txt}")

    def falsify_report(self, stabilite_annoncee: int, or_annonce: int) -> None:
        if self.current_card_revealed is None:
            return
        self.interieur_falsifie = True
        self.current_card_revealed["stabilite_delta"] = stabilite_annoncee
        self.current_card_revealed["or_public_delta"] = or_annonce

    def generer_options_falsification(self, card: dict) -> list[dict]:
        options = []
        
        options.append({
            "id": "fidele",
            "label": "📜 Transmettre fidèlement",
            "stabilite_delta": card["stabilite_delta"],
            "or_public_delta": card["or_public_delta"],
            "description": f"Effets réels : Stabilité {card['stabilite_delta']:+d}, Caisses {card['or_public_delta']:+d}"
        })
        
        options.append({
            "id": "minimiser",
            "label": "⬆️ Minimiser la crise",
            "stabilite_delta": min(0, card["stabilite_delta"] + random.randint(3, 8)),
            "or_public_delta": min(0, card["or_public_delta"] + random.randint(3, 10)),
            "description": "Faire passer la crise pour moins grave"
        })
        
        options.append({
            "id": "maximiser",
            "label": "⬇️ Maximiser la crise",
            "stabilite_delta": max(0, card["stabilite_delta"] - random.randint(3, 8)),
            "or_public_delta": max(0, card["or_public_delta"] - random.randint(3, 10)),
            "description": "Surenchérir la crise pour demander plus de budget"
        })
        
        if self.decisions.get("interieur", 0) == 0:
            options.append({
                "id": "inverser",
                "label": "🔄 Inverser les effets",
                "stabilite_delta": -card["stabilite_delta"],
                "or_public_delta": -card["or_public_delta"],
                "description": "Inverser complètement les effets de la crise"
            })
        
        return options

    # ========== Résolution ==========

    def _apply_deltas(self, p: PlayerState | None, deltas: dict) -> None:
        if "stabilite" in deltas:
            self.stabilite += deltas["stabilite"]
        if "or_public" in deltas:
            self.or_public += deltas["or_public"]
        if p is not None:
            if "or_personnel" in deltas:
                p.or_personnel = max(0, p.or_personnel + deltas["or_personnel"])
            if "influence" in deltas:
                p.influence = max(0, p.influence + deltas["influence"])
            if "prestige_cour" in deltas:
                p.prestige_cour = max(0, p.prestige_cour + deltas["prestige_cour"])

    def resolve_cycle(self) -> dict:
        summary = {"cycle": self.cycle, "effets": [], "carte": None, "ruine": False,
                   "demasque": None, "entreprise": None, "tache": None, "dossiers": []}

        self.securite_active = None

        real_card = CARD_BY_ID.get(self.current_card_id) if self.current_card_id else None
        if real_card:
            self.stabilite += real_card.stabilite_delta
            self.or_public += real_card.or_public_delta
            summary["carte"] = {
                "titre": real_card.titre,
                "annonce": self.current_card_revealed,
                "reel": {
                    "stabilite_delta": real_card.stabilite_delta,
                    "or_public_delta": real_card.or_public_delta,
                },
                "falsifie": self.interieur_falsifie,
            }

            if self.interieur_falsifie and self.current_card_revealed:
                ecart = (abs(self.current_card_revealed["stabilite_delta"] - real_card.stabilite_delta)
                         + abs(self.current_card_revealed["or_public_delta"] - real_card.or_public_delta))
                chance_demasque = min(0.85, 0.15 + ecart / 40)
                interieur_uid = self.ministre_uid_for_role("interieur")
                interieur_p = self.players.get(interieur_uid) if interieur_uid else None
                demasque = random.random() < chance_demasque
                if demasque:
                    self.stabilite -= 6
                    if interieur_p:
                        interieur_p.prestige_cour = max(0, interieur_p.prestige_cour - 3)
                        self.add_personal_log(interieur_uid, "Démasqué ! -6 Stabilité, -3 Prestige")
                    self.log.append("🕵️ Le Cabinet du Roi démasque le mensonge de l'Intérieur !")
                    summary["demasque"] = True
                else:
                    if interieur_p:
                        interieur_p.influence += 3
                        self.add_personal_log(interieur_uid, "Falsification réussie ! +3 Influence")
                    self.log.append("🎭 Le rapport falsifié passe inaperçu.")
                    summary["demasque"] = False

        for uid, montant in list(self.dettes_a_venir.items()):
            p = self.players.get(uid)
            if p:
                p.or_personnel = max(0, p.or_personnel - montant)
                self.add_personal_log(uid, f"Remboursement aux Génois : -{montant} Or.")
        self.dettes_a_venir = {}

        connetable_ouvert = self.decisions.get("connetable", 0) >= 2

        for role_id in self.active_role_ids:
            niveau = self.decisions.get(role_id, 0)
            uid = self.ministre_uid_for_role(role_id)
            if uid is None:
                continue
            p = self.players[uid]
            effet = self._apply_robinet_effect(role_id, niveau, p, connetable_ouvert)
            summary["effets"].append({
                "role_id": role_id,
                "role_name": ROLES[role_id].name,
                "pseudo": p.pseudo,
                "niveau": niveau,
                **effet,
            })

        if self.entreprise_active:
            ent = ENTERPRISE_BY_ID[self.entreprise_active["id"]]
            roles_requis = ent["roles_requis"]
            investissements = self.entreprise_active["investissements"]
            reussite = all(investissements.get(r) for r in roles_requis)
            if reussite:
                self._apply_deltas(None, {k: v for k, v in ent["gain"].items()
                                           if k in ("stabilite", "or_public")})
                for r in roles_requis:
                    uid = self.ministre_uid_for_role(r)
                    p = self.players.get(uid) if uid else None
                    if p:
                        if "influence_par_ministre" in ent["gain"]:
                            p.influence += ent["gain"]["influence_par_ministre"]
                            self.add_personal_log(uid, f"Entreprise réussie : +{ent['gain']['influence_par_ministre']} Influence")
                        if "or_personnel_par_ministre" in ent["gain"]:
                            p.or_personnel += ent["gain"]["or_personnel_par_ministre"]
                            self.add_personal_log(uid, f"Entreprise réussie : +{ent['gain']['or_personnel_par_ministre']} Or")
                self.log.append(f"🏆 « {ent['nom']} » est une réussite !")
            else:
                self._apply_deltas(None, ent["perte"])
                self.log.append(f"⚰️ « {ent['nom']} » échoue.")
            summary["entreprise"] = {"nom": ent["nom"], "reussite": reussite}
            self.entreprise_active = None

        if self.tache_active:
            t = self.tache_active
            uid = self.ministre_uid_for_role(t["role_id"])
            p = self.players.get(uid) if uid else None
            if t["accomplie"] and t["aide_obtenue"]:
                self._apply_deltas(p, t["gain"])
                self.log.append(f"✅ Mission accomplie : « {t['titre']} »")
                if p:
                    self.add_personal_log(uid, f"Mission accomplie !")
            else:
                self._apply_deltas(p, t["perte"])
                self.log.append(f"❌ Mission négligée : « {t['titre']} »")
                if p:
                    self.add_personal_log(uid, f"Mission échouée.")
            summary["tache"] = {"titre": t["titre"], "reussite": t["accomplie"] and t["aide_obtenue"]}
            self.tache_active = None

        for dossier in self.dossiers_actifs[:]:
            dossier["tours_restants"] -= 1
            if dossier["tours_restants"] <= 0:
                atteint = True
                for res, montant in dossier["ressources_requises"].items():
                    if dossier["ressource_courante"].get(res, 0) < montant:
                        atteint = False
                        break
                if atteint:
                    self._apply_deltas(None, dossier["recompense_reussite"])
                    for uid in dossier["contributions"]:
                        if uid in self.players:
                            self.players[uid].influence += 3
                            self.add_personal_log(uid, f"Dossier '{dossier['nom']}' réussi ! +3 Influence")
                    self.log.append(f"🎉 Dossier réussi : {dossier['nom']}")
                else:
                    self._apply_deltas(None, dossier["penalite_echec"])
                    self.log.append(f"💔 Dossier échoué : {dossier['nom']}")
                self.dossiers_actifs.remove(dossier)
                self.dossiers_termines.append(dossier["id"])
                summary["dossiers"].append({"nom": dossier["nom"], "reussite": atteint})

        self.stabilite = max(0, min(STABILITE_MAX, self.stabilite))

        if self.stabilite <= 0 or self.or_public < 0:
            self.phase = Phase.TERMINEE_RUINE
            summary["ruine"] = True
            self.log.append("💀 LE ROYAUME S'EFFONDRE !")
            for uid in self.players:
                self.add_personal_log(uid, "Le Royaume s'est effondré !")
            return summary

        self.log.append(f"Cycle {self.cycle} résolu — Stabilité: {self.stabilite}, Caisses: {self.or_public}")

        if self.cycle >= NB_CYCLES:
            self._compute_final_scores()
            self.phase = Phase.TERMINEE_VICTOIRE
        else:
            self.cycle += 1
            self.phase = Phase.RESOLUTION

        return summary

    def _apply_robinet_effect(self, role_id: str, niveau: int, p: PlayerState,
                                connetable_ouvert: bool) -> dict:
        rng = random.Random()
        d = {"stabilite_delta": 0, "or_public_delta": 0, "or_personnel_delta": 0, "influence_delta": 0}

        if role_id == "interieur":
            if niveau == 0:
                d["or_personnel_delta"] = 20
            elif niveau == 1:
                d["or_personnel_delta"] = 10
            elif niveau == 2:
                d["influence_delta"] = 3
                d["stabilite_delta"] = 1
            else:
                d["influence_delta"] = 5
                d["stabilite_delta"] = -5

        elif role_id == "finances":
            if niveau == 0:
                d["or_personnel_delta"] = 30
            elif niveau == 1:
                d["or_personnel_delta"] = 15
            elif niveau == 2:
                d["or_public_delta"] = 5
            else:
                d["or_public_delta"] = 15
                d["stabilite_delta"] = -5

        elif role_id == "aumonier":
            if niveau == 0:
                d["or_personnel_delta"] = 20
                d["stabilite_delta"] = -5
            elif niveau == 1:
                d["or_personnel_delta"] = 10
            elif niveau == 2:
                d["stabilite_delta"] = 3
            else:
                d["stabilite_delta"] = 8
                d["or_public_delta"] = -10

        elif role_id == "subsistances":
            if niveau == 0:
                d["or_personnel_delta"] = 15
                d["stabilite_delta"] = -4
            elif niveau == 1:
                d["or_personnel_delta"] = 8
            elif niveau == 2:
                d["or_public_delta"] = 2
                d["stabilite_delta"] = 1
            else:
                d["or_public_delta"] = 5
                d["stabilite_delta"] = 3
                d["or_personnel_delta"] = -5

        elif role_id == "connetable":
            if niveau == 0:
                d["or_personnel_delta"] = 15
                d["stabilite_delta"] = -3
            elif niveau == 1:
                d["or_personnel_delta"] = 8
            elif niveau == 2:
                d["stabilite_delta"] = 3
            else:
                d["stabilite_delta"] = 6
                d["or_public_delta"] = -5

        if role_id != "connetable" and niveau < 2 and rng.random() < 0.25:
            steal = min(p.or_personnel, 5)
            if steal > 0:
                d["or_personnel_delta"] -= steal
                d["vol_subi"] = steal
                self.add_personal_log(p.player_uid, f"Vol nocturne : -{steal} Or.")

        p.or_personnel = max(0, p.or_personnel + d["or_personnel_delta"])
        p.influence = max(0, p.influence + d["influence_delta"])
        self.stabilite += d["stabilite_delta"]
        self.or_public += d["or_public_delta"]

        return d

    def _compute_final_scores(self) -> None:
        scores = {}
        bonus_stabilite = self.stabilite // 2
        for uid, p in self.players.items():
            if not p.role_ids:
                continue
            statut = sum(ROLES[r].statut_ministere for r in p.role_ids)
            bonus_statut = statut * 10
            roles_noms = " + ".join(ROLES[r].name for r in p.role_ids)
            puissance = p.or_personnel + p.influence + p.prestige_cour + bonus_statut + bonus_stabilite
            scores[uid] = {
                "pseudo": p.pseudo,
                "role": roles_noms,
                "or_personnel": p.or_personnel,
                "influence": p.influence,
                "prestige_cour": p.prestige_cour,
                "statut_ministere": statut,
                "bonus_statut": bonus_statut,
                "stabilite_finale": self.stabilite,
                "bonus_stabilite": bonus_stabilite,
                "puissance": puissance,
            }
        self.final_scores = scores
        if scores:
            self.winner_uid = max(scores, key=lambda u: scores[u]["puissance"])

    # ========== Personal Logs ==========

    def add_personal_log(self, uid: str, message: str) -> None:
        p = self.players.get(uid)
        if p:
            p.personal_log.append(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")

    # ========== Dossiers Royaux ==========

    def lancer_dossier(self, dossier_id: str, proposer_uid: str) -> tuple[bool, str]:
        if proposer_uid != self.king_uid():
            return False, "Seul le Roi peut lancer un Dossier Royal."
        dossier_def = next((d for d in DOSSIERS_CATALOG if d["id"] == dossier_id), None)
        if not dossier_def:
            return False, "Dossier inconnu."
        if dossier_id in self.dossiers_termines:
            return False, "Ce dossier a déjà été terminé."
        if any(d["id"] == dossier_id for d in self.dossiers_actifs):
            return False, "Ce dossier est déjà en cours."
        new_dossier = {
            "id": dossier_def["id"],
            "nom": dossier_def["nom"],
            "description": dossier_def["description"],
            "ressources_requises": dossier_def["ressources_requises"],
            "ressource_courante": {res: 0 for res in dossier_def["ressources_requises"]},
            "duree_tours": dossier_def["duree_tours"],
            "tours_restants": dossier_def["duree_tours"],
            "contributions": {},
            "recompense_reussite": dossier_def["recompense_reussite"],
            "penalite_echec": dossier_def["penalite_echec"],
        }
        self.dossiers_actifs.append(new_dossier)
        self.log.append(f"📜 Le Roi lance le Dossier Royal : {dossier_def['nom']}")
        return True, "ok"

    def contribuer_dossier(self, dossier_id: str, ressource: str, montant: int, uid: str) -> tuple[bool, str]:
        dossier = next((d for d in self.dossiers_actifs if d["id"] == dossier_id), None)
        if not dossier:
            return False, "Dossier introuvable."
        if ressource not in dossier["ressources_requises"]:
            return False, "Ressource non requise."
        p = self.players.get(uid)
        if not p:
            return False, "Joueur introuvable."
        
        requis = dossier["ressources_requises"][ressource]
        courant = dossier["ressource_courante"].get(ressource, 0)
        reste = requis - courant
        if montant > reste:
            return False, f"Seulement {reste} de {ressource} est encore requis."
        
        if ressource == "or_personnel":
            if p.or_personnel < montant:
                return False, "Pas assez d'Or personnel."
            p.or_personnel -= montant
        elif ressource == "influence":
            if p.influence < montant:
                return False, "Pas assez d'Influence."
            p.influence -= montant
        elif ressource == "stabilite":
            if self.stabilite < montant:
                return False, "Stabilité trop basse."
            self.stabilite -= montant
        else:
            return False, "Ressource invalide."

        dossier["ressource_courante"][ressource] = courant + montant
        if uid not in dossier["contributions"]:
            dossier["contributions"][uid] = {}
        dossier["contributions"][uid][ressource] = dossier["contributions"][uid].get(ressource, 0) + montant

        self.add_personal_log(uid, f"Contribution au dossier '{dossier['nom']}' : +{montant} {ressource}.")
        self.log.append(f"⚙️ {p.pseudo} contribue {montant} {ressource} au dossier {dossier['nom']}.")
        return True, "ok"

    # ========== Décrets ==========

    def proposer_decret(self, proposer_uid: str, description: str, public_effects: dict,
                        secret_target_uid: str | None = None, secret_effects: dict | None = None) -> tuple[bool, str]:
        if self.phase not in (Phase.DISCUSSION, Phase.DECISION):
            return False, "Impossible de proposer un décret maintenant."
        if self.decrets_actifs:
            return False, "Un décret est déjà en cours."
        decret = {
            "id": str(uuid.uuid4()),
            "proposer_uid": proposer_uid,
            "description": description,
            "public_effects": public_effects,
            "secret_effects": secret_effects,
            "secret_target_uid": secret_target_uid,
            "votes": {},
            "bribes": {},
        }
        self.decrets_actifs.append(decret)
        self.log.append(f"📜 Un décret est proposé par {self.players[proposer_uid].pseudo} : {description}")
        return True, "ok"

    def voter_decret(self, uid: str, vote_oui: bool) -> tuple[bool, str]:
        if not self.decrets_actifs:
            return False, "Aucun décret en cours."
        decret = self.decrets_actifs[0]
        if uid in decret["votes"]:
            return False, "Vous avez déjà voté."
        decret["votes"][uid] = vote_oui
        if len(decret["votes"]) >= len(self.players):
            self._resoudre_decret(decret)
        return True, "ok"

    def _resoudre_decret(self, decret: dict) -> None:
        oui = sum(1 for v in decret["votes"].values() if v)
        total = len(decret["votes"])
        if oui > total / 2:
            self._apply_deltas(None, decret["public_effects"])
            if decret["secret_effects"] and decret["secret_target_uid"]:
                target = self.players.get(decret["secret_target_uid"])
                if target:
                    self._apply_deltas(target, decret["secret_effects"])
                    self.add_personal_log(decret["secret_target_uid"], f"Effet secret du décret : {decret['secret_effects']}")
            self.log.append(f"✅ Décret adopté : {decret['description']}")
            for uid, montant in decret["bribes"].items():
                if decret["votes"].get(uid) == True:
                    self.players[uid].or_personnel += montant
                    self.add_personal_log(uid, f"Pot-de-vin reçu : +{montant} Or.")
        else:
            self.log.append(f"❌ Décret rejeté : {decret['description']}")
        self.decrets_actifs.remove(decret)

    def offrir_bribe(self, decret_id: str, cible_uid: str, montant: int, uid: str) -> tuple[bool, str]:
        if not self.decrets_actifs:
            return False, "Aucun décret en cours."
        decret = self.decrets_actifs[0]
        if decret["id"] != decret_id:
            return False, "Décret incorrect."
        if uid not in self.players or cible_uid not in self.players:
            return False, "Joueur invalide."
        if uid == cible_uid:
            return False, "Vous ne pouvez pas vous offrir un pot-de-vin à vous-même."
        if self.players[uid].or_personnel < montant:
            return False, "Pas assez d'Or."
        decret["bribes"][cible_uid] = decret["bribes"].get(cible_uid, 0) + montant
        self.players[uid].or_personnel -= montant
        self.add_personal_log(uid, f"Pot-de-vin offert : {montant} Or à {self.players[cible_uid].pseudo}.")
        self.add_personal_log(cible_uid, f"Pot-de-vin reçu pour voter OUI : {montant} Or.")
        self.log.append(f"💵 {self.players[uid].pseudo} offre un pot-de-vin de {montant} Or.")
        return True, "ok"

    # ========== Tribunal ==========

    def initier_tribunal(self, accusateur_uid: str, accuse_uid: str) -> tuple[bool, str]:
        if self.phase != Phase.DISCUSSION:
            return False, "Le tribunal ne peut être convoqué qu'en phase de discussion."
        if self.tribunal_actif:
            return False, "Un tribunal est déjà en cours."
        if accusateur_uid != self.king_uid():
            return False, "Seul le Roi peut convoquer un tribunal."
        if accuse_uid == self.king_uid():
            return False, "Le Roi ne peut pas être accusé."
        if accuse_uid not in self.players:
            return False, "Accusé introuvable."
        if self.stabilite < 10:
            return False, "La Stabilité est trop basse (coût 10)."
        self.stabilite -= 10
        self.timer_running = False
        self.tribunal_actif = {
            "accusateur_uid": accusateur_uid,
            "accuse_uid": accuse_uid,
            "phase": "vote",
            "votes": {},
            "sentence": "",
        }
        self.phase = Phase.TRIBUNAL
        self.log.append(f"⚖️ Le Roi convoque un tribunal contre {self.players[accuse_uid].pseudo} !")
        return True, "ok"

    def voter_tribunal(self, uid: str, coupable: bool) -> tuple[bool, str]:
        if not self.tribunal_actif:
            return False, "Aucun tribunal en cours."
        if self.tribunal_actif["phase"] != "vote":
            return False, "La phase de vote est terminée."
        if uid in self.tribunal_actif["votes"]:
            return False, "Vous avez déjà voté."
        self.tribunal_actif["votes"][uid] = coupable
        if len(self.tribunal_actif["votes"]) >= len(self.players):
            self.tribunal_actif["phase"] = "sentence"
            coupables = sum(1 for v in self.tribunal_actif["votes"].values() if v)
            if coupables > len(self.players) / 2:
                self.tribunal_actif["sentence"] = "coupable"
                self._appliquer_sentence_tribunal()
            else:
                self.tribunal_actif["sentence"] = "innocent"
                self.log.append(f"⚖️ {self.players[self.tribunal_actif['accuse_uid']].pseudo} est déclaré innocent !")
            self.phase = Phase.DISCUSSION
            self.timer_running = True
            self.tribunal_actif = None
        return True, "ok"

    def _appliquer_sentence_tribunal(self) -> None:
        accuse_uid = self.tribunal_actif["accuse_uid"]
        accuse = self.players.get(accuse_uid)
        if not accuse:
            return
        accuse.or_personnel = max(0, accuse.or_personnel - 20)
        accuse.influence = max(0, accuse.influence - 5)
        self.add_personal_log(accuse_uid, "Condamné ! -20 Or, -5 Influence.")
        if accuse.role_ids:
            role = random.choice(accuse.role_ids)
            accuse.role_ids.remove(role)
            self.log.append(f"⚖️ {accuse.pseudo} perd son poste de {ROLES[role].name} !")
            self.add_personal_log(accuse_uid, f"Vous perdez votre poste de {ROLES[role].name}.")

    # ========== NOUVEAUX POUVOIRS ==========

    def use_pot_de_vin_institutionnel(self, requester_uid: str, decret_id: str, cible_uid: str, montant: int) -> tuple[bool, str]:
        uid = self.ministre_uid_for_role("finances")
        if uid != requester_uid:
            return False, "Vous n'êtes pas le Surintendant des Finances."
        if "finances" in self.pouvoirs_utilises:
            return False, "Ce pouvoir a déjà été utilisé."
        if self.phase not in (Phase.DISCUSSION, Phase.DECISION):
            return False, "Ce pouvoir ne peut être utilisé qu'en phase de discussion ou décision."
        if not self.decrets_actifs:
            return False, "Aucun décret en cours."
        
        decret = self.decrets_actifs[0]
        if decret["id"] != decret_id:
            return False, "Décret incorrect."
        
        p = self.players[uid]
        if p.or_personnel < montant:
            return False, f"Pas assez d'Or personnel (besoin de {montant})."
        
        cible = self.players.get(cible_uid)
        if not cible:
            return False, "Cible introuvable."
        if cible.is_king:
            return False, "Vous ne pouvez pas corrompre le Roi."
        
        p.or_personnel -= montant
        self.add_personal_log(cible_uid, f"💵 Vous avez reçu un pot-de-vin secret de {montant} Or pour voter OUI au décret.")
        
        decret["bribes"][cible_uid] = decret["bribes"].get(cible_uid, 0) + montant
        
        self.pouvoirs_utilises.append("finances")
        self.log.append(f"💰 {p.pseudo} utilise un Pot-de-Vin Institutionnel sur {cible.pseudo}.")
        
        return True, "ok"

    def use_arrestation_preventive(self, requester_uid: str, cible_uid: str) -> tuple[bool, str]:
        uid = self.ministre_uid_for_role("connetable")
        if uid != requester_uid:
            return False, "Vous n'êtes pas le Connétable."
        if "connetable" in self.pouvoirs_utilises:
            return False, "Ce pouvoir a déjà été utilisé."
        if self.phase != Phase.DISCUSSION:
            return False, "Ce pouvoir ne peut être utilisé qu'en début de phase de discussion."
        if cible_uid not in self.players:
            return False, "Cible introuvable."
        if cible_uid == uid:
            return False, "Vous ne pouvez pas vous arrêter vous-même."
        if self.players[cible_uid].is_king:
            return False, "Vous ne pouvez pas arrêter le Roi."
        
        self.securite_active = {
            "connétable_uid": uid,
            "cible_uid": cible_uid,
            "duree": 30,
            "debut": time.time()
        }
        
        for chat in self.chats_prives[:]:
            if cible_uid in chat.membres:
                chat.membres.remove(cible_uid)
                if len(chat.membres) < 2:
                    self.chats_prives.remove(chat)
        
        self.pouvoirs_utilises.append("connetable")
        self.log.append(f"🛡️ {self.players[uid].pseudo} place {self.players[cible_uid].pseudo} en arrestation préventive !")
        self.add_personal_log(cible_uid, "🛡️ Vous êtes en arrestation préventive ! Vous êtes banni de tous les chats privés.")
        
        return True, "ok"

    def use_veto_populaire(self, requester_uid: str) -> tuple[bool, str]:
        uid = self.ministre_uid_for_role("subsistances")
        if uid != requester_uid:
            return False, "Vous n'êtes pas le Grand Maître des Subsistances."
        if "subsistances" in self.pouvoirs_utilises:
            return False, "Ce pouvoir a déjà été utilisé."
        if not self.decrets_actifs:
            return False, "Aucun décret en cours à annuler."
        
        decret = self.decrets_actifs.pop(0)
        self.decrets_historique.append(decret["id"])
        
        self.pouvoirs_utilises.append("subsistances")
        self.log.append(f"🌾 {self.players[uid].pseudo} utilise son Veto Populaire pour annuler le décret !")
        self.add_personal_log(uid, "Veto Populaire : vous avez annulé le décret au nom du peuple.")
        
        return True, "ok"

    # ============================================================================
    # GESTION DES CHATS PRIVÉS
    # ============================================================================

    def creer_chat_prive(self, createur_uid: str, nom: str, membres: list[str]) -> tuple[bool, str]:
        uid = self.ministre_uid_for_role("aumonier")
        if uid != createur_uid:
            return False, "Seul le Grand Aumônier peut créer des chats privés."
        
        for m in membres:
            if m not in self.players:
                return False, f"Joueur {m} introuvable."
            if self.players[m].is_king:
                return False, "Le Roi ne peut pas être invité dans un chat privé."
        
        if createur_uid not in membres:
            membres.append(createur_uid)
        
        chat = ChatPrive(
            id=str(uuid.uuid4()),
            nom=nom,
            createur_uid=createur_uid,
            membres=membres
        )
        self.chats_prives.append(chat)
        self.log.append(f"⛪ {self.players[createur_uid].pseudo} crée un chat privé : {nom}")
        
        for uid_membre in membres:
            self.add_personal_log(uid_membre, f"📩 Vous avez été invité dans le chat privé '{nom}'.")
        
        return True, "ok"

    def envoyer_message_prive(self, chat_id: str, expediteur_uid: str, message: str) -> tuple[bool, str]:
        chat = next((c for c in self.chats_prives if c.id == chat_id), None)
        if not chat:
            return False, "Chat introuvable."
        if expediteur_uid not in chat.membres:
            return False, "Vous n'êtes pas membre de ce chat."
        if not chat.actif:
            return False, "Ce chat est inactif."
        
        if self.securite_active and self.securite_active.get("cible_uid") == expediteur_uid:
            return False, "Vous êtes en arrestation préventive et ne pouvez pas envoyer de messages."
        
        chat.messages.append({
            "uid": expediteur_uid,
            "pseudo": self.players[expediteur_uid].pseudo,
            "message": message[:500],
            "timestamp": datetime.now().isoformat()
        })
        
        for membre in chat.membres:
            if membre != expediteur_uid:
                emit("message_prive", {
                    "chat_id": chat_id,
                    "chat_nom": chat.nom,
                    "expediteur": self.players[expediteur_uid].pseudo,
                    "message": message[:500]
                }, room=self.players[membre].sid)
        
        return True, "ok"

    # ============================================================================
    # AUDIENCE ROYALE PRIVÉE
    # ============================================================================

    def convoquer_audience(self, roi_uid: str, cible_uid: str) -> tuple[bool, str]:
        if roi_uid != self.king_uid():
            return False, "Seul le Roi peut convoquer une audience."
        if self.phase != Phase.DISCUSSION:
            return False, "L'audience ne peut être convoquée qu'en phase de discussion."
        if self.audience_active:
            return False, "Une audience est déjà en cours."
        if cible_uid not in self.players:
            return False, "Cible introuvable."
        if cible_uid == roi_uid:
            return False, "Le Roi ne peut pas se convoquer lui-même."
        if self.players[cible_uid].is_king:
            return False, "Le Roi ne peut pas se convoquer lui-même."
        if not self.players[cible_uid].role_ids:
            return False, "Cette personne n'a pas de portefeuille."
        
        temps_restant = self.timer_seconds
        self.timer_running = False
        
        self.audience_active = AudienceRoyale(
            roi_uid=roi_uid,
            cible_uid=cible_uid,
            debut=time.time(),
            duree=DUREE_AUDIENCE
        )
        
        self.log.append(f"👑 {self.players[roi_uid].pseudo} convoque {self.players[cible_uid].pseudo} en Audience Royale Privée !")
        
        emit("audience_debute", {
            "roi": self.players[roi_uid].pseudo,
            "cible": self.players[cible_uid].pseudo,
            "duree": DUREE_AUDIENCE
        }, room=self.players[roi_uid].sid)
        
        emit("audience_debute", {
            "roi": self.players[roi_uid].pseudo,
            "cible": self.players[cible_uid].pseudo,
            "duree": DUREE_AUDIENCE
        }, room=self.players[cible_uid].sid)
        
        for uid, p in self.players.items():
            if uid != roi_uid and uid != cible_uid:
                emit("audience_notification", {
                    "roi": self.players[roi_uid].pseudo,
                    "cible": self.players[cible_uid].pseudo
                }, room=p.sid)
        
        threading.Timer(DUREE_AUDIENCE, self._fin_audience, args=[roi_uid, cible_uid, temps_restant]).start()
        
        return True, "ok"

    def _fin_audience(self, roi_uid: str, cible_uid: str, temps_restant: int = 0) -> None:
        if self.audience_active:
            self.audience_active.actif = False
            self.audience_active = None
            if self.phase == Phase.DISCUSSION:
                self.timer_running = True
                self.timer_seconds = max(1, temps_restant)
        
        emit("audience_terminée", {
            "roi": self.players[roi_uid].pseudo,
            "cible": self.players[cible_uid].pseudo
        }, room=self.room_code)

    def envoyer_audience(self, expediteur_uid: str, message: str) -> tuple[bool, str]:
        if not self.audience_active or not self.audience_active.actif:
            return False, "Aucune audience active."
        if expediteur_uid not in (self.audience_active.roi_uid, self.audience_active.cible_uid):
            return False, "Vous n'êtes pas dans l'audience."
        
        destinateur = self.audience_active.roi_uid if expediteur_uid == self.audience_active.cible_uid else self.audience_active.cible_uid
        
        message_data = {
            "expediteur": self.players[expediteur_uid].pseudo,
            "message": message[:500]
        }
        
        emit("message_audience", message_data, room=self.players[expediteur_uid].sid)
        emit("message_audience", message_data, room=self.players[destinateur].sid)
        
        return True, "ok"

    # ============================================================================
    # AUTRES MÉTHODES EXISTANTES
    # ============================================================================

    def resign(self, role_id: str, successor_uid: str) -> bool:
        if successor_uid == self.king_uid():
            return False
        successor = self.players.get(successor_uid)
        if successor is None:
            return False
        old_uid = self.ministre_uid_for_role(role_id)
        if old_uid is None or old_uid == successor_uid:
            return False
        self.players[old_uid].role_ids.remove(role_id)
        self.players[old_uid].resigned_roles.append(role_id)
        if role_id not in successor.role_ids:
            successor.role_ids.append(role_id)
        cumul = " (cumul)" if len(successor.role_ids) > 1 else ""
        self.log.append(f"💥 {self.players[old_uid].pseudo} démissionne de {ROLES[role_id].name} ! Transféré à {successor.pseudo}{cumul}.")
        return True

    def revoke(self, role_id: str, successor_uid: str, requester_uid: str) -> tuple[bool, str]:
        if requester_uid != self.king_uid():
            return False, "Seul le Roi peut révoquer."
        if self.revocations_restantes <= 0:
            return False, "Le Roi a épuisé ses droits de révocation."
        if successor_uid == self.king_uid():
            return False, "Le Roi ne peut pas se nommer."
        successor = self.players.get(successor_uid)
        if successor is None:
            return False, "Successeur introuvable."
        old_uid = self.ministre_uid_for_role(role_id)
        if old_uid is None or old_uid == successor_uid:
            return False, "Ce ministère ne peut pas être révoqué ainsi."
        self.players[old_uid].role_ids.remove(role_id)
        self.players[old_uid].resigned_roles.append(role_id)
        if role_id not in successor.role_ids:
            successor.role_ids.append(role_id)
        self.revocations_restantes -= 1
        cumul = " (cumul)" if len(successor.role_ids) > 1 else ""
        self.log.append(f"👑 Le Roi destitue {self.players[old_uid].pseudo} de {ROLES[role_id].name} ! Successeur : {successor.pseudo}{cumul}.")
        return True, "ok"

    def launch_enterprise(self, entreprise_id: str, requester_uid: str) -> tuple[bool, str]:
        if requester_uid != self.king_uid():
            return False, "Seul le Roi peut ordonner une Grande Entreprise."
        if self.entreprise_active is not None:
            return False, "Une entreprise est déjà en cours."
        if entreprise_id in self.entreprises_utilisees:
            return False, "Cette entreprise a déjà eu lieu."
        ent = ENTERPRISE_BY_ID.get(entreprise_id)
        if ent is None:
            return False, "Entreprise inconnue."
        presents = self.roles_presents()
        if not all(r in presents for r in ent["roles_requis"]):
            return False, "Ministères manquants."
        self.entreprise_active = {
            "id": entreprise_id,
            "investissements": {r: False for r in ent["roles_requis"]},
        }
        self.entreprises_utilisees.append(entreprise_id)
        self.log.append(f"👑 Le Roi ordonne : « {ent['nom']} » !")
        return True, "ok"

    def investir_entreprise(self, role_id: str, requester_uid: str) -> tuple[bool, str]:
        if self.entreprise_active is None:
            return False, "Aucune entreprise en cours."
        if role_id not in self.entreprise_active["investissements"]:
            return False, "Ce ministère n'est pas concerné."
        uid = self.ministre_uid_for_role(role_id)
        if uid != requester_uid:
            return False, "Vous ne détenez pas ce ministère."
        p = self.players[uid]
        ent = ENTERPRISE_BY_ID[self.entreprise_active["id"]]
        cout = ent["cout_investissement"]
        if p.or_personnel < cout:
            return False, f"Il faut {cout} Or personnel."
        p.or_personnel -= cout
        self.entreprise_active["investissements"][role_id] = True
        self.log.append(f"⚙️ {p.pseudo} s'investit dans « {ent['nom']} ».")
        return True, "ok"

    def aider_tache(self, requester_uid: str) -> tuple[bool, str]:
        if self.tache_active is None or self.tache_active["role_aide"] is None:
            return False, "Aucune aide requise."
        role_aide = self.tache_active["role_aide"]
        uid = self.ministre_uid_for_role(role_aide)
        if uid != requester_uid:
            return False, "Vous n'êtes pas le ministère sollicité."
        p = self.players[uid]
        cout = self.tache_active["cout_aide"]
        if p.or_personnel < cout:
            return False, f"Il faut {cout} Or personnel."
        p.or_personnel -= cout
        self.tache_active["aide_obtenue"] = True
        self.log.append(f"🤝 {p.pseudo} apporte l'aide demandée.")
        return True, "ok"

    def accomplir_tache(self, requester_uid: str) -> tuple[bool, str]:
        if self.tache_active is None:
            return False, "Aucune mission en cours."
        role_id = self.tache_active["role_id"]
        uid = self.ministre_uid_for_role(role_id)
        if uid != requester_uid:
            return False, "Cette mission n'est pas la vôtre."
        if not self.tache_active["aide_obtenue"]:
            return False, "L'aide requise n'a pas été obtenue."
        self.tache_active["accomplie"] = True
        self.log.append(f"🎯 {self.players[uid].pseudo} accomplit la mission.")
        return True, "ok"

    def use_pouvoir(self, role_id: str, requester_uid: str, target_role_id: str | None = None,
                    decret_id: str | None = None, montant: int | None = None) -> tuple[bool, str, dict | None]:
        if role_id == "finances":
            if not decret_id or not target_role_id or montant is None:
                return False, "Il faut spécifier un décret, une cible et un montant.", None
            ok, msg = self.use_pot_de_vin_institutionnel(requester_uid, decret_id, target_role_id, montant)
            return ok, msg, None
        elif role_id == "connetable":
            if not target_role_id:
                return False, "Il faut spécifier une cible.", None
            ok, msg = self.use_arrestation_preventive(requester_uid, target_role_id)
            return ok, msg, None
        elif role_id == "subsistances":
            ok, msg = self.use_veto_populaire(requester_uid)
            return ok, msg, None
        elif role_id == "interieur":
            return True, "La falsification se fait lors du rapport de crise.", None
        else:
            return False, "Pouvoir inconnu.", None

    # ========== Sérialisation ==========

    def to_dict(self) -> dict:
        return {
            "room_code": self.room_code,
            "phase": self.phase.value,
            "cycle": self.cycle,
            "stabilite": self.stabilite,
            "or_public": self.or_public,
            "active_role_ids": self.active_role_ids,
            "players": {uid: p.to_public_dict() for uid, p in self.players.items()},
            "deck": self.deck,
            "deck_position": self.deck_position,
            "current_card_id": self.current_card_id,
            "current_card_revealed": self.current_card_revealed,
            "decisions": self.decisions,
            "interieur_falsifie": self.interieur_falsifie,
            "log": self.log[-50:],
            "winner_uid": self.winner_uid,
            "final_scores": self.final_scores,
            "host_uid": self.host_uid,
            "revocations_restantes": self.revocations_restantes,
            "pouvoirs_utilises": self.pouvoirs_utilises,
            "entreprises_utilisees": self.entreprises_utilisees,
            "entreprise_active": self.entreprise_active,
            "dettes_a_venir": self.dettes_a_venir,
            "tache_active": self.tache_active,
            "dernier_pouvoir_info": self.dernier_pouvoir_info,
            "timer_seconds": self.timer_seconds,
            "timer_running": self.timer_running,
            "dossiers_actifs": self.dossiers_actifs,
            "dossiers_termines": self.dossiers_termines,
            "decrets_actifs": self.decrets_actifs,
            "decrets_historique": self.decrets_historique,
            "tribunal_actif": self.tribunal_actif,
            "tribunal_historique": self.tribunal_historique,
            "chats_prives": [asdict(c) for c in self.chats_prives],
            "audience_active": asdict(self.audience_active) if self.audience_active else None,
            "securite_active": self.securite_active,
        }

    @staticmethod
    def from_dict(data: dict) -> "GameState":
        gs = GameState(room_code=data["room_code"])
        gs.phase = Phase(data["phase"])
        gs.cycle = data["cycle"]
        gs.stabilite = data["stabilite"]
        gs.or_public = data["or_public"]
        gs.active_role_ids = data["active_role_ids"]
        gs.players = {uid: PlayerState(**pdata) for uid, pdata in data["players"].items()}
        gs.deck = data["deck"]
        gs.deck_position = data["deck_position"]
        gs.current_card_id = data["current_card_id"]
        gs.current_card_revealed = data["current_card_revealed"]
        gs.decisions = data["decisions"]
        gs.interieur_falsifie = data["interieur_falsifie"]
        gs.log = data["log"]
        gs.winner_uid = data["winner_uid"]
        gs.revocations_restantes = data.get("revocations_restantes", REVOCATIONS_ROYALES_INITIALES)
        gs.pouvoirs_utilises = data.get("pouvoirs_utilises", [])
        gs.entreprises_utilisees = data.get("entreprises_utilisees", [])
        gs.entreprise_active = data.get("entreprise_active")
        gs.dettes_a_venir = data.get("dettes_a_venir", {})
        gs.tache_active = data.get("tache_active")
        gs.dernier_pouvoir_info = data.get("dernier_pouvoir_info")
        gs.final_scores = data["final_scores"]
        gs.host_uid = data["host_uid"]
        gs.timer_seconds = data.get("timer_seconds", 0)
        gs.timer_running = data.get("timer_running", False)
        gs.dossiers_actifs = data.get("dossiers_actifs", [])
        gs.dossiers_termines = data.get("dossiers_termines", [])
        gs.decrets_actifs = data.get("decrets_actifs", [])
        gs.decrets_historique = data.get("decrets_historique", [])
        gs.tribunal_actif = data.get("tribunal_actif")
        gs.tribunal_historique = data.get("tribunal_historique", [])
        gs.chats_prives = [ChatPrive(**c) for c in data.get("chats_prives", [])]
        audience_data = data.get("audience_active")
        gs.audience_active = AudienceRoyale(**audience_data) if audience_data else None
        gs.securite_active = data.get("securite_active")
        return gs


# ============================================================================
# APPLICATION FLASK + SOCKETIO
# ============================================================================

init_db()

_sid_index: dict[str, tuple[str, str]] = {}
_rooms: dict[str, GameState] = {}
_rooms_lock = threading.Lock()
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def generate_room_code(length: int = 5) -> str:
    while True:
        code = "".join(random.choice(CODE_ALPHABET) for _ in range(length))
        with _rooms_lock:
            in_memory = code in _rooms
        if not in_memory and not room_exists_in_db(code):
            return code


def create_room() -> GameState:
    code = generate_room_code()
    gs = GameState(room_code=code)
    with _rooms_lock:
        _rooms[code] = gs
    persist(gs)
    return gs


def get_room(room_code: str) -> GameState | None:
    room_code = room_code.upper()
    with _rooms_lock:
        gs = _rooms.get(room_code)
    if gs is not None:
        return gs
    data = load_game(room_code)
    if data is None:
        return None
    gs = GameState.from_dict(data)
    with _rooms_lock:
        _rooms[room_code] = gs
    return gs


def persist(gs: GameState) -> None:
    save_game(gs.room_code, gs.to_dict())


def public_state(gs: GameState) -> dict:
    d = gs.to_dict()
    if gs.phase == Phase.DECISION:
        d["decisions"] = {role: True for role in gs.decisions}
    return d


def roles_catalog() -> dict:
    return {
        "king": KING_ROLE,
        "roles": {rid: {
            "id": r.id, "name": r.name, "icon": r.icon, "color": r.color,
            "flux": r.flux, "voie_serviteur": r.voie_serviteur, "voie_fourbe": r.voie_fourbe,
            "niveau_desc": r.niveau_desc,
            "statut_ministere": r.statut_ministere,
            "titre_courtisan": r.titre_courtisan,
            "pouvoir_nom": r.pouvoir_nom, "pouvoir_desc": r.pouvoir_desc,
        } for rid, r in ROLES.items()},
        "role_order": ROLE_ORDER,
        "nb_cycles": NB_CYCLES,
        "revocations_initiales": REVOCATIONS_ROYALES_INITIALES,
        "taches": TASK_CATALOG,
        "entreprises": ENTERPRISE_CATALOG,
        "dossiers": DOSSIERS_CATALOG,
    }


def broadcast_state(gs: GameState) -> None:
    socketio.emit("state_update", public_state(gs), room=gs.room_code)
    for uid, p in gs.players.items():
        if p.personal_log:
            socketio.emit("personal_log", {"logs": p.personal_log}, room=p.sid)
    persist(gs)


def broadcast_timer(gs: GameState) -> None:
    socketio.emit("timer_update", {"seconds": gs.timer_seconds}, room=gs.room_code)


def nb_ministres_for(total_players: int) -> int:
    return min(5, total_players - 1)


# ---- Timer thread ----
def timer_loop():
    while True:
        time.sleep(1)
        with _rooms_lock:
            rooms_copy = list(_rooms.values())
        for gs in rooms_copy:
            if gs.timer_running and gs.phase == Phase.DISCUSSION:
                changed = gs.tick_timer()
                if changed:
                    broadcast_state(gs)
                    socketio.emit("timer_finished", {}, room=gs.room_code)
                else:
                    broadcast_timer(gs)


timer_thread = threading.Thread(target=timer_loop, daemon=True)
timer_thread.start()


# ---------- Routes HTTP ----------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/room/<room_code>")
def room_page(room_code):
    return render_template("room.html", room_code=room_code.upper())


@app.route("/regles")
def regles_page():
    return render_template("regles.html")


@app.route("/api/roles")
def api_roles():
    return jsonify(roles_catalog())


@app.route("/healthz")
def healthz():
    return {"status": "ok"}


# ---------- SocketIO Events ----------
@socketio.on("create_room")
def on_create_room(data):
    pseudo = (data or {}).get("pseudo", "").strip()[:24] or "Joueur"
    gs = create_room()
    player_uid = str(uuid.uuid4())
    gs.add_player(player_uid, request.sid, pseudo)
    join_room(gs.room_code)
    _sid_index[request.sid] = (gs.room_code, player_uid)
    emit("room_joined", {"room_code": gs.room_code, "player_uid": player_uid, "is_host": True})
    broadcast_state(gs)


@socketio.on("join_room_event")
def on_join_room(data):
    data = data or {}
    pseudo = (data.get("pseudo") or "").strip()[:24] or "Joueur"
    room_code = (data.get("room_code") or "").strip().upper()
    existing_uid = data.get("player_uid")

    gs = get_room(room_code)
    if gs is None:
        emit("join_error", {"message": "Ce salon n'existe pas."})
        return

    if existing_uid and existing_uid in gs.players:
        player_uid = existing_uid
        gs.players[player_uid].sid = request.sid
        gs.players[player_uid].connected = True
    else:
        if gs.phase != Phase.LOBBY:
            emit("join_error", {"message": "La partie a déjà commencé."})
            return
        if len(gs.players) >= MAX_TOTAL_PLAYERS:
            emit("join_error", {"message": f"Salon complet ({MAX_TOTAL_PLAYERS} max)."})
            return
        player_uid = str(uuid.uuid4())
        gs.add_player(player_uid, request.sid, pseudo)

    join_room(room_code)
    _sid_index[request.sid] = (room_code, player_uid)
    emit("room_joined", {"room_code": gs.room_code, "player_uid": player_uid, "is_host": gs.host_uid == player_uid})
    broadcast_state(gs)


@socketio.on("start_game")
def on_start_game(data):
    info = _sid_index.get(request.sid)
    if not info:
        return
    room_code, player_uid = info
    gs = get_room(room_code)
    if gs is None or gs.host_uid != player_uid:
        emit("action_error", {"message": "Seul l'hôte peut démarrer."})
        return
    if len(gs.players) < MIN_TOTAL_PLAYERS:
        emit("action_error", {"message": f"Minimum {MIN_TOTAL_PLAYERS} joueurs."})
        return
    if gs.phase != Phase.LOBBY:
        return

    king_uid = (data or {}).get("king_uid") or player_uid
    if king_uid not in gs.players:
        king_uid = player_uid

    gs.assign_roles(king_uid)
    broadcast_state(gs)


@socketio.on("submit_decision")
def on_submit_decision(data):
    info = _sid_index.get(request.sid)
    if not info:
        return
    room_code, player_uid = info
    gs = get_room(room_code)
    if gs is None or gs.phase != Phase.DECISION:
        return

    niveau = int((data or {}).get("niveau", 0))
    role_id = (data or {}).get("role_id")
    if not (0 <= niveau <= 3):
        return

    player = gs.players.get(player_uid)
    if player is None or role_id not in player.role_ids:
        emit("action_error", {"message": "Vous ne contrôlez pas ce portefeuille."})
        return

    gs.submit_decision(role_id, niveau)

    if gs.all_decisions_in():
        if "interieur" in gs.active_role_ids:
            card = gs.draw_crisis_card()
            broadcast_state(gs)
            
            interieur_uid = gs.ministre_uid_for_role("interieur")
            if interieur_uid:
                interieur_sid = gs.players[interieur_uid].sid
                options = gs.generer_options_falsification(card)
                socketio.emit("crisis_drawn", {
                    "card": card,
                    "can_falsify": gs.decisions.get("interieur", 0) == 0,
                    "options": options
                }, room=interieur_sid)
                
                for uid, p in gs.players.items():
                    if uid != interieur_uid:
                        socketio.emit("crisis_attente", {
                            "message": "Le Ministre de l'Intérieur prépare son rapport..."
                        }, room=p.sid)
            return
        else:
            summary = gs.resolve_cycle()
            broadcast_state(gs)
            socketio.emit("cycle_resolved", summary, room=gs.room_code)
            return

    broadcast_state(gs)


@socketio.on("falsify_report")
def on_falsify_report(data):
    info = _sid_index.get(request.sid)
    if not info:
        return
    room_code, player_uid = info
    gs = get_room(room_code)
    if gs is None or gs.phase != Phase.RAPPORT:
        emit("action_error", {"message": "Phase de rapport terminée."})
        return

    player = gs.players.get(player_uid)
    if player is None or "interieur" not in player.role_ids:
        emit("action_error", {"message": "Vous n'êtes pas le Ministre de l'Intérieur."})
        return

    if gs.decisions.get("interieur", 0) != 0:
        emit("action_error", {"message": "Vous ne pouvez falsifier que si votre robinet est fermé."})
        return

    stab = int((data or {}).get("stabilite_annoncee", 0))
    org = int((data or {}).get("or_annonce", 0))
    option_id = (data or {}).get("option_id", "inconnu")
    
    gs.falsify_report(stab, org)
    gs.log.append(f"📜 Le Ministre de l'Intérieur a falsifié le rapport (option: {option_id}).")

    summary = gs.resolve_cycle()
    broadcast_state(gs)
    socketio.emit("cycle_resolved", summary, room=gs.room_code)


@socketio.on("confirm_report")
def on_confirm_report(data):
    info = _sid_index.get(request.sid)
    if not info:
        return
    room_code, player_uid = info
    gs = get_room(room_code)
    if gs is None or gs.phase != Phase.RAPPORT:
        emit("action_error", {"message": "Phase de rapport terminée."})
        return

    player = gs.players.get(player_uid)
    if player is None or "interieur" not in player.role_ids:
        emit("action_error", {"message": "Vous n'êtes pas le Ministre de l'Intérieur."})
        return

    summary = gs.resolve_cycle()
    broadcast_state(gs)
    socketio.emit("cycle_resolved", summary, room=gs.room_code)


@socketio.on("next_cycle")
def on_next_cycle(data):
    info = _sid_index.get(request.sid)
    if not info:
        return
    room_code, player_uid = info
    gs = get_room(room_code)
    if gs is None or gs.phase != Phase.RESOLUTION:
        return
    # ✅ On passe en discussion, pas directement en décision
    gs.start_discussion_phase()
    broadcast_state(gs)


@socketio.on("resign")
def on_resign(data):
    info = _sid_index.get(request.sid)
    if not info:
        return
    room_code, player_uid = info
    gs = get_room(room_code)
    if gs is None:
        return
    role_id = (data or {}).get("role_id")
    successor_uid = (data or {}).get("successor_uid")
    player = gs.players.get(player_uid)
    if player is None or role_id not in player.role_ids:
        emit("action_error", {"message": "Vous ne pouvez démissionner que de votre propre poste."})
        return
    ok = gs.resign(role_id, successor_uid)
    if not ok:
        emit("action_error", {"message": "Démission impossible."})
        return
    broadcast_state(gs)


@socketio.on("revoke")
def on_revoke(data):
    info = _sid_index.get(request.sid)
    if not info:
        return
    room_code, player_uid = info
    gs = get_room(room_code)
    if gs is None:
        return
    role_id = (data or {}).get("role_id")
    successor_uid = (data or {}).get("successor_uid")
    ok, message = gs.revoke(role_id, successor_uid, requester_uid=player_uid)
    if not ok:
        emit("action_error", {"message": message})
        return
    broadcast_state(gs)


@socketio.on("use_pouvoir")
def on_use_pouvoir(data):
    info = _sid_index.get(request.sid)
    if not info:
        return
    room_code, player_uid = info
    gs = get_room(room_code)
    if gs is None:
        return
    role_id = (data or {}).get("role_id")
    target_role_id = (data or {}).get("target_role_id")
    decret_id = (data or {}).get("decret_id")
    montant = (data or {}).get("montant")
    ok, message, payload = gs.use_pouvoir(role_id, requester_uid=player_uid,
                                           target_role_id=target_role_id,
                                           decret_id=decret_id,
                                           montant=montant)
    if not ok:
        emit("action_error", {"message": message})
        return
    broadcast_state(gs)


@socketio.on("launch_enterprise")
def on_launch_enterprise(data):
    info = _sid_index.get(request.sid)
    if not info:
        return
    room_code, player_uid = info
    gs = get_room(room_code)
    if gs is None:
        return
    entreprise_id = (data or {}).get("entreprise_id")
    ok, message = gs.launch_enterprise(entreprise_id, requester_uid=player_uid)
    if not ok:
        emit("action_error", {"message": message})
        return
    broadcast_state(gs)


@socketio.on("investir_entreprise")
def on_investir_entreprise(data):
    info = _sid_index.get(request.sid)
    if not info:
        return
    room_code, player_uid = info
    gs = get_room(room_code)
    if gs is None:
        return
    role_id = (data or {}).get("role_id")
    ok, message = gs.investir_entreprise(role_id, requester_uid=player_uid)
    if not ok:
        emit("action_error", {"message": message})
        return
    broadcast_state(gs)


@socketio.on("aider_tache")
def on_aider_tache(data):
    info = _sid_index.get(request.sid)
    if not info:
        return
    room_code, player_uid = info
    gs = get_room(room_code)
    if gs is None:
        return
    ok, message = gs.aider_tache(requester_uid=player_uid)
    if not ok:
        emit("action_error", {"message": message})
        return
    broadcast_state(gs)


@socketio.on("accomplir_tache")
def on_accomplir_tache(data):
    info = _sid_index.get(request.sid)
    if not info:
        return
    room_code, player_uid = info
    gs = get_room(room_code)
    if gs is None:
        return
    ok, message = gs.accomplir_tache(requester_uid=player_uid)
    if not ok:
        emit("action_error", {"message": message})
        return
    broadcast_state(gs)


# ---------- NOUVELLES FONCTIONNALITÉS ----------

@socketio.on("launch_dossier")
def on_launch_dossier(data):
    info = _sid_index.get(request.sid)
    if not info:
        return
    room_code, player_uid = info
    gs = get_room(room_code)
    if gs is None:
        return
    dossier_id = (data or {}).get("dossier_id")
    ok, msg = gs.lancer_dossier(dossier_id, player_uid)
    if not ok:
        emit("action_error", {"message": msg})
        return
    broadcast_state(gs)


@socketio.on("contribuer_dossier")
def on_contribuer_dossier(data):
    info = _sid_index.get(request.sid)
    if not info:
        return
    room_code, player_uid = info
    gs = get_room(room_code)
    if gs is None:
        return
    dossier_id = (data or {}).get("dossier_id")
    ressource = (data or {}).get("ressource")
    montant = int((data or {}).get("montant", 0))
    if montant <= 0:
        emit("action_error", {"message": "Montant invalide."})
        return
    ok, msg = gs.contribuer_dossier(dossier_id, ressource, montant, player_uid)
    if not ok:
        emit("action_error", {"message": msg})
        return
    broadcast_state(gs)


@socketio.on("proposer_decret")
def on_proposer_decret(data):
    info = _sid_index.get(request.sid)
    if not info:
        return
    room_code, player_uid = info
    gs = get_room(room_code)
    if gs is None:
        return
    description = (data or {}).get("description", "").strip()
    if not description:
        emit("action_error", {"message": "Description requise."})
        return
    public_effects = (data or {}).get("public_effects", {})
    secret_target_uid = (data or {}).get("secret_target_uid")
    secret_effects = (data or {}).get("secret_effects", {})
    ok, msg = gs.proposer_decret(player_uid, description, public_effects, secret_target_uid, secret_effects)
    if not ok:
        emit("action_error", {"message": msg})
        return
    broadcast_state(gs)


@socketio.on("voter_decret")
def on_voter_decret(data):
    info = _sid_index.get(request.sid)
    if not info:
        return
    room_code, player_uid = info
    gs = get_room(room_code)
    if gs is None:
        return
    vote_oui = (data or {}).get("vote_oui", True)
    ok, msg = gs.voter_decret(player_uid, vote_oui)
    if not ok:
        emit("action_error", {"message": msg})
        return
    broadcast_state(gs)


@socketio.on("offrir_bribe")
def on_offrir_bribe(data):
    info = _sid_index.get(request.sid)
    if not info:
        return
    room_code, player_uid = info
    gs = get_room(room_code)
    if gs is None:
        return
    decret_id = (data or {}).get("decret_id")
    cible_uid = (data or {}).get("cible_uid")
    montant = int((data or {}).get("montant", 0))
    if montant <= 0:
        emit("action_error", {"message": "Montant invalide."})
        return
    ok, msg = gs.offrir_bribe(decret_id, cible_uid, montant, player_uid)
    if not ok:
        emit("action_error", {"message": msg})
        return
    broadcast_state(gs)


@socketio.on("initier_tribunal")
def on_initier_tribunal(data):
    info = _sid_index.get(request.sid)
    if not info:
        return
    room_code, player_uid = info
    gs = get_room(room_code)
    if gs is None:
        return
    accuse_uid = (data or {}).get("accuse_uid")
    if not accuse_uid:
        emit("action_error", {"message": "Accusé requis."})
        return
    ok, msg = gs.initier_tribunal(player_uid, accuse_uid)
    if not ok:
        emit("action_error", {"message": msg})
        return
    for uid, p in gs.players.items():
        if uid != player_uid:
            socketio.emit("tribunal_vote_required", {
                "accuse_pseudo": gs.players[accuse_uid].pseudo,
                "accuse_uid": accuse_uid
            }, room=p.sid)
    broadcast_state(gs)


@socketio.on("voter_tribunal")
def on_voter_tribunal(data):
    info = _sid_index.get(request.sid)
    if not info:
        return
    room_code, player_uid = info
    gs = get_room(room_code)
    if gs is None:
        return
    coupable = (data or {}).get("coupable", True)
    ok, msg = gs.voter_tribunal(player_uid, coupable)
    if not ok:
        emit("action_error", {"message": msg})
        return
    broadcast_state(gs)


# ---------- CHATS PRIVÉS ----------
@socketio.on("creer_chat_prive")
def on_creer_chat_prive(data):
    info = _sid_index.get(request.sid)
    if not info:
        return
    room_code, player_uid = info
    gs = get_room(room_code)
    if gs is None:
        return
    nom = (data or {}).get("nom", "").strip()
    membres = (data or {}).get("membres", [])
    if not nom:
        emit("action_error", {"message": "Donnez un nom au chat."})
        return
    if not membres:
        emit("action_error", {"message": "Choisissez au moins un membre."})
        return
    ok, msg = gs.creer_chat_prive(player_uid, nom, membres)
    if not ok:
        emit("action_error", {"message": msg})
        return
    for uid in membres:
        socketio.emit("chat_prive_cree", {"nom": nom, "membres": [gs.players[u].pseudo for u in membres]}, room=gs.players[uid].sid)
    broadcast_state(gs)


@socketio.on("envoyer_message_prive")
def on_envoyer_message_prive(data):
    info = _sid_index.get(request.sid)
    if not info:
        return
    room_code, player_uid = info
    gs = get_room(room_code)
    if gs is None:
        return
    chat_id = (data or {}).get("chat_id")
    message = (data or {}).get("message", "").strip()
    if not chat_id or not message:
        emit("action_error", {"message": "Message invalide."})
        return
    ok, msg = gs.envoyer_message_prive(chat_id, player_uid, message)
    if not ok:
        emit("action_error", {"message": msg})
        return


# ---------- AUDIENCE ROYALE ----------
@socketio.on("convoquer_audience")
def on_convoquer_audience(data):
    info = _sid_index.get(request.sid)
    if not info:
        return
    room_code, player_uid = info
    gs = get_room(room_code)
    if gs is None:
        return
    cible_uid = (data or {}).get("cible_uid")
    if not cible_uid:
        emit("action_error", {"message": "Cible requise."})
        return
    ok, msg = gs.convoquer_audience(player_uid, cible_uid)
    if not ok:
        emit("action_error", {"message": msg})
        return
    broadcast_state(gs)


@socketio.on("envoyer_audience")
def on_envoyer_audience(data):
    info = _sid_index.get(request.sid)
    if not info:
        return
    room_code, player_uid = info
    gs = get_room(room_code)
    if gs is None:
        return
    message = (data or {}).get("message", "").strip()
    if not message:
        emit("action_error", {"message": "Message vide."})
        return
    ok, msg = gs.envoyer_audience(player_uid, message)
    if not ok:
        emit("action_error", {"message": msg})


@socketio.on("send_chat")
def on_send_chat(data):
    info = _sid_index.get(request.sid)
    if not info:
        return
    room_code, player_uid = info
    gs = get_room(room_code)
    if gs is None:
        return
    player = gs.players.get(player_uid)
    if player is None:
        return
    message = (data or {}).get("message", "").strip()[:500]
    if not message:
        return
    socketio.emit("chat_message", {
        "pseudo": player.pseudo,
        "message": message,
        "is_king": player.is_king,
    }, room=room_code)


@socketio.on("disconnect")
def on_disconnect():
    info = _sid_index.pop(request.sid, None)
    if not info:
        return
    room_code, player_uid = info
    gs = get_room(room_code)
    if gs is None:
        return
    player = gs.players.get(player_uid)
    if player:
        player.connected = False
        broadcast_state(gs)


# ============================================================================
# LANCEMENT
# ============================================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port)
