import stripe
import requests
from decouple import config
import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import time
import json
import os

# 🔹 Logger configuration
logger = logging.getLogger("ConsoleLogger")
logger.setLevel(logging.DEBUG)
file_handler = logging.FileHandler("console_and_errors.log", encoding="utf-8")
console_handler = logging.StreamHandler()
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(message)s'))
console_handler.setFormatter(logging.Formatter('%(message)s'))
logger.addHandler(file_handler)
logger.addHandler(console_handler)
print = logger.info

# 🔹 API Keys
try:
    stripe.api_key = config('STRIPE_API_KEY')
    smtp_username = config('SMTP_USERNAME')
    smtp_password = config('SMTP_PASSWORD')
    fleetee_api_key = config('FLEETEE_API_KEY')
    fleetee_secret_key = config('FLEETEE_SECRET_KEY')
    connected_accounts_file = config('CONNECTED_ACCOUNTS_FILE')
except Exception:
    print("❌ Erreur de configuration : variables d'environnement manquantes.")
    exit(1)

# 🔹 Utilitaire pour requêtes API Fleetee
FLEETEE_API_BASE = "https://api.fleetee.io"

def fleetee_api_get(endpoint, params=None):
    headers = {
        "x-api-key": fleetee_api_key,
        "secret-key": fleetee_secret_key
    }
    url = f"{FLEETEE_API_BASE}{endpoint}"
    response = requests.get(url, headers=headers, params=params)
    # print(response.status_code)
    # print(response.text)
    if response.status_code == 200:
        return response.json()
    else:
        print(f"Erreur API Fleetee: {response.status_code} {response.text}")
        return None

# 🔹 Récupération des détails de réservation via API Fleetee
def fetch_reservation_details(reference):
    errors = []
    params = {"reference": reference}
    data, err = fleetee_api_get("/bookings", params=params)
    if err:
        errors.append(err)
        return None, errors
    if not data or not data.get('list'):
        errors.append(f"Référence introuvable : {reference}")
        return None, errors
    booking = data['list'][0]
    details = {
        "Référence": booking.get("reference"),
        "Statut": booking.get("status"),
        "Bien": booking.get("vehicle", {}).get("brand", ""),
        "Locataire": booking.get("customer", {}).get("name", ""),
        "Début": booking.get("start_date"),
        "Fin": booking.get("end_date"),
        "Durée": f"{int(booking.get('nb_days', 0))} jours",
        "Tarif": booking.get("total_amount"),
        "Balance": booking.get("payment_balance") or booking.get("total_amount"),
    }
    asset = booking.get("vehicle", {})
    if asset:
        details["Marque"] = asset.get("brand", "")
        details["Modèle"] = asset.get("category", {}).get("custom_subtitle", "")
        details["Plaque"] = asset.get("plate_number", "")
    owner = booking.get("company_name", "")
    if owner:
        details["Propriétaire"] = owner
    return details, errors

# 🔹 Initiales du vendeur
def get_seller_initials(reference):
    parts = reference.split('-')
    return parts[1] if len(parts) >= 2 else None

# 🔹 Création du produit Stripe
def create_product_on_connected_account(product_name, product_price, connected_account_id):
    try:
        return stripe.Product.create(
            name=product_name,
            default_price_data={'unit_amount': int(product_price * 100), 'currency': 'eur'},
            stripe_account=connected_account_id
        )['default_price']
    except Exception as e:
        print(f"Erreur Stripe : {e}")
        return None

def load_connected_accounts():
    with open(connected_accounts_file, 'r', encoding='utf-8') as f:
        accounts = json.load(f)
    # Filtrer les entrées réservées (pas de vrai compte Stripe)
    return {k: v for k, v in accounts.items() if v.get('id') != 'RESERVED'}

connected_accounts = load_connected_accounts()

