# -*- coding: utf-8 -*-
"""
════════════════════════════════════════════════════════════════════
 SYNCHRONISATION DES FICHIERS DE RÉFÉRENCE — Thecarsociety
════════════════════════════════════════════════════════════════════
 Met à jour, à partir du Google Sheets (registre) et de l'API Fleetee :

   • connected_accounts.json  (PRODUCTION : Projets/Production_mode/
                               Stripe_Onboarding/data/) — complète les
                               entrées existantes (nom) et ajoute les
                               nouveaux propriétaires. Structure conservée :
                               {id, email, name, role, registered_at}
   • carsitters.json          (compte Stripe carsitter -> coordonnées)
   • vehicules_proprietaires.json (véhicules : ajoute les NOUVEAUX,
                                   conserve les owner_code / carsitter_id
                                   déjà renseignés — aucune perte)

 Une sauvegarde .bak est créée avant toute écriture.

 Lancer :  python synchroniser_fichiers.py
           python synchroniser_fichiers.py --dry-run   (simulation)
════════════════════════════════════════════════════════════════════
"""
import json
import os
import re
import shutil
import sys
from datetime import datetime

import requests
from decouple import config

BASE = os.path.dirname(os.path.abspath(__file__))
DRY = "--dry-run" in sys.argv

# ⚠️ connected_accounts.json de PRODUCTION (et non la copie de test locale)
FIC_COMPTES = os.path.normpath(os.path.join(
    BASE, "..", "..", "..", "Projets", "Production_mode",
    "Stripe_Onboarding", "data", "connected_accounts.json"))
FIC_CARSITTERS = os.path.join(BASE, "carsitters.json")
FIC_VEHICULES = os.path.join(BASE, "vehicules_proprietaires.json")

# ── Registre Google Sheets ──────────────────────────────────────────
GSHEET_ID = config("GSHEET_ID",
                   default="1mn5OzZcAgPdant78-Uy9YRoMMAz3Piw_KjTPfT-FZ9A")
GSHEET_GID = config("GSHEET_GID", default="1311029633")
GSA_JSON = config("GSA_JSON",
                  default=os.path.join(BASE, "..", "..", "..",
                                       "Portail_inscription", "service_account.json"))

# ── API Fleetee ─────────────────────────────────────────────────────
FLEETEE_API = "https://api.fleetee.io"
H = {"x-api-key": config("FLEETEE_API_KEY"),
     "secret-key": config("FLEETEE_SECRET_KEY"),
     "Accept": "application/json"}


def sauvegarder(chemin):
    if os.path.exists(chemin):
        bak = f"{chemin}.bak_{datetime.now():%Y%m%d_%H%M%S}"
        shutil.copy2(chemin, bak)
        print(f"   💾 sauvegarde : {os.path.basename(bak)}")


def ecrire(chemin, donnees):
    if DRY:
        print(f"   🧪 [DRY-RUN] {os.path.basename(chemin)} non modifié")
        return
    sauvegarder(chemin)
    with open(chemin, "w", encoding="utf-8") as f:
        json.dump(donnees, f, ensure_ascii=False, indent=2)
    print(f"   ✅ écrit : {os.path.basename(chemin)}")


