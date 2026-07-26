"""
Phase_8.py — Confirmation automatique des locations après paiement.

Chaîne automatisée (suite de Phase_7) :
    1. Détecte les paiements Stripe réussis sur les comptes connectés (polling).
    2. Retrouve la réservation Fleetee correspondante via sa référence.
    3. Enregistre le paiement sur Fleetee   -> POST /bookings/{id}/payments
    4. Passe le statut "en attente de paiement" -> "confirmé"
                                             -> PUT  /bookings/{id}/status {"status":"confirm"}
    5. Envoie le reçu de paiement au client (receipt_url renvoyé par Fleetee).

Le mapping paiement -> réservation repose sur le fait que Phase_7 crée, pour
chaque lien de paiement, un produit Stripe dont le NOM est la référence de la
location (ex: "LOC-P0001-2026-04-0006"). On lit donc le nom du produit de la
session payée pour retrouver la référence.

Dédup : chaque session Stripe traitée est mémorisée dans confirmed_payments.json
afin de ne jamais enregistrer deux fois le même paiement.

Lancement :
    python Phase_8.py            # boucle continue (polling toutes les 60 s)
    python Phase_8.py --once     # un seul passage puis sortie
    python Phase_8.py --dry-run  # simulation : aucun appel d'écriture (Fleetee/email)
"""

import sys
import json
import time
import os
import tempfile
from datetime import datetime, timedelta, timezone

import stripe
import requests

# On réutilise toute la configuration et les helpers déjà éprouvés de Phase_7
# (clés API, SMTP, comptes connectés, logger). L'import n'exécute PAS la boucle
# while de Phase_7 (elle est sous `if __name__ == "__main__"`).
from Phase_7 import (
    stripe as _stripe_configured,   # garantit stripe.api_key déjà positionné
    fleetee_api_key,
    fleetee_secret_key,
    FLEETEE_API_BASE,
    smtp_username,
    smtp_password,
    load_connected_accounts,
    load_reference_map,
    get_owner_info,   # véhicule -> (propriétaire, photo, carsitter_id)
    print,  # logger configuré (console + fichier)
)

# ---------------------------------------------------------------------------
# Paramètres
# ---------------------------------------------------------------------------
CONFIRMED_FILE = "confirmed_payments.json"   # sessions Stripe déjà traitées
LOOKBACK_DAYS = 7         # fenêtre de scan des sessions Stripe (évite tout l'historique)
POLL_INTERVAL = 60        # secondes entre deux passages en mode boucle

# "En attente de paiement" tel que défini dans Phase_7 :
#   status == "accepted"  ET  status_code == 20
# Phase_8 n'agit QUE dans ce cas. Tout autre statut (expired, cancel, refused,
# confirm, waiting, booked...) est ignoré.
AWAITING_PAYMENT_STATUS = "accepted"
AWAITING_PAYMENT_CODE = 20

DRY_RUN = "--dry-run" in sys.argv

CARSITTERS_FILE = "carsitters.json"   # carsitter_id (compte Stripe) -> coordonnées

# Sessions déjà ignorées (statut non éligible) — pour ne pas réafficher le même
# log à chaque passage. En mémoire uniquement : si la location repasse en
# "en attente de paiement" plus tard, elle sera réévaluée au prochain run.
_skip_logged = set()