# 🔹 Création du lien de paiement
#
# ⚠️  Le total_amount retourné par FLEETEE inclut DÉJÀ le montant CARTAGE.
#     On ne l'ajoute donc PAS au prix du lien — on le soustrait pour obtenir
#     le prix de réservation pur, sur lequel on applique la commission.
#
# Répartition :
#   Sans carsitter → application_fee = 5% × prix_resa + CARTAGE
#                    propriétaire reçoit 95% du prix_resa
#   Avec carsitter → application_fee = 30% × prix_resa + CARTAGE
#                    propriétaire reçoit 70% du prix_resa
#                    (les 25% du carsitter sont reversés manuellement depuis la plateforme)
#
def create_payment_link(reference, total_price, quantity, cartage_price, carsitter_id=None):
    """Retourne (url, payment_link_id). L'id permet de désactiver le lien
    plus tard si le tarif change avant paiement."""
    initials = get_seller_initials(reference)
    account = connected_accounts.get(initials)
    if not account:
        print(f"Compte connecté non trouvé pour {initials}")
        return None, None
    account_id = account["id"]

    # Prix de réservation pur (hors CARTAGE)
    base_price = total_price - cartage_price

    # Taux de commission selon présence d'un carsitter
    commission_rate = 0.30 if carsitter_id else 0.05

    # application_fee = commission sur le prix resa + montant CARTAGE intégral
    # → CARTAGE revient à la plateforme qui le reverse à CARTAGE séparément
    application_fee = int(base_price * quantity * commission_rate * 100) + int(cartage_price * 100)

    print(f"💶 total_price={total_price:.2f}€ | base_price={base_price:.2f}€ | "
          f"cartage={cartage_price:.2f}€ | commission={commission_rate*100:.0f}% | "
          f"application_fee={application_fee/100:.2f}€ | "
          f"proprio reçoit={(total_price - application_fee/100):.2f}€ | "
          f"carsitter reçoit={(application_fee/100 * 0.25):.2f}€")

    # Le lien de paiement = total_amount FLEETEE (inclut déjà CARTAGE)
    price_id = create_product_on_connected_account(reference, total_price, account_id)
    try:
        link = stripe.PaymentLink.create(
            line_items=[{'price': price_id, 'quantity': quantity}],
            application_fee_amount=application_fee,
            # 🔒 USAGE UNIQUE : Stripe désactive le lien automatiquement après
            # le 1er paiement complété → plus de double paiement possible,
            # même si Phase_8 est à l'arrêt à ce moment-là.
            restrictions={"completed_sessions": {"limit": 1}},
            stripe_account=account_id
        )
        return link.url, link.id
    except Exception as e:
        # Repli : si la version de l'API Stripe ne connaît pas 'restrictions',
        # on crée le lien sans (Phase_8 le désactivera après paiement).
        if "restrictions" in str(e):
            print("ℹ️ Paramètre 'restrictions' non supporté par cette version Stripe — "
                  "lien créé sans usage unique (désactivation gérée par Phase_8).")
            try:
                link = stripe.PaymentLink.create(
                    line_items=[{'price': price_id, 'quantity': quantity}],
                    application_fee_amount=application_fee,
                    stripe_account=account_id
                )
                return link.url, link.id
            except Exception as e2:
                print(f"Erreur Stripe : {e2}")
                return None, None
        print(f"Erreur Stripe : {e}")
        return None, None

def deactivate_payment_link(link_id, effective_reference):
    """Désactive un lien de paiement Stripe devenu obsolète (changement de
    tarif ou remplacement). Sans effet si le lien a déjà été utilisé."""
    if not link_id:
        return False
    initials = get_seller_initials(effective_reference)
    account = connected_accounts.get(initials)
    if not account:
        return False
    try:
        stripe.PaymentLink.modify(link_id, active=False, stripe_account=account["id"])
        print(f"🔒 Ancien lien de paiement désactivé ({effective_reference})")
        return True
    except Exception as e:
        print(f"⚠️ Désactivation du lien {effective_reference} impossible : {e}")
        return False

# 🔹 Récupération des infos client via API Fleetee
def get_tenant_info(tenant_name):
    errors = []
    tenant_name_parts = tenant_name.split(" ")
    if len(tenant_name_parts) < 2:
        errors.append(f"Format du nom complet invalide : {tenant_name}")
        return None, errors
    first_name = tenant_name_parts[0]
    last_name = " ".join(tenant_name_parts[1:])
    params = {"firstName": first_name, "lastName": last_name}
    data, err = fleetee_api_get("/customers", params=params)
    if err:
        errors.append(err)
        return None, errors
    if not data or not data.get('results'):
        errors.append(f"Aucune correspondance trouvée pour : {first_name} {last_name}")
        return None, errors
    customer = data['results'][0]
    tenant_info = {
        "prenom": customer.get("firstName", ""),
        "nom": customer.get("lastName", ""),
        "email": customer.get("email", ""),
        "date_de_naissance": customer.get("birthDate", ""),
        "telephone": customer.get("phone", "")
    }
    return tenant_info, errors