def charger(chemin, defaut):
    try:
        with open(chemin, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return defaut


# ════════════ 1) Lecture du registre Google Sheets ════════════
def lire_registre():
    """Retourne (proprietaires, carsitters) depuis le Google Sheets.
    Colonnes A-E = propriétaires (P), G-K = carsitters (C)."""
    import gspread
    if not os.path.isfile(GSA_JSON):
        sys.exit(f"❌ Clé de service Google introuvable : {GSA_JSON}\n"
                 f"   → indique son chemin dans le .env (GSA_JSON=...)")
    gc = gspread.service_account(filename=GSA_JSON)
    ws = gc.open_by_key(GSHEET_ID).get_worksheet_by_id(int(GSHEET_GID))
    lignes = ws.get_all_values()

    proprios, carsitters = {}, {}
    for r in lignes[1:]:                      # on saute l'en-tête
        r = r + [""] * (11 - len(r))          # sécurise la longueur
        # Propriétaires : A=code, B=stripe, C=email, D=nom, E=tel
        code, acct, email, nom, tel = (x.strip() for x in r[0:5])
        if re.fullmatch(r"P\d+", code) and acct.startswith("acct_"):
            proprios[code] = {"id": acct, "name": nom, "email": email,
                              "phone": tel}
        # Carsitters : G=code, H=stripe, I=email, J=nom, K=tel
        code_c, acct_c, email_c, nom_c, tel_c = (x.strip() for x in r[6:11])
        if re.fullmatch(r"C\d+", code_c) and acct_c.startswith("acct_"):
            carsitters[acct_c] = {"code": code_c, "name": nom_c,
                                  "email": email_c, "phone": tel_c}
    return proprios, carsitters


# ════════════ 2) Véhicules depuis Fleetee ════════════
def lire_vehicules_fleetee():
    vus, page, liste = set(), 0, []
    while True:
        r = requests.get(f"{FLEETEE_API}/vehicles", headers=H, timeout=30,
                         params={"page": page, "include_inactive": 1})
        r.raise_for_status()
        data = r.json()
        lot = [v for v in data.get("list", []) if v.get("id") not in vus]
        vus.update(v["id"] for v in lot)
        liste.extend(lot)
        if not lot or len(liste) >= data.get("total", 0):
            break
        page += 1
    return liste


# ════════════════════════ Exécution ════════════════════════
print(f"\n{'='*60}\n SYNCHRONISATION {'[DRY-RUN]' if DRY else '[PRODUCTION]'}\n{'='*60}")

print("\n1️⃣  Lecture du registre Google Sheets…")
proprios, carsitters = lire_registre()
print(f"   {len(proprios)} propriétaire(s) et {len(carsitters)} carsitter(s) trouvés")

# ── connected_accounts.json (PRODUCTION) ──
print("\n2️⃣  connected_accounts.json (production)")
print(f"   {FIC_COMPTES}")
anciens = charger(FIC_COMPTES, {})
resultat_comptes = dict(anciens)          # on repart de l'existant : rien n'est perdu
ajouts, completes = [], []
for code, v in proprios.items():
    if code in resultat_comptes:
        e = resultat_comptes[code]
        # complète uniquement les champs vides (on n'écrase jamais l'existant)
        if not e.get("name") and v.get("name"):
            e["name"] = v["name"]
            completes.append(code)
        if not e.get("email") and v.get("email"):
            e["email"] = v["email"]
        if not e.get("id") and v.get("id"):
            e["id"] = v["id"]
        e.setdefault("role", "P")
        e.setdefault("registered_at", "")
    else:
        resultat_comptes[code] = {
            "id": v["id"], "email": v["email"], "name": v.get("name", ""),
            "role": "P",
            "registered_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        }
        ajouts.append(code)
print(f"   avant : {len(anciens)} entrée(s) | après : {len(resultat_comptes)}")
if ajouts:
    print(f"   ➕ nouveaux propriétaires : {', '.join(sorted(ajouts))}")
if completes:
    print(f"   ✏️  noms complétés depuis le registre : {len(completes)} entrée(s)")
if not ajouts and not completes:
    print("   ✅ déjà à jour")
ecrire(FIC_COMPTES, dict(sorted(resultat_comptes.items())))

# ── carsitters.json ──
print("\n3️⃣  carsitters.json")
anciens_cs = charger(FIC_CARSITTERS, {})
ajouts_cs = [a for a in carsitters if a not in anciens_cs]
print(f"   avant : {len(anciens_cs)} | après : {len(carsitters)}")
if ajouts_cs:
    print(f"   ➕ ajouts : {', '.join(carsitters[a]['name'] for a in ajouts_cs)}")
ecrire(FIC_CARSITTERS, carsitters)

# ── vehicules_proprietaires.json ──
print("\n4️⃣  vehicules_proprietaires.json (Fleetee)")
vehicules_api = lire_vehicules_fleetee()
print(f"   {len(vehicules_api)} véhicule(s) sur Fleetee")
actuels = charger(FIC_VEHICULES, [])
par_id = {v.get("vehicle_id"): v for v in actuels if v.get("vehicle_id")}
par_plaque = {(v.get("plate") or "").upper(): v for v in actuels if v.get("plate")}

nouveaux_v, inchanges = [], 0
for v in vehicules_api:
    vid = v.get("id")
    plaque = (v.get("registration_number") or "").upper()
    existant = par_id.get(vid) or par_plaque.get(plaque)
    if existant:                       # on ne touche À RIEN (owner_code, carsitter_id…)
        existant["vehicle_id"] = vid   # on complète juste l'id si absent
        existant["plate"] = existant.get("plate") or plaque
        inchanges += 1
    else:
        nouveaux_v.append({
            "vehicle_id": vid,
            "plate": plaque,
            "owner_name": "",          # ⚠️ à compléter
            "owner_code": "",          # ⚠️ à compléter (Pxxxx)
            "photo_url": "",
            "carsitter_id": None,      # ⚠️ à compléter si véhicule chez un carsitter
            "brand": v.get("brand", ""),
            "model": v.get("model", ""),
        })

resultat = actuels + nouveaux_v
print(f"   {inchanges} véhicule(s) déjà connus (conservés tels quels)")
if nouveaux_v:
    print(f"   ➕ {len(nouveaux_v)} NOUVEAU(X) véhicule(s) :")
    for v in nouveaux_v:
        print(f"      • id {v['vehicle_id']} — {v['brand']} {v['model']} ({v['plate']})")
ecrire(FIC_VEHICULES, resultat)

# ── Contrôles de cohérence ──
print(f"\n5️⃣  Contrôles")
sans_code = [v for v in resultat if not v.get("owner_code")]
if sans_code:
    print(f"   ⚠️ {len(sans_code)} véhicule(s) SANS owner_code "
          f"(paiement non routé !) :")
    for v in sans_code[:15]:
        print(f"      • id {v.get('vehicle_id')} — {v.get('brand','')} "
              f"{v.get('model','')} ({v.get('plate','')})")
    if len(sans_code) > 15:
        print(f"      … et {len(sans_code)-15} autre(s)")
codes_inconnus = {v.get("owner_code") for v in resultat
                  if v.get("owner_code") and v["owner_code"] not in resultat_comptes}
if codes_inconnus:
    print(f"   ⚠️ owner_code absents de connected_accounts.json : "
          f"{', '.join(sorted(codes_inconnus))}")
cs_inconnus = {v.get("carsitter_id") for v in resultat
               if v.get("carsitter_id") and v["carsitter_id"] not in carsitters}
if cs_inconnus:
    print(f"   ⚠️ carsitter_id absents de carsitters.json : "
          f"{', '.join(sorted(cs_inconnus))}")
if not sans_code and not codes_inconnus and not cs_inconnus:
    print("   ✅ Tout est cohérent.")

print(f"\n{'='*60}")
if DRY:
    print(" DRY-RUN terminé : aucun fichier modifié.")
    print(" Relance sans --dry-run pour appliquer.")
else:
    print(" Synchronisation terminée.")
    print(" ⚠️ Complète les owner_code / carsitter_id des nouveaux véhicules")
    print("    dans vehicules_proprietaires.json.")
print("="*60)
input("\nEntrée pour fermer.")
