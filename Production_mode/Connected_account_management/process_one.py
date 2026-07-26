"""
Script pour traiter UNE SEULE réservation manuellement (ex : prolongation).
Lance ce script à la place de Phase_7.py pour cibler une référence précise.

Usage :
    python process_one.py LOC-P0002-2026-04-0006
"""

import sys
from Phase_7 import (
    fleetee_api_get,
    get_owner_info,
    get_owner_code,
    build_effective_reference,
    make_unique_reference,
    load_reference_map,
    save_reference_map,
    create_payment_link,
    send_payment_link_auto,
    load_sent_references,
    save_sent_references,
    print,
)

def process_single(reference: str, force: bool = False):
    sent_refs = load_sent_references()

    if reference in sent_refs and not force:
        print(f"⚠️  Ce lien a déjà été envoyé pour {reference}. Relance avec force=True pour forcer.")
        return

    # Récupération de la réservation via Fleetee
    params = {"reference": reference}
    data = fleetee_api_get("/bookings", params=params)
    if not data or not data.get("list"):
        print(f"❌ Réservation introuvable : {reference}")
        return

    booking = data["list"][0]
    ref_found = booking.get("reference")
    status = booking.get("status")
    status_code = booking.get("status_code")
    print(f"📋 Réservation : {ref_found} | Statut : {status} (code {status_code})")

    # Infos client
    client_name = booking.get("customer", {}).get("name", "")
    email       = booking.get("customer", {}).get("email", "")

    # Infos véhicule
    vehicle    = booking.get("vehicle", {})
    vehicle_id = vehicle.get("id")
    plate      = vehicle.get("registration_number", "")
    car_identity = vehicle.get("brand", "")
    model = vehicle.get("model", "")
    if model:
        car_identity = f"{car_identity} {model}"

    owner_info, photo_url, carsitter_id = get_owner_info(vehicle_id, plate)

    # Référence basée sur le véhicule (propriétaire) et non sur l'agence
    owner_code = get_owner_code(vehicle_id, plate)
    effective_reference = build_effective_reference(reference, owner_code)
    if owner_code and effective_reference != reference:
        effective_reference = make_unique_reference(effective_reference, load_reference_map())
        print(f"🔁 {reference} -> {effective_reference} (véhicule {plate}, propriétaire {owner_code})")
    elif not owner_code:
        print(f"⚠️ Pas d'owner_code pour le véhicule {plate} (id {vehicle_id}). "
              f"Référence inchangée : {reference}.")

    # Dates & montants
    start_date = booking.get("start_date")
    end_date   = booking.get("end_date")
    days_number = int(booking.get("nb_days", 0))

    total_amount_full = float(booking.get("total_amount", 0))
    payment_balance   = float(booking.get("payment_balance") or total_amount_full)
    total_price       = payment_balance

    cartage_price_full = days_number * 5
    if payment_balance < total_amount_full and total_amount_full > 0:
        cartage_price = round(payment_balance * cartage_price_full / total_amount_full)
        print(f"📅 Prolongation détectée : balance={payment_balance:.2f}€ / total={total_amount_full:.2f}€ → CARTAGE prorata={cartage_price:.2f}€")
    else:
        cartage_price = cartage_price_full
        print(f"🆕 Premier paiement : total={total_amount_full:.2f}€ → CARTAGE={cartage_price:.2f}€")

    base_price = total_price - cartage_price

    # Carsitter
    if carsitter_id:
        print(f"🚘 Carsitter détecté ({carsitter_id}) → commission 30%")
        print(f"   → Plateforme garde : {base_price * 0.30:.2f}€ + {cartage_price:.2f}€ CARTAGE")
        print(f"   → Propriétaire reçoit : {base_price * 0.70:.2f}€")
        print(f"   → Carsitter reçoit (transfert manuel) : {base_price * 0.25:.2f}€")
    else:
        print(f"🏠 Pas de carsitter → commission 5%")
        print(f"   → Plateforme garde : {base_price * 0.05:.2f}€ + {cartage_price:.2f}€ CARTAGE")
        print(f"   → Propriétaire reçoit : {base_price * 0.95:.2f}€")

    # Génération du lien
    payment_link = create_payment_link(effective_reference, total_price, 1, cartage_price, carsitter_id)
    if not payment_link:
        print("❌ Échec de la génération du lien de paiement.")
        return

    print(f"✅ Lien de paiement généré : {payment_link}")

    # Envoi email
    success = send_payment_link_auto(
        email=email,
        client_name=client_name,
        product_name=effective_reference,
        payment_link=payment_link,
        product_price=total_price,
        car_identity=car_identity,
        days_number=days_number,
        cartage_price=cartage_price,
        owner_info=owner_info,
        start_date=start_date,
        end_date=end_date,
        photo_url=photo_url,
    )

    if success:
        sent_refs.add(reference)
        save_sent_references(sent_refs)
        ref_map = load_reference_map()
        ref_map[effective_reference] = {
            "fleetee_reference": reference,
            "booking_id": booking.get("id"),
            "vehicle_id": vehicle_id,
        }
        save_reference_map(ref_map)
        print(f"📧 Email envoyé à {email} ({client_name})")
    else:
        print(f"❌ Échec de l'envoi de l'email à {email}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage : python process_one.py <REFERENCE>")
        print("Exemple : python process_one.py LOC-P0002-2026-04-0006")
        sys.exit(1)

    ref = sys.argv[1]
    process_single(ref, force="--force" in sys.argv)