def send_payment_link_auto(email, client_name, product_name, payment_link, product_price, car_identity, days_number, cartage_price, owner_info, photo_url, start_date, end_date):
    smtp_server = 'smtp.office365.com'
    smtp_port = 587
    msg = MIMEMultipart()
    msg['From'] = smtp_username
    msg['To'] = email
    msg['Subject'] = f"Malovelycar - {product_name} : Votre lien de paiement"
    body = f"""
    <html>
    <body>
        <p><img src="https://raw.githubusercontent.com/Lamine-admin/Thecarsociety_images/refs/heads/main/Background.JPG" alt="Background" style="width:800px;height:auto;"></p>
        <p>Bonjour <b>{client_name}</b>,</p>
        <p>Merci d'avoir réservé un véhicule sur notre site <a href="https://www.malovelycar.com" target="_blank"><b>Malovelycar</b></a> !</p>
        <p>Votre réservation est maintenant <b>en attente de paiement</b>.</p>
        <p>Détails de la réservation  {product_name} :</p>
        <p><img src="{photo_url}" alt="Photo du véhicule" style="width:400px;height:auto;"></p>
        <ul>
            <li><b>Véhicule :</b> {car_identity}</li>
            <li><b>Propriétaire :</b> {owner_info}</li>
            <li><b>Début :</b> {start_date}</li>
            <li><b>Fin :</b> {end_date}</li>
            <li><b>Durée :</b> {days_number} jours</li>
            <li><b>Tarif :</b> {product_price:.2f} €</li>
        </ul>
        <p>Cliquez sur le bouton ci-dessous pour régler votre réservation :</p>
        <p><a href="{payment_link}"><img src="https://raw.githubusercontent.com/Lamine-admin/Thecarsociety_images/main/Payer_reservation_logo_new.png" alt="Lien de paiement" style="width:200px;height:auto;"></a></p>
        <p>Si vous avez des difficultés à accéder à la page de paiement, rendez-vous sur notre page de paiement en cliquant sur <a href="https://malovelycar-payment-page.streamlit.app/" target="_blank">"Payer ma réservation"</a>.</p>
        <p>Pour toute question, contactez-nous au +33 7 53 50 82 27 ou à contact@malovelycar.com</p>
        <p>Cordialement,</p>
        <p>L'équipe Malovelycar</p>
        <p><img src="https://raw.githubusercontent.com/Lamine-admin/Thecarsociety_images/refs/heads/main/Logos_MLC_TLC_combin%C3%A9s_2.PNG" alt="Logo Malovelycar" style="width:150px;height:auto;"></p>
    </body>
    </html>
    """
    msg.attach(MIMEText(body, 'html'))
    try:
        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
        server.login(smtp_username, smtp_password)
        server.sendmail(smtp_username, email, msg.as_string())
        server.quit()
        print(f"Email envoyé à {email}")
        return True
    except Exception as e:
        print(f"Erreur lors de l'envoi du mail : {e}")
        return False

SENT_FILE = "sent_payments.json"