def load_carsitters():
    """carsitter_id (compte Stripe) -> {code, name, email, phone}."""
    try:
        with open(CARSITTERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        print(f"⚠️ {CARSITTERS_FILE} introuvable/illisible : "
              f"pas de notification carsitter possible.")
        return {}


# ---------------------------------------------------------------------------
# Helpers API Fleetee (GET déjà dans Phase_7 ; ici POST / PUT)
# ---------------------------------------------------------------------------
def _fleetee_headers():
    return {
        "x-api-key": fleetee_api_key,
        "secret-key": fleetee_secret_key,
        "Content-Type": "application/json",
    }


def fleetee_get(endpoint, params=None):
    url = f"{FLEETEE_API_BASE}{endpoint}"
    r = requests.get(url, headers=_fleetee_headers(), params=params, timeout=30)
    if r.status_code == 200:
        return r.json()
    print(f"❌ Fleetee GET {endpoint} : {r.status_code} {r.text}")
    return None


def fleetee_post(endpoint, payload):
    url = f"{FLEETEE_API_BASE}{endpoint}"
    r = requests.post(url, headers=_fleetee_headers(), json=payload, timeout=30)
    if r.status_code in (200, 201):
        return r.json()
    print(f"❌ Fleetee POST {endpoint} : {r.status_code} {r.text}")
    return None


def fleetee_put(endpoint, payload):
    url = f"{FLEETEE_API_BASE}{endpoint}"
    r = requests.put(url, headers=_fleetee_headers(), json=payload, timeout=30)
    if r.status_code in (200, 201):
        return r.json()
    print(f"❌ Fleetee PUT {endpoint} : {r.status_code} {r.text}")
    return None


def get_booking_by_reference(reference):
    """Retourne le booking 'mini' (liste) correspondant à une référence, ou None."""
    data = fleetee_get("/bookings", params={"reference": reference})
    if not data or not data.get("list"):
        return None
    # Match exact de la référence (l'API fait une recherche, on sécurise)
    for b in data["list"]:
        if b.get("reference") == reference:
            return b
    return data["list"][0]


# ---------------------------------------------------------------------------
# Dédup : sessions Stripe déjà confirmées
# ---------------------------------------------------------------------------
def load_confirmed_sessions():
    try:
        with open(CONFIRMED_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_confirmed_sessions(sessions):
    with open(CONFIRMED_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(sessions), f, indent=2)


# ---------------------------------------------------------------------------
# Stripe : récupération des sessions payées et de leur référence
# ---------------------------------------------------------------------------
def get_reference_from_session(session, account_id):
    """Lit le nom du produit de la session payée -> référence de la location."""
    try:
        line_items = stripe.checkout.Session.list_line_items(
            session.id,
            stripe_account=account_id,
            limit=10,
            expand=["data.price.product"],
        )
    except Exception as e:
        print(f"⚠️  Impossible de lire les line_items de la session {session.id} : {e}")
        return None

    for item in line_items.auto_paging_iter():
        price = getattr(item, "price", None)
        product = getattr(price, "product", None) if price else None
        name = None
        if product is not None:
            # product peut être un objet étendu ou un id
            name = getattr(product, "name", None) if not isinstance(product, str) else None
        if not name:
            name = getattr(item, "description", None)
        if name and name.startswith("LOC-"):
            return name.strip()
    return None


# Liens déjà désactivés pendant ce run (évite les appels répétés quand une
# session non éligible est réévaluée à chaque passage).
_deactivated_links = set()


def deactivate_session_payment_link(session, account_id, reference=""):
    """🔒 Anti double paiement : désactive le lien de paiement à l'origine
    d'une session payée. Un lien Stripe reste actif par défaut après
    paiement — des clients ont déjà payé deux fois la même location via le
    même lien. La session Checkout porte l'id du lien (champ payment_link).
    Complète la restriction 'usage unique' posée à la création par Phase_7
    (utile pour les liens créés avant cette mise à jour)."""
    link_id = session.get("payment_link")
    if not link_id or link_id in _deactivated_links:
        return
    if DRY_RUN:
        print(f"🧪 [DRY-RUN] Désactivation du lien {link_id} ({reference})")
        _deactivated_links.add(link_id)
        return
    try:
        stripe.PaymentLink.modify(link_id, active=False, stripe_account=account_id)
        _deactivated_links.add(link_id)
        print(f"🔒 Lien de paiement désactivé après paiement ({reference}).")
    except Exception as e:
        print(f"⚠️ Désactivation du lien {link_id} impossible ({reference}) : {e}")


def iter_paid_sessions(account_id):
    """Génère les sessions Checkout payées (récentes) d'un compte connecté."""
    since = int((datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).timestamp())
    try:
        sessions = stripe.checkout.Session.list(
            stripe_account=account_id,
            created={"gte": since},
            limit=100,
        )
    except Exception as e:
        print(f"⚠️  Stripe Session.list a échoué pour {account_id} : {e}")
        return

    for s in sessions.auto_paging_iter():
        if s.get("payment_status") == "paid":
            yield s


# ---------------------------------------------------------------------------
# Email : envoi du reçu de paiement au client
# ---------------------------------------------------------------------------
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders


def _normalize_url(url):
    """Fleetee renvoie parfois des URLs sans schéma (ex: //s3-eu-west-1...)."""
    if not url:
        return url
    url = url.strip()
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return "https://s3-eu-west-1.amazonaws.com" + url
    if not url.startswith(("http://", "https://")):
        return "https://" + url
    return url


def find_confirmation_document(booking):
    """URL du document de confirmation / contrat de location."""
    if not isinstance(booking, dict):
        return None
    for d in (booking.get("documents") or []):
        name = (d.get("name") or "").lower()
        if any(k in name for k in ("confirm", "réserv", "reserv", "contrat", "location")):
            if d.get("url"):
                return d["url"]
    return booking.get("contract_url")


def download_document(url, dest):
    """Télécharge un document Fleetee (essai public puis avec authentification)."""
    url = _normalize_url(url)
    if not url:
        return None
    base = {"User-Agent": "Mozilla/5.0"}
    for headers in (base, {**base, **_fleetee_headers()}):
        try:
            r = requests.get(url, headers=headers, timeout=30)
            if r.status_code == 200 and r.content:
                with open(dest, "wb") as f:
                    f.write(r.content)
                return dest
        except Exception:
            continue
    return None


def send_receipt_email(email, client_name, reference, amount, receipt_url,
                       car_identity="", start_date="", end_date="", contract_path=None,
                       is_extension=False):
    if not email:
        print(f"⚠️  Pas d'email client pour {reference}, reçu non envoyé.")
        return False

    smtp_server, smtp_port = "smtp.office365.com", 587
    msg = MIMEMultipart()
    msg["From"] = smtp_username
    msg["To"] = email
    if is_extension:
        msg["Subject"] = f"Malovelycar - {reference} : Prolongation confirmée & reçu de paiement"
    else:
        msg["Subject"] = f"Malovelycar - {reference} : Confirmation & reçu de paiement"

    # Fleetee renvoie parfois une URL sans schéma (ex: //s3-eu-west-1.amazonaws.com/...).
    # On force https:// pour que le lien soit cliquable dans le mail.
    if receipt_url:
        receipt_url = receipt_url.strip()
        if receipt_url.startswith("//"):
            receipt_url = "https:" + receipt_url
        elif receipt_url.startswith("/"):
            receipt_url = "https://s3-eu-west-1.amazonaws.com" + receipt_url
        elif not receipt_url.startswith(("http://", "https://")):
            receipt_url = "https://" + receipt_url

    receipt_block = (
        f'<p>Vous pouvez télécharger votre reçu de paiement en cliquant ici : '
        f'<a href="{receipt_url}" target="_blank">Voir mon reçu</a></p>'
        if receipt_url else
        "<p>Votre reçu de paiement vous sera transmis sous peu.</p>"
    )
    doc_block = ("<p>Vous trouverez également votre <b>document de confirmation de "
                 "location</b> en pièce jointe.</p>" if contract_path else "")

    if is_extension:
        intro = (f"<p>Nous confirmons la réception de votre paiement complémentaire. "
                 f"La <b>prolongation</b> de votre location <b>{reference}</b> est "
                 f"désormais <b>validée</b> ✅. Votre nouvelle date de retour est le "
                 f"<b>{end_date}</b>.</p>")
    else:
        intro = (f"<p>Nous confirmons la réception de votre paiement. Votre réservation "
                 f"<b>{reference}</b> est désormais <b>confirmée</b> ✅.</p>")

    body = f"""
    <html><body>
        <p><img src="https://raw.githubusercontent.com/Lamine-admin/Thecarsociety_images/refs/heads/main/Background.JPG" alt="Background" style="width:800px;height:auto;"></p>
        <p>Bonjour <b>{client_name}</b>,</p>
        {intro}
        <ul>
            <li><b>Véhicule :</b> {car_identity}</li>
            <li><b>Début :</b> {start_date}</li>
            <li><b>Fin :</b> {end_date}</li>
            <li><b>Montant réglé :</b> {amount:.2f} €</li>
        </ul>
        {receipt_block}
        {doc_block}
        <p>Pour toute question, contactez-nous au +33 7 53 50 82 27 ou à contact@malovelycar.com</p>
        <p>Cordialement,</p>
        <p>L'équipe Malovelycar</p>
        <p><img src="https://raw.githubusercontent.com/Lamine-admin/Thecarsociety_images/refs/heads/main/Logos_MLC_TLC_combin%C3%A9s_2.PNG" alt="Logo Malovelycar" style="width:150px;height:auto;"></p>
    </body></html>
    """
    msg.attach(MIMEText(body, "html"))

    # Pièce jointe : document de confirmation de location
    if contract_path and os.path.isfile(contract_path):
        try:
            with open(contract_path, "rb") as f:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header("Content-Disposition",
                            f'attachment; filename="Confirmation_location_{reference}.pdf"')
            msg.attach(part)
        except Exception as e:
            print(f"⚠️ Pièce jointe (confirmation) non ajoutée : {e}")

    try:
        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
        server.login(smtp_username, smtp_password)
        server.sendmail(smtp_username, email, msg.as_string())
        server.quit()
        print(f"📧 Reçu envoyé à {email} ({reference})")
        return True
    except Exception as e:
        print(f"❌ Échec envoi reçu à {email} : {e}")
        return False


def send_carsitter_email(email, carsitter_name, reference, car_identity,
                         start_date, end_date, owner_name=""):
    """Prévient le carsitter qu'un paiement est reçu pour un véhicule de son
    agence : il peut préparer le bien sur l'application Fleetee Check."""
    if not email:
        print(f"⚠️ Pas d'email carsitter pour {reference}, notification non envoyée.")
        return False

    smtp_server, smtp_port = "smtp.office365.com", 587
    msg = MIMEMultipart()
    msg["From"] = smtp_username
    msg["To"] = email
    msg["Subject"] = f"Malovelycar - {reference} : Paiement reçu, préparez le véhicule"
    body = f"""
    <html><body>
        <p><img src="https://raw.githubusercontent.com/Lamine-admin/Thecarsociety_images/refs/heads/main/Background.JPG" alt="Background" style="width:800px;height:auto;"></p>
        <p>Bonjour <b>{carsitter_name}</b>,</p>
        <p>Le paiement de la réservation <b>{reference}</b> vient d'être
        effectué par le locataire ✅. Un véhicule de votre agence est concerné :</p>
        <ul>
            <li><b>Véhicule :</b> {car_identity}</li>
            {f"<li><b>Propriétaire :</b> {owner_name}</li>" if owner_name else ""}
            <li><b>Début :</b> {start_date}</li>
            <li><b>Fin :</b> {end_date}</li>
        </ul>
        <p>Vous pouvez dès à présent <b>préparer le bien</b> et gérer l'état des
        lieux directement sur l'application <b>Fleetee Check</b>.</p>
        <p>Merci pour votre réactivité 🚗</p>
        <p>Pour toute question, contactez-nous au +33 7 53 50 82 27 ou à contact@thecarsociety.fr</p>
        <p>Cordialement,</p>
        <p>L'équipe Thecarsociety</p>
        <p><img src="https://raw.githubusercontent.com/Lamine-admin/Thecarsociety_images/refs/heads/main/Logos_MLC_TLC_combin%C3%A9s_2.PNG" alt="Logo" style="width:150px;height:auto;"></p>
    </body></html>
    """
    msg.attach(MIMEText(body, "html"))
    try:
        s = smtplib.SMTP(smtp_server, smtp_port)
        s.starttls()
        s.login(smtp_username, smtp_password)
        s.sendmail(smtp_username, email, msg.as_string())
        s.quit()
        print(f"📧 Notification carsitter envoyée à {email} ({reference})")
        return True
    except Exception as e:
        print(f"❌ Échec notification carsitter à {email} : {e}")
        return False


# ---------------------------------------------------------------------------
# Cœur : enregistrement paiement + confirmation + reçu pour une session payée
# ---------------------------------------------------------------------------
def confirm_booking_for_session(reference, amount_paid, customer_email_fallback=None,
                                is_extension=False):
    """Enregistre le paiement, confirme la location, envoie le reçu.

    is_extension=True → paiement d'une PROLONGATION (lien créé par
    process_extensions de Phase_7) : la location est déjà confirmée ou en
    cours, on enregistre donc le paiement SANS toucher au statut.
    Retourne True si tout le cycle a réussi (ou en dry-run)."""

    booking = get_booking_by_reference(reference)
    if not booking:
        print(f"❌ Réservation introuvable sur Fleetee : {reference}")
        return False

    booking_id = booking.get("id")
    status = booking.get("status")
    status_code = booking.get("status_code")
    client_name = (booking.get("customer") or {}).get("name", "")
    email = (booking.get("customer") or {}).get("email") or customer_email_fallback
    vehicle = booking.get("vehicle") or {}
    car_identity = " ".join(x for x in [vehicle.get("brand", ""), vehicle.get("model", "")] if x)
    start_date = booking.get("start_date", "")
    end_date = booking.get("end_date", "")

    print(f"📋 {reference} | booking_id={booking_id} | statut actuel={status} "
          f"(code {status_code}) | montant payé={amount_paid:.2f}€"
          f"{' | PROLONGATION' if is_extension else ''}")

    # Garde selon le type de paiement :
    #  - paiement initial : UNIQUEMENT si en attente de paiement (accepted/20)
    #  - prolongation     : UNIQUEMENT si la location est confirmée ou en cours
    if is_extension:
        if status not in ("confirm", "check-out"):
            if reference not in _skip_logged:
                print(f"⏭️  Prolongation {reference} ignorée : statut '{status}' "
                      f"(code {status_code}) ≠ confirmée/en cours. Aucune action.")
                _skip_logged.add(reference)
            return False
    elif not (status == AWAITING_PAYMENT_STATUS and status_code == AWAITING_PAYMENT_CODE):
        if reference not in _skip_logged:
            print(f"⏭️  {reference} ignorée : statut '{status}' (code {status_code}) "
                  f"≠ en attente de paiement. Aucune action.")
            _skip_logged.add(reference)
        return False

    if DRY_RUN:
        action_statut = "statut conservé" if is_extension else "PUT status=confirm"
        print(f"🧪 [DRY-RUN] POST paiement {amount_paid:.2f}€ + {action_statut} + reçu à {email}")
        return True

    # 1) Enregistrer le paiement
    payment_payload = {
        "amount": round(amount_paid, 2),
        "payment_method": "card",          # paiement Stripe = carte
        "payment_type": "payment",
        "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "comment": ("Prolongation — paiement Stripe (automatique Malovelycar)"
                    if is_extension else "Paiement Stripe (automatique Malovelycar)"),
        "card_type": "",
    }
    res = fleetee_post(f"/bookings/{booking_id}/payments", payment_payload)
    if res is None:
        print(f"❌ Échec enregistrement paiement pour {reference} — on n'enchaîne pas.")
        return False

    # Récupérer le receipt_url du paiement le plus récent
    receipt_url = None
    payments = res.get("payments") or []
    if payments:
        # le plus récent = celui qu'on vient de créer
        last = sorted(payments, key=lambda p: p.get("date", ""))[-1]
        receipt_url = last.get("receipt_url")

    # 2) Confirmer la location — UNIQUEMENT pour un paiement initial.
    #    Une prolongation ne change pas le statut (déjà confirm/check-out).
    status_res = None
    if is_extension:
        print(f"ℹ️  Prolongation {reference} : paiement enregistré, statut '{status}' conservé.")
    else:
        status_res = fleetee_put(f"/bookings/{booking_id}/status", {"status": "confirm"})
        if status_res is None:
            print(f"⚠️  Paiement enregistré mais passage en 'confirm' a échoué pour {reference}.")
            # on continue quand même pour envoyer le reçu

    # 2bis) Récupérer le document de confirmation de location (généré à la confirmation)
    fresh = fleetee_get(f"/bookings/{booking_id}") or status_res or res or {}
    contract_url = find_confirmation_document(fresh)
    contract_path = None
    if contract_url:
        dest = os.path.join(tempfile.gettempdir(), f"confirmation_{booking_id}.pdf")
        contract_path = download_document(contract_url, dest)
        if not contract_path:
            print(f"⚠️ Document de confirmation non téléchargé pour {reference}.")
    else:
        print(f"ℹ️ Aucun document de confirmation trouvé pour {reference}.")

    # 3) Envoyer le reçu + le document de confirmation au client
    send_receipt_email(
        email=email,
        client_name=client_name,
        reference=reference,
        amount=amount_paid,
        receipt_url=receipt_url,
        car_identity=car_identity,
        start_date=start_date,
        end_date=end_date,
        contract_path=contract_path,
        is_extension=is_extension,
    )
    if contract_path:
        try:
            os.remove(contract_path)
        except OSError:
            pass

    # 4) Notifier le carsitter du véhicule, s'il y en a un.
    #    Lien véhicule -> carsitter via vehicules_proprietaires.json (get_owner_info),
    #    puis carsitter_id (compte Stripe) -> email via carsitters.json.
    plate = vehicle.get("registration_number", "")
    vehicle_id = vehicle.get("id")
    try:
        owner_name, _photo, carsitter_id = get_owner_info(vehicle_id, plate)
    except Exception as e:
        print(f"⚠️ get_owner_info a échoué pour {reference} : {e}")
        carsitter_id, owner_name = None, ""
    if carsitter_id:
        cs = load_carsitters().get(carsitter_id)
        if cs:
            send_carsitter_email(
                email=cs.get("email"),
                carsitter_name=cs.get("name", "carsitter"),
                reference=reference,
                car_identity=car_identity,
                start_date=start_date,
                end_date=end_date,
                owner_name=owner_name,
            )
        else:
            print(f"⚠️ Carsitter {carsitter_id} absent de carsitters.json "
                  f"pour {reference} : notification non envoyée.")
    else:
        print(f"ℹ️ Pas de carsitter pour le véhicule de {reference} : "
              f"aucune notification carsitter.")
    return True


# ---------------------------------------------------------------------------
# Passage de polling
# ---------------------------------------------------------------------------
def run_once():
    confirmed = load_confirmed_sessions()
    connected_accounts = load_connected_accounts()
    ref_map = load_reference_map()   # référence effective -> réf Fleetee + booking_id
    new_confirmed = False

    for initials, account in connected_accounts.items():
        account_id = account.get("id")
        if not account_id:
            continue

        for session in iter_paid_sessions(account_id):
            if session.id in confirmed:
                continue  # déjà traité

            effective_reference = get_reference_from_session(session, account_id)
            if not effective_reference:
                # session payée mais pas rattachable à une location -> on ignore
                continue

            # 🔒 Anti double paiement : le client a payé, son lien ne doit plus
            # être utilisable — désactivation immédiate, indépendamment de la
            # suite du traitement (statut, confirmation, reçu…).
            deactivate_session_payment_link(session, account_id, effective_reference)

            # La référence lue sur Stripe est la référence "effective" (LOC-Pxxxx,
            # ou LOC-Pxxxx-…-EXTn pour une prolongation). On retrouve la référence
            # Fleetee d'origine + le type via le mapping écrit par Phase_7.
            entry = ref_map.get(effective_reference)
            fleetee_reference = entry["fleetee_reference"] if entry else effective_reference
            is_extension = bool(entry.get("is_extension")) if entry else False

            amount_paid = (session.get("amount_total") or 0) / 100.0
            email_fallback = (session.get("customer_details") or {}).get("email")

            mapped = f" -> Fleetee {fleetee_reference}" if fleetee_reference != effective_reference else ""
            type_txt = "Paiement de PROLONGATION détecté" if is_extension else "Paiement détecté"
            print(f"💳 {type_txt} : {effective_reference}{mapped} ({amount_paid:.2f}€) "
                  f"compte {initials} session {session.id}")

            ok = confirm_booking_for_session(fleetee_reference, amount_paid, email_fallback,
                                             is_extension=is_extension)
            # ⚠️ Ne JAMAIS mémoriser la session en dry-run : rien n'a été
            # réellement écrit (ni Fleetee, ni reçu). La marquer "traitée"
            # la ferait ignorer définitivement par les passages en production
            # (cause du paiement de prolongation jamais enregistré du 09/07).
            if ok and not DRY_RUN:
                confirmed.add(session.id)
                new_confirmed = True

    if new_confirmed:
        save_confirmed_sessions(confirmed)
    else:
        print("🔍 Aucun nouveau paiement à confirmer.")


def main():
    mode = "DRY-RUN" if DRY_RUN else "PRODUCTION"
    print(f"⏳ Phase_8 — confirmation automatique des paiements [{mode}]")

    if "--once" in sys.argv:
        run_once()
        return

    while True:
        try:
            run_once()
        except Exception as e:
            print(f"💥 Erreur dans le passage de polling : {e}")
        print(f"🔄 Prochain contrôle dans {POLL_INTERVAL} s.")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
