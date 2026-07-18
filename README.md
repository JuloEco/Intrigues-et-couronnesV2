# 👑 Intrigues & Couronne

**La frontière entre le dévouement et la haute trahison n'est dictée que par votre ambition.**

---

## 📖 Présentation

**Intrigues & Couronne** est un jeu de société politique en ligne, multijoueur et temps réel, qui plonge les joueurs dans les arcanes d'une cour royale versaillaise. Chaque participant incarne un ministre ou le Roi, et doit naviguer entre complots, corruption et gestion du Royaume pour accumuler la plus grande **Puissance Politique**... sans faire sombrer le Royaume dans l'anarchie ou la faillite.

---

## 🎮 Fonctionnalités

### 👑 Le Roi
- Arbitre suprême de la partie
- Peut **révoquer** un ministre (2 fois par partie)
- Peut **lancer des Dossiers Royaux** (projets collaboratifs)
- Peut **convoquer un Tribunal** (coût 10 Stabilité)
- Peut **convoquer une Audience Royale Privée** (45 secondes)

### 📜 Les Ministères (5 rôles)
| Ministère | Pouvoir Spécial |
|-----------|-----------------|
| 📜 **Intérieur** | Falsification de Rapport : modifie les chiffres des crises |
| 💰 **Finances** | Pot-de-Vin Institutionnel : achète des votes secrets |
| ⛪ **Aumônier** | Canaux Clandestins : crée des chats privés |
| 🌾 **Subsistances** | Veto Populaire : annule un décret |
| 🛡️ **Connétable** | Arrestation Préventive : isole un ministre |

### ⚙️ Mécaniques de jeu
- **8 cycles** de jeu
- **Timer de 55 secondes** pour les phases de négociation
- **Robinets à 4 niveaux** (Fermé / Restreint / Normal / Surchauffe)
- **Cartes de Crise** à gérer
- **Dossiers Royaux** collaboratifs
- **Décrets** avec votes et pots-de-vin
- **Tribunal Royal** pour juger les traîtres
- **Chambres secrètes** (chats privés exclusifs)
- **Audiences Royales** privées
- **Journal personnel** pour chaque joueur

### 🏆 Conditions de victoire
- **Défaite collective** : Stabilité ≤ 0 ou Caisses < 0
- **Victoire individuelle** : Puissance Politique maximale après 8 cycles
- **Puissance =** Or Personnel + Influence + Prestige + (Statut × 10) + (Stabilité ÷ 2)

---

## 🚀 Installation

### Prérequis
- Python 3.10 ou supérieur
- pip

### Étapes

```bash
# 1. Cloner le projet
git clone <url-du-projet>
cd intrigues_couronne

# 2. Créer un environnement virtuel (recommandé)
python -m venv venv
source venv/bin/activate  # Sur Linux/Mac
# ou
venv\Scripts\activate     # Sur Windows

# 3. Installer les dépendances
pip install -r requirements.txt

# 4. Lancer le serveur
python app.py