def load_sent_references():
    try:
        with open(SENT_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()

def save_sent_references(sent_refs):
    with open(SENT_FILE, "w", encoding="utf-8") as f:
        json.dump(list(sent_refs), f)

def load_vehicle_owners():
    with open("vehicules_proprietaires.json", "r", encoding="utf-8") as f:
        return json.load(f)

vehicle_owners = load_vehicle_owners()

def get_owner_info(vehicle_id, plate):
    for v in vehicle_owners:
        if v["vehicle_id"] == vehicle_id or v["plate"] == plate:
            return v["owner_name"], v.get("photo_url", ""), v.get("carsitter_id")
    return "Propriétaire inconnu", "", None

def get_owner_code(vehicle_id, plate):
    """Code propriétaire (ex: 'P0002') du véhicule, ou None si non renseigné
    dans vehicules_proprietaires.json (champ owner_code)."""
    for v in vehicle_owners:
        if v["vehicle_id"] == vehicle_id or v["plate"] == plate:
            code = (v.get("owner_code") or "").strip()
            return code or None
    return None

def build_effective_reference(reference, owner_code):
    """Remplace le 2e segment (code agence) de la référence par le code
    propriétaire, pour router selon le véhicule et non selon l'agence.
    Ex: LOC-C04-2026-06-0001 + P0002 -> LOC-P0002-2026-06-0001.
    Si owner_code est vide/None, renvoie la référence inchangée (fallback)."""
    if not owner_code or not reference:
        return reference
    parts = reference.split('-')
    if len(parts) >= 2:
        parts[1] = owner_code
        return '-'.join(parts)
    return reference

def increment_last_segment(reference):
    """Incrémente de 1 le dernier segment numérique de la référence, en
    conservant le zéro-padding. Ex: LOC-P0002-2026-06-0001 -> ...-0002."""
    parts = reference.split('-')
    last = parts[-1]
    try:
        num = int(last)
    except (ValueError, IndexError):
        return reference
    parts[-1] = str(num + 1).zfill(len(last))
    return '-'.join(parts)

def reference_exists(candidate, ref_map=None):
    """True si la référence existe déjà : soit dans Fleetee (correspondance
    exacte), soit déjà attribuée par nous dans reference_map.json."""
    if ref_map and candidate in ref_map:
        return True
    data = fleetee_api_get("/bookings", params={"reference": candidate})
    if data and data.get("list"):
        for b in data["list"]:
            if b.get("reference") == candidate:
                return True
    return False

def make_unique_reference(reference, ref_map=None, max_tries=1000):
    """Garantit l'unicité : si 'reference' existe déjà (dans Fleetee ou déjà
    attribuée), incrémente le dernier segment jusqu'à trouver une référence
    libre. Ex: LOC-P0002-2026-06-0001 -> LOC-P0002-2026-06-0002 si le premier
    est déjà pris."""
    candidate = reference
    tries = 0
    while reference_exists(candidate, ref_map):
        nxt = increment_last_segment(candidate)
        if nxt == candidate:
            print(f"⚠️ Impossible d'incrémenter la référence {candidate}.")
            break
        print(f"♻️ Référence {candidate} déjà existante → essai {nxt}")
        candidate = nxt
        tries += 1
        if tries >= max_tries:
            print(f"⚠️ Limite d'incréments atteinte pour {reference}.")
            break
    return candidate

# Mapping : référence effective (LOC-Pxxxx-...) -> référence Fleetee d'origine
# + booking_id. Permet à Phase_8 de retrouver la location dans Fleetee à partir
# du nom de produit Stripe (qui porte la référence effective).
REFERENCE_MAP_FILE = "reference_map.json"

def load_reference_map():
    try:
        with open(REFERENCE_MAP_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_reference_map(m):
    with open(REFERENCE_MAP_FILE, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=2)

# ---------------------------------------------------------------------------
# 🔹 MODIFICATION TARIFAIRE AVANT PAIEMENT
#
# Cas : la demande est acceptée (100 €), le lien est envoyé, puis le tarif
# change (ex. option livraison +20 € → 120 €) AVANT que le client ne paie.
# À chaque passage, on compare le montant du lien envoyé (amount_billed,
# mémorisé dans reference_map.json) au montant dû actuel : s'ils diffèrent,
# on DÉSACTIVE l'ancien lien Stripe (pour interdire un paiement au mauvais
# montant), on génère un nouveau lien et on prévient le client.
# ---------------------------------------------------------------------------
PRICE_CHANGE_TOLERANCE = 0.50   # écart minimal (€) considéré comme un vrai changement

def find_pending_entry(reference, ref_map):
    """Entrée reference_map du lien INITIAL (hors prolongations) d'une
    réservation Fleetee. Retourne (référence_effective, entrée) ou (None, None)."""
    for k, v in ref_map.items():
        if isinstance(v, dict) and v.get("fleetee_reference") == reference \
                and not v.get("is_extension"):
            return k, v
    return None, None

def send_updated_payment_link(email, client_name, product_name, payment_link,
                              old_price, new_price, car_identity, start_date,
                              end_date, photo_url):
    smtp_server = 'smtp.office365.com'
    smtp_port = 587
    msg = MIMEMultipart()
    msg['From'] = smtp_username
    msg['To'] = email
    msg['Subject'] = f"Malovelycar - {product_name} : Réservation mise à jour — nouveau lien de paiement"
    body = f"""
    <html>
    <body>
        <p><img src="https://raw.githubusercontent.com/Lamine-admin/Thecarsociety_images/refs/heads/main/Background.JPG" alt="Background" style="width:800px;height:auto;"></p>
        <p>Bonjour <b>{client_name}</b>,</p>
        <p>Votre réservation <b>{product_name}</b> a été <b>mise à jour</b> et son montant a changé :</p>
        <p><img src="{photo_url}" alt="Photo du véhicule" style="width:400px;height:auto;"></p>
        <ul>
            <li><b>Véhicule :</b> {car_identity}</li>
            <li><b>Début :</b> {start_date}</li>
            <li><b>Fin :</b> {end_date}</li>
            <li><b>Ancien montant :</b> <s>{old_price:.2f} €</s></li>
            <li><b>Nouveau montant :</b> {new_price:.2f} €</li>
        </ul>
        <p>⚠️ <b>Le précédent lien de paiement n'est plus valide.</b> Merci d'utiliser ce nouveau lien :</p>
        <p><a href="{payment_link}"><img src="https://raw.githubusercontent.com/Lamine-admin/Thecarsociety_images/main/Payer_reservation_logo_new.png" alt="Lien de paiement" style="width:200px;height:auto;"></a></p>
        <p>Si vous avez des difficultés à accéder à la page de paiement, rendez-vous sur notre page de paiement en cliquant sur <a href="https://malovelycar-payment-page.streamlit.app/" target="_blank">"Payer ma réservation"</a>.</p>
        <p>Pour toute question, contactez-nous au +33 7 53 50 82 27 ou à contact@malovelycar.com</p>
        <p>Cordialement,</p>
        <p>L'équipe Malovelycar</p>
        <p><img src="https://raw.githubusercontent.com/Lamine-admin/Thecarsociety_images/refs/heads/main/Logos_MLC_TLC_combin%C3%A9s_2.PNG" alt="Logo Malovelycar" style="width:150px;height:auto;"></p>
    </body>
    </html>
    """
    msg.attach(MIMEText(body, 'html'))
    try:
        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
        server.login(smtp_username, smtp_password)
        server.sendmail(smtp_username, email, msg.as_string())
        server.quit()
        print(f"📧 Nouveau lien (tarif mis à jour) envoyé à {email} ({product_name})")
        return True
    except Exception as e:
        print(f"❌ Erreur envoi mail tarif mis à jour : {e}")
        return False

def handle_price_update(booking, ref_map):
    """Détecte un changement de tarif sur une réservation en attente de
    paiement dont le lien a déjà été envoyé. Remplace le lien si besoin.
    Retourne True si reference_map a été modifié."""
    reference = booking.get("reference")
    effective_reference, entry = find_pending_entry(reference, ref_map)
    if not entry:
        return False

    total_amount_full = float(booking.get("total_amount", 0))
    payment_balance = float(booking.get("payment_balance") or total_amount_full)
    new_price = round(payment_balance, 2)

    old_price = entry.get("amount_billed")
    if old_price is None:
        # Lien envoyé avant l'introduction du suivi de montant : on mémorise
        # le montant actuel comme référence, sans renvoyer de lien.
        entry["amount_billed"] = new_price
        return True
    old_price = float(old_price)
    if abs(new_price - old_price) <= PRICE_CHANGE_TOLERANCE:
        return False  # pas de changement significatif

    print(f"💱 Tarif modifié pour {reference} : {old_price:.2f}€ → {new_price:.2f}€ "
          f"— remplacement du lien de paiement")

    # Recalcul CARTAGE / commission (mêmes règles que le flux principal)
    vehicle = booking.get("vehicle", {})
    vehicle_id = vehicle.get("id")
    plate = vehicle.get("registration_number", "")
    car_identity = " ".join(x for x in [vehicle.get("brand", ""), vehicle.get("model", "")] if x)
    owner_info, photo_url, carsitter_id = get_owner_info(vehicle_id, plate)
    days_number = int(booking.get("nb_days", 0))
    cartage_price_full = days_number * 5
    if payment_balance < total_amount_full and total_amount_full > 0:
        cartage_price = round(payment_balance * cartage_price_full / total_amount_full)
    else:
        cartage_price = cartage_price_full

    # 1) Désactiver l'ancien lien (interdit un paiement à l'ancien montant)
    deactivate_payment_link(entry.get("payment_link_id"), effective_reference)

    # 2) Nouveau lien au nouveau montant (même référence effective : Phase_8
    #    retrouvera la location à l'identique)
    payment_link, payment_link_id = create_payment_link(
        effective_reference, new_price, 1, cartage_price, carsitter_id)
    if not payment_link:
        # L'ancien lien est désactivé mais le nouveau a échoué : on ne met pas
        # à jour amount_billed pour retenter au prochain passage.
        print(f"⚠️ Nouveau lien non créé pour {reference}, nouvelle tentative au prochain passage.")
        return False

    # 3) Prévenir le client
    customer = booking.get("customer") or {}
    if send_updated_payment_link(
        email=customer.get("email", ""),
        client_name=customer.get("name", ""),
        product_name=effective_reference,
        payment_link=payment_link,
        old_price=old_price,
        new_price=new_price,
        car_identity=car_identity,
        start_date=booking.get("start_date"),
        end_date=booking.get("end_date"),
        photo_url=photo_url,
    ):
        entry["amount_billed"] = new_price
        entry["payment_link_id"] = payment_link_id
        return True
    return False

def process_pending_payments():
    sent_refs = load_sent_references()
    ref_map = load_reference_map()
    params = {"status": "accepted"}
    data = fleetee_api_get("/bookings", params=params)
    if not data or not data.get('list'):
        print("Aucune réservation en attente de paiement.")
        return
    updated = False
    for booking in data['list']:
        if booking.get("status") != "accepted" or booking.get("status_code") != 20:
            continue
        reference = booking.get("reference")
        if reference in sent_refs:
            # Mail déjà envoyé → vérifier si le tarif a changé depuis
            # (option ajoutée, remise…) et remplacer le lien si besoin.
            if handle_price_update(booking, ref_map):
                updated = True
            continue
        client_name = booking.get("customer", {}).get("name", "")
        email = booking.get("customer", {}).get("email", "")
        vehicle = booking.get("vehicle", {})
        vehicle_id = vehicle.get("id")
        plate = vehicle.get("registration_number", "")
        car_identity = vehicle.get("brand", "")
        model = vehicle.get("model", "")
        if model:
            car_identity = f"{car_identity} {model}"
        owner_info, photo_url, carsitter_id = get_owner_info(vehicle_id, plate)
        # Référence basée sur le véhicule (propriétaire) et non sur l'agence
        owner_code = get_owner_code(vehicle_id, plate)
        effective_reference = build_effective_reference(reference, owner_code)
        if owner_code and effective_reference != reference:
            # Garantir l'unicité : pas de doublon avec une réf déjà sur Fleetee
            effective_reference = make_unique_reference(effective_reference, ref_map)
            print(f"🔁 {reference} -> {effective_reference} (véhicule {plate}, propriétaire {owner_code})")
        elif not owner_code:
            print(f"⚠️ Pas d'owner_code pour le véhicule {plate} (id {vehicle_id}). "
                  f"Référence inchangée : {reference}. Renseigne owner_code dans vehicules_proprietaires.json.")
        start_date = booking.get("start_date")
        end_date = booking.get("end_date")
        days_number = int(booking.get("nb_days", 0))
        total_amount_full = float(booking.get("total_amount", 0))
        payment_balance = float(booking.get("payment_balance") or total_amount_full)
        total_price = payment_balance  # montant restant à payer (prolongation ou premier paiement)
        cartage_price_full = days_number * 5
        # Si prolongation (balance < total), CARTAGE au prorata du montant restant
        if payment_balance < total_amount_full and total_amount_full > 0:
            cartage_price = round(payment_balance * cartage_price_full / total_amount_full)
            print(f"📅 Prolongation détectée : balance={payment_balance:.2f}€ / total={total_amount_full:.2f}€ → cartage prorata={cartage_price:.2f}€")
        else:
            cartage_price = cartage_price_full
        base_price = total_price - cartage_price              # prix réservation pur
        if carsitter_id:
            print(f"🚘 Carsitter détecté pour {reference} : {carsitter_id} → commission 25% pour carsitter +5% pour plateforme")
        else:
            print(f"🏠 Pas de carsitter pour {reference} → commission 0% pour carsitter +5% pour plateforme")
        payment_link, payment_link_id = create_payment_link(effective_reference, total_price, 1, cartage_price, carsitter_id)
        if payment_link:
            if send_payment_link_auto(
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
                photo_url=photo_url
            ):
                sent_refs.add(reference)
                # Trace effective -> Fleetee (réf d'origine + booking_id) pour Phase_8.
                # amount_billed + payment_link_id : nécessaires pour détecter un
                # changement de tarif et désactiver l'ancien lien.
                ref_map[effective_reference] = {
                    "fleetee_reference": reference,
                    "booking_id": booking.get("id"),
                    "vehicle_id": vehicle_id,
                    "amount_billed": round(total_price, 2),
                    "payment_link_id": payment_link_id,
                }
                updated = True
    if updated:
        save_sent_references(sent_refs)
        save_reference_map(ref_map)

# ---------------------------------------------------------------------------
# 🔹 PROLONGATIONS DE LOCATION
#
# Quand des jours sont ajoutés à une location déjà payée (statut "confirm" ou
# "check-out"), Fleetee augmente le total_amount : la différence avec le montant
# déjà encaissé (paid_amount) = le montant de la prolongation à facturer.
#
# Pour chaque location en cours/confirmée avec une balance > 0 :
#   1. Création d'une référence de prolongation dédiée : LOC-…-EXT1 (puis EXT2…)
#   2. Lien de paiement Stripe du montant de la balance (CARTAGE au prorata,
#      même répartition de commission que le flux principal)
#   3. Email spécifique "prolongation" au client
#   4. Marquage is_extension dans reference_map.json → Phase_8 enregistrera le
#      paiement SANS modifier le statut de la location.
#
# Déduplication : clé "référence|EXT|date_fin|montant" dans sent_payments.json.
# Une nouvelle prolongation (nouvelle date de fin / nouveau montant) génère donc
# bien un nouveau lien, mais jamais deux liens pour la même prolongation.
# ---------------------------------------------------------------------------
EXTENSION_STATUSES = ("confirm", "check-out")
EXTENSION_MIN_BALANCE = 0.50   # tolérance centimes / arrondis Fleetee

def compute_paid_amount(detail):
    """Montant réellement encaissé : somme des paiements de type payment /
    down_payment (la caution n'est pas un encaissement), avec repli sur
    paid_amount si le détail ne liste pas les paiements."""
    payments = detail.get("payments") or []
    paid, counted = 0.0, False
    for p in payments:
        if (p.get("payment_type") or "") in ("payment", "down_payment"):
            amt = p.get("paid_amount")
            if amt is None:
                amt = p.get("amount") or 0
            paid += float(amt)
            counted = True
    if not counted:
        paid = float(detail.get("paid_amount") or 0)
    return round(paid, 2)

def find_effective_base_reference(reference, ref_map, vehicle_id, plate):
    """Référence effective (LOC-Pxxxx-…) de la location : celle déjà attribuée
    par le flux principal si présente dans reference_map, sinon reconstruite
    depuis le code propriétaire."""
    for k, v in ref_map.items():
        if isinstance(v, dict) and v.get("fleetee_reference") == reference \
                and not v.get("is_extension"):
            return k
    owner_code = get_owner_code(vehicle_id, plate)
    return build_effective_reference(reference, owner_code)

def build_extension_reference(effective_reference, ref_map):
    """LOC-…-EXT1, LOC-…-EXT2, … (le préfixe LOC- est conservé : Phase_8
    lit le nom du produit Stripe et exige ce préfixe)."""
    n = 1
    while f"{effective_reference}-EXT{n}" in ref_map:
        n += 1
    return f"{effective_reference}-EXT{n}"

def send_extension_payment_link(email, client_name, product_name, payment_link,
                                amount, extra_days, car_identity, end_date,
                                photo_url):
    smtp_server = 'smtp.office365.com'
    smtp_port = 587
    msg = MIMEMultipart()
    msg['From'] = smtp_username
    msg['To'] = email
    msg['Subject'] = f"Malovelycar - {product_name} : Prolongation de votre location"
    jours_txt = f"environ {extra_days} jour{'s' if extra_days > 1 else ''}" if extra_days else "la période ajoutée"
    body = f"""
    <html>
    <body>
        <p><img src="https://raw.githubusercontent.com/Lamine-admin/Thecarsociety_images/refs/heads/main/Background.JPG" alt="Background" style="width:800px;height:auto;"></p>
        <p>Bonjour <b>{client_name}</b>,</p>
        <p>Votre demande de <b>prolongation de location</b> a bien été prise en compte 🎉</p>
        <p><img src="{photo_url}" alt="Photo du véhicule" style="width:400px;height:auto;"></p>
        <ul>
            <li><b>Véhicule :</b> {car_identity}</li>
            <li><b>Prolongation :</b> {jours_txt}</li>
            <li><b>Nouvelle date de retour :</b> {end_date}</li>
            <li><b>Montant complémentaire :</b> {amount:.2f} €</li>
        </ul>
        <p>Pour valider définitivement la prolongation, merci de régler ce complément :</p>
        <p><a href="{payment_link}"><img src="https://raw.githubusercontent.com/Lamine-admin/Thecarsociety_images/main/Payer_reservation_logo_new.png" alt="Lien de paiement" style="width:200px;height:auto;"></a></p>
        <p>⚠️ Sans règlement avant la date de retour initiale, la prolongation ne pourra pas être garantie.</p>
        <p>Pour toute question, contactez-nous au +33 7 53 50 82 27 ou à contact@malovelycar.com</p>
        <p>Cordialement,</p>
        <p>L'équipe Malovelycar</p>
        <p><img src="https://raw.githubusercontent.com/Lamine-admin/Thecarsociety_images/refs/heads/main/Logos_MLC_TLC_combin%C3%A9s_2.PNG" alt="Logo Malovelycar" style="width:150px;height:auto;"></p>
    </body>
    </html>
    """
    msg.attach(MIMEText(body, 'html'))
    try:
        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
        server.login(smtp_username, smtp_password)
        server.sendmail(smtp_username, email, msg.as_string())
        server.quit()
        print(f"📧 Lien de prolongation envoyé à {email} ({product_name})")
        return True
    except Exception as e:
        print(f"❌ Erreur envoi mail prolongation : {e}")
        return False

def process_extensions():
    """Détecte les locations confirmées/en cours dont le total a augmenté
    (jours ajoutés) et envoie le lien de paiement du complément."""
    sent_refs = load_sent_references()
    ref_map = load_reference_map()
    updated = False
    today = time.strftime("%Y-%m-%d 00:00")

    for st in EXTENSION_STATUSES:
        data = fleetee_api_get("/bookings", params={"status": st, "min_end_date": today})
        for mini in (data or {}).get("list") or []:
            booking_id = mini.get("id")
            reference = mini.get("reference")
            if not booking_id or not reference:
                continue
            # Détail complet : paid_amount / payments ne sont pas dans la liste
            detail = fleetee_api_get(f"/bookings/{booking_id}")
            if not detail:
                continue
            total = float(detail.get("total_amount") or 0)
            paid = compute_paid_amount(detail)
            balance = round(total - paid, 2)
            if balance <= EXTENSION_MIN_BALANCE:
                continue  # rien à facturer
            if paid <= 0:
                # Jamais payée : c'est le flux principal qui gère, pas une prolongation
                continue

            end_date = detail.get("end_date", "")
            dedup_key = f"{reference}|EXT|{end_date}|{balance:.2f}"
            if dedup_key in sent_refs:
                continue  # lien déjà envoyé pour cette prolongation précise

            customer = detail.get("customer") or {}
            client_name = customer.get("name", "")
            email = customer.get("email", "")
            vehicle = detail.get("vehicle") or {}
            vehicle_id = vehicle.get("id")
            plate = vehicle.get("registration_number", "")
            car_identity = " ".join(x for x in [vehicle.get("brand", ""), vehicle.get("model", "")] if x)
            owner_info, photo_url, carsitter_id = get_owner_info(vehicle_id, plate)

            nb_days = int(detail.get("nb_days") or 0)
            # Estimation des jours ajoutés au tarif journalier moyen
            extra_days = int(round(balance / (total / nb_days))) if (total > 0 and nb_days > 0) else 0
            # CARTAGE au prorata du complément (même logique que le flux principal)
            cartage_full = nb_days * 5
            cartage_ext = round(balance * cartage_full / total) if total > 0 else 0

            effective_base = find_effective_base_reference(reference, ref_map, vehicle_id, plate)
            ext_reference = build_extension_reference(effective_base, ref_map)

            print(f"📅 Prolongation détectée : {reference} (statut {st}) — total={total:.2f}€ "
                  f"payé={paid:.2f}€ → complément={balance:.2f}€ (~{extra_days} j) "
                  f"→ lien {ext_reference}")

            # Désactiver les liens des prolongations précédentes de cette
            # location : s'ils n'ont pas été payés, leur montant est devenu
            # faux (la nouvelle balance englobe tout le restant dû). S'ils
            # ont été payés, la désactivation est sans effet.
            for k, v in ref_map.items():
                if isinstance(v, dict) and v.get("is_extension") \
                        and v.get("booking_id") == booking_id \
                        and v.get("payment_link_id"):
                    deactivate_payment_link(v["payment_link_id"], k)
                    v["payment_link_id"] = None
                    updated = True

            payment_link, payment_link_id = create_payment_link(ext_reference, balance, 1, cartage_ext, carsitter_id)
            if not payment_link:
                continue
            if send_extension_payment_link(
                email=email,
                client_name=client_name,
                product_name=ext_reference,
                payment_link=payment_link,
                amount=balance,
                extra_days=extra_days,
                car_identity=car_identity,
                end_date=end_date,
                photo_url=photo_url,
            ):
                sent_refs.add(dedup_key)
                ref_map[ext_reference] = {
                    "fleetee_reference": reference,
                    "booking_id": booking_id,
                    "vehicle_id": vehicle_id,
                    "is_extension": True,
                    "extension_balance": balance,
                    "extension_end_date": end_date,
                    "amount_billed": balance,
                    "payment_link_id": payment_link_id,
                }
                updated = True

    if updated:
        save_sent_references(sent_refs)
        save_reference_map(ref_map)

if __name__ == "__main__":
    print("⏳ Script d'envoi automatique des liens de paiement en cours d'exécution...")
    while True:
        process_pending_payments()
        process_extensions()
        print("🔄 Script relancé, prochaine vérification dans 1 minute.")
        # Attends 1 minute avant de relancer (modifiable selon besoin)
        time.sleep(60)
