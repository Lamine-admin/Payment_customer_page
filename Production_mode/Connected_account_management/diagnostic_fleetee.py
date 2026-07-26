"""
Script de diagnostic - Affiche tous les champs retournés par l'API FLEETEE
pour une réservation donnée, afin d'identifier le champ "solde restant dû".

Utilisation :
  python diagnostic_fleetee.py
  → Entrer la référence d'une réservation prolongée (ex: LOC-P0003-2025-01-0012)
"""

import requests
import json
from decouple import config

fleetee_api_key   = config('FLEETEE_API_KEY')
fleetee_secret_key = config('FLEETEE_SECRET_KEY')

FLEETEE_API_BASE = "https://api.fleetee.io"

headers = {
    "x-api-key": fleetee_api_key,
    "secret-key": fleetee_secret_key,
    "Accept": "application/json"
}

def inspect_booking(reference):
    print(f"\n🔍 Inspection de la réservation : {reference}\n")
    response = requests.get(
        f"{FLEETEE_API_BASE}/bookings",
        headers=headers,
        params={"reference": reference}
    )

    if response.status_code != 200:
        print(f"❌ Erreur API : {response.status_code} — {response.text}")
        return

    data = response.json()
    bookings = data.get('list', [])

    if not bookings:
        print("❌ Aucune réservation trouvée pour cette référence.")
        return

    booking = bookings[0]

    # Afficher tous les champs de premier niveau
    print("=" * 60)
    print("📋 TOUS LES CHAMPS DE LA RÉSERVATION (premier niveau) :")
    print("=" * 60)
    for key, value in booking.items():
        if isinstance(value, dict):
            print(f"  {key}: {{...}}  ← objet imbriqué")
        elif isinstance(value, list):
            print(f"  {key}: [...]  ← liste")
        else:
            print(f"  {key}: {value}")

    # Zoom sur les champs liés au montant
    print("\n" + "=" * 60)
    print("💰 CHAMPS LIÉS AUX MONTANTS :")
    print("=" * 60)
    montant_keywords = ['amount', 'price', 'balance', 'paid', 'due',
                        'total', 'deposit', 'acompte', 'solde', 'reste',
                        'fee', 'cost', 'tarif', 'payment']
    for key, value in booking.items():
        if any(kw in key.lower() for kw in montant_keywords):
            print(f"  ✅ {key}: {value}")

    # Afficher les objets imbriqués complets
    print("\n" + "=" * 60)
    print("🔎 OBJETS IMBRIQUÉS COMPLETS :")
    print("=" * 60)
    for key, value in booking.items():
        if isinstance(value, (dict, list)):
            print(f"\n  [{key}]")
            print(json.dumps(value, indent=4, ensure_ascii=False))

if __name__ == "__main__":
    ref = input("Entrez la référence d'une réservation prolongée : ").strip()
    if ref:
        inspect_booking(ref)
    else:
        print("❌ Référence vide.")
