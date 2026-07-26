"""
contraventions.py — Traitement automatisé des contraventions / FPS (Thecarsociety).

Pour une amende reçue (photo/scan ou saisie manuelle), le script :
  1. Extrait la plaque d'immatriculation + la date/heure d'infraction (OCR Tesseract,
     avec saisie manuelle en secours / confirmation).
  2. Interroge l'API Fleetee pour retrouver la location (et donc le locataire) couvrant
     la date/heure de l'infraction pour ce véhicule.
  3. Génère un dossier PDF par infraction : page de garde (infos infraction + locataire),
     photos du permis recto/verso, et contrat de location.
  4. Stocke le dossier dans ./dossiers/<PLAQUE>/<AAAA-MM-JJ>/.
  5. Crée un lien de paiement Stripe (compte plateforme Thecarsociety) et envoie un
     email au locataire (SMTP Office365, comme Phase_7/Phase_8).

TARIFICATION
------------
  - Pénalité fixe : 15 € par amende (contravention ET FPS).
  - FPS : on ajoute le montant du FPS. Ex : FPS 25 € + 15 € = 40 € à payer.
  - Contravention simple : 15 €.

USAGE
-----
  python contraventions.py --image avis.jpg
  python contraventions.py --plate AB-123-CD --datetime "2026-05-10 14:30" --type contravention
  python contraventions.py --image fps.jpg --type fps --fps-amount 25
  python contraventions.py --image avis.jpg --yes          # envoie l'email sans confirmation
  python contraventions.py --plate AB-123-CD --datetime "2026-05-10 14:30" --type fps --fps-amount 25 --no-email

INSTALLATION (une fois)
-----------------------
  pip install requests stripe python-decouple reportlab pypdf pillow pytesseract
  # OCR : installer le moteur Tesseract + langue française
  #   Windows : https://github.com/UB-Mannheim/tesseract/wiki  (cocher "French")
  #   puis, si besoin, renseigner TESSERACT_CMD dans le .env

VARIABLES .env REQUISES (déjà présentes dans ton .env, + une optionnelle)
-------------------------------------------------------------------------
  STRIPE_API_KEY, FLEETEE_API_KEY, FLEETEE_SECRET_KEY, SMTP_USERNAME, SMTP_PASSWORD
  PENALTY_AMOUNT      (optionnel, défaut 15)
  TESSERACT_CMD       (optionnel, chemin vers tesseract.exe si non dans le PATH)
"""

import os
import re
import sys
import csv
import json
import argparse
import smtplib
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders

import requests
import stripe
from decouple import config

# --- Configuration -----------------------------------------------------------
try:
    stripe.api_key = config("STRIPE_API_KEY")
    FLEETEE_API_KEY = config("FLEETEE_API_KEY")
    FLEETEE_SECRET_KEY = config("FLEETEE_SECRET_KEY")
    SMTP_USERNAME = config("SMTP_USERNAME")
    SMTP_PASSWORD = config("SMTP_PASSWORD")
except Exception:
    print("❌ Variables d'environnement manquantes (.env).")
    sys.exit(1)

PENALTY_AMOUNT = config("PENALTY_AMOUNT", default=15, cast=float)
TESSERACT_CMD = config("TESSERACT_CMD", default="")
FLEETEE_API_BASE = "https://api.fleetee.io"
OUTPUT_ROOT = config("CONTRAVENTIONS_DIR", default="dossiers")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# Mapping plaque -> code propriétaire (déjà utilisé par Phase_7) et infos perso
VEHICULES_FILE = config("VEHICULES_FILE",
                        default=os.path.join(SCRIPT_DIR, "..", "vehicules_proprietaires.json"))
PROPRIETAIRES_FILE = config("PROPRIETAIRES_FILE",
                            default=os.path.join(SCRIPT_DIR, "proprietaires.json"))


# --- API Fleetee -------------------------------------------------------------
def _fleetee_headers():
    return {"x-api-key": FLEETEE_API_KEY, "secret-key": FLEETEE_SECRET_KEY}


def fleetee_get(endpoint, params=None):
    try:
        r = requests.get(f"{FLEETEE_API_BASE}{endpoint}", headers=_fleetee_headers(),
                         params=params, timeout=30)
    except Exception as e:
        print(f"❌ Erreur réseau Fleetee : {e}")
        return None
    if r.status_code == 200:
        return r.json()
    print(f"❌ Fleetee {endpoint} : {r.status_code} {r.text[:200]}")
    return None


def parse_dt(value):
    """Parse une date/heure depuis divers formats Fleetee ou utilisateur."""
    if not value:
        return None
    value = value.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                "%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y"):
        try:
            return datetime.strptime(value[:len(fmt) + 2].strip(), fmt)
        except ValueError:
            continue
    # dernier essai : tronquer à 16 caractères "YYYY-MM-DD HH:MM"
    try:
        return datetime.strptime(value[:16], "%Y-%m-%d %H:%M")
    except ValueError:
        return None


def find_booking(plate, infraction_dt):
    """Retrouve la location couvrant la date/heure d'infraction pour la plaque."""
    dstr = infraction_dt.strftime("%Y-%m-%d %H:%M")
    # Variantes de plaque (avec/sans tirets) pour fiabiliser la recherche
    variants = {plate, plate.replace("-", ""), plate.replace("-", " "),
                plate.replace(" ", "-"), plate.replace(" ", "")}
    candidates = []
    for pl in variants:
        data = fleetee_get("/bookings", params={
            "vehicle_registration_number": pl,
            "max_start_date": dstr,
            "min_end_date": dstr,
            "limit": 50,
        })
        if data and data.get("list"):
            candidates = data["list"]
            break
    if not candidates:
        # Repli : toutes les locations de la plaque, filtrage local sur la période
        for pl in variants:
            data = fleetee_get("/bookings", params={
                "vehicle_registration_number": pl, "limit": 100})
            if data and data.get("list"):
                candidates = data["list"]
                break

    # Filtre local : start_date <= infraction <= end_date
    for b in candidates:
        start = parse_dt(b.get("start_date"))
        end = parse_dt(b.get("end_date"))
        if start and end and start <= infraction_dt <= end:
            return b
    return candidates[0] if candidates else None


def get_booking_detail(booking_id):
    return fleetee_get(f"/bookings/{booking_id}")


def get_customer_detail(customer_id):
    """Détail client (adresse, date/lieu de naissance, permis…) via /customers/{id}."""
    if not customer_id:
        return {}
    return fleetee_get(f"/customers/{customer_id}") or {}


def _load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def get_owner_details(plate):
    """Plaque -> code propriétaire (vehicules_proprietaires.json) -> infos perso
    (proprietaires.json). Renvoie (owner_code, details_dict)."""
    pn = plate.replace("-", "").replace(" ", "").upper()
    owner_code = None
    for v in (_load_json(VEHICULES_FILE) or []):
        vp = str(v.get("plate", "")).replace("-", "").replace(" ", "").upper()
        if vp and vp == pn:
            owner_code = (v.get("owner_code") or "").strip()
            break
    props = _load_json(PROPRIETAIRES_FILE) or {}
    return owner_code, (props.get(owner_code) or {} if owner_code else {})


def _fmt_addr(a):
    """Formate une adresse Fleetee (objet) en une ligne lisible."""
    if not a or not isinstance(a, dict):
        return "Non renseigné"
    ville = f'{a.get("postcode","") or ""} {a.get("city","") or ""}'.strip()
    parts = [a.get("street_line_one"), a.get("street_line_two"), a.get("district"), ville]
    out = ", ".join(p for p in parts if p and str(p).strip())
    return out or "Non renseigné"


# --- OCR ---------------------------------------------------------------------
PLATE_SIV = re.compile(r"\b([A-Z]{2})[\s-]?(\d{3})[\s-]?([A-Z]{2})\b")
PLATE_FNI = re.compile(r"\b(\d{1,4})[\s-]?([A-Z]{2,3})[\s-]?(\d{2})\b")
DATE_RE = re.compile(r"\b(\d{2})[/.\-](\d{2})[/.\-](\d{4})\b")
TIME_RE = re.compile(r"\b(\d{1,2})[hH:](\d{2})\b")


def ocr_image(image_path):
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        print("⚠️ pytesseract/Pillow non installés — passage en saisie manuelle.")
        return ""
    if TESSERACT_CMD:
        pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD
    try:
        txt = pytesseract.image_to_string(Image.open(image_path), lang="fra")
    except Exception as e:
        print(f"⚠️ OCR indisponible ({e}) — saisie manuelle.")
        return ""
    return txt.upper()


def extract_plate(text):
    m = PLATE_SIV.search(text)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = PLATE_FNI.search(text)
    if m:
        return f"{m.group(1)} {m.group(2)} {m.group(3)}"
    return None


def extract_datetime(text):
    d = DATE_RE.search(text)
    t = TIME_RE.search(text)
    if d:
        date_part = f"{d.group(3)}-{d.group(2)}-{d.group(1)}"
        time_part = f"{int(t.group(1)):02d}:{t.group(2)}" if t else "00:00"
        return f"{date_part} {time_part}"
    return None


def ask(prompt, default=None):
    suffix = f" [{default}]" if default else ""
    val = input(f"{prompt}{suffix} : ").strip()
    return val or default


def gather_infraction(args):
    """Renvoie (plate, infraction_dt, type, fps_amount) après OCR/saisie/confirmation."""
    ocr_plate = ocr_dt = None
    if args.image:
        if not os.path.isfile(args.image):
            print(f"❌ Image introuvable : {args.image}")
            sys.exit(1)
        text = ocr_image(args.image)
        ocr_plate = extract_plate(text)
        ocr_dt = extract_datetime(text)
        print(f"🔍 OCR — plaque détectée : {ocr_plate or '—'} | date/heure : {ocr_dt or '—'}")

    plate = args.plate or ocr_plate
    plate = ask("Plaque d'immatriculation", plate)
    dt_str = args.datetime or ocr_dt
    dt_str = ask("Date/heure d'infraction (AAAA-MM-JJ HH:MM)", dt_str)
    infraction_dt = parse_dt(dt_str)
    while not infraction_dt:
        dt_str = ask("Format invalide. Date/heure (AAAA-MM-JJ HH:MM)")
        infraction_dt = parse_dt(dt_str)

    inf_type = (args.type or ask("Type (contravention/fps)", "contravention")).lower()
    fps_amount = 0.0
    if inf_type == "fps":
        amt = args.fps_amount if args.fps_amount is not None else ask("Montant du FPS (€)", "0")
        try:
            fps_amount = float(str(amt).replace(",", "."))
        except ValueError:
            fps_amount = 0.0
    return plate.strip().upper(), infraction_dt, inf_type, fps_amount


# --- PDF ---------------------------------------------------------------------
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


def _download(url, dest):
    url = _normalize_url(url)
    if not url:
        return None
    base = {"User-Agent": "Mozilla/5.0"}
    # 1er essai sans auth (URL S3 publique) ; repli avec en-têtes Fleetee
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


def build_dossier(booking, infraction, out_pdf, owner=None, customer_full=None):
    """Construit le dossier PDF : page de garde (infraction, propriétaire,
    locataire, location) + permis recto/verso + contrat."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas
    from pypdf import PdfReader, PdfWriter

    owner = owner or {}
    customer_full = customer_full or {}
    tmp_dir = os.path.dirname(out_pdf)
    front_pdf = os.path.join(tmp_dir, "_front.pdf")
    W, H = A4

    customer = booking.get("customer") or {}
    vehicle = booking.get("vehicle") or {}
    licences = booking.get("driving_licences") or customer_full.get("driving_licences") or []
    lic = licences[0] if licences else {}

    c = canvas.Canvas(front_pdf, pagesize=A4)
    state = {"y": H - 25 * mm}

    def line(text, indent=30 * mm, font="Helvetica", size=11, dy=7 * mm):
        if state["y"] < 25 * mm:
            c.showPage()
            state["y"] = H - 25 * mm
        c.setFont(font, size)
        c.drawString(indent, state["y"], text)
        state["y"] -= dy

    def header(text):
        state["y"] -= 4 * mm
        line(text, indent=25 * mm, font="Helvetica-Bold", size=12, dy=8 * mm)

    line("Dossier d'infraction — Thecarsociety / Malovelycar",
         indent=25 * mm, font="Helvetica-Bold", size=15, dy=10 * mm)

    header("Infraction")
    for label, val in [
        ("Type", infraction["type"].upper()),
        ("Plaque", infraction["plate"]),
        ("Date / heure", infraction["datetime"].strftime("%d/%m/%Y %H:%M")),
        ("Montant FPS", f'{infraction["fps_amount"]:.2f} €' if infraction["type"] == "fps" else "—"),
        ("Pénalité", f'{infraction["penalty"]:.2f} €'),
        ("Total à payer", f'{infraction["total"]:.2f} €'),
    ]:
        line(f"{label} : {val}")

    header("Propriétaire du véhicule")
    owner_name = " ".join(x for x in [owner.get("prenom", ""), owner.get("nom", "")] if x).strip()
    for label, val in [
        ("Nom / prénom", owner_name or "Non renseigné"),
        ("Date de naissance", owner.get("date_naissance") or "Non renseigné"),
        ("Lieu de naissance", owner.get("lieu_naissance") or "Non renseigné"),
        ("Adresse postale", owner.get("adresse") or "Non renseigné"),
        ("Email", owner.get("email") or "Non renseigné"),
    ]:
        line(f"{label} : {val}")

    header("Locataire")
    lieu_delivrance = (lic.get("license_place") or lic.get("place")
                       or lic.get("issuing_place") or lic.get("country") or "Non renseigné")
    for label, val in [
        ("Nom", customer.get("name") or f'{customer.get("first_name","")} {customer.get("last_name","")}'.strip()),
        ("Email", customer.get("email") or "—"),
        ("Téléphone", customer.get("phone_number") or "—"),
        ("Adresse postale", _fmt_addr(customer_full.get("address"))),
        ("N° de permis", lic.get("license_number") or "Non renseigné"),
        ("Date de délivrance du permis", lic.get("license_date") or "Non renseigné"),
        ("Lieu de délivrance", lieu_delivrance),
    ]:
        line(f"{label} : {val}")

    header("Location")
    for label, val in [
        ("Référence", booking.get("reference") or "—"),
        ("Véhicule", f'{vehicle.get("brand","")} {vehicle.get("model","")}'.strip()),
        ("Période", f'{booking.get("start_date","")} → {booking.get("end_date","")}'),
    ]:
        line(f"{label} : {val}")
    c.showPage()

    # --- Photos du permis (recto/verso) ---
    img_urls = []
    if lic.get("recto_url"):
        img_urls.append(("Permis — recto", lic["recto_url"]))
    if lic.get("verso_url"):
        img_urls.append(("Permis — verso", lic["verso_url"]))
    for i, (title, url) in enumerate(img_urls):
        path = _download(url, os.path.join(tmp_dir, f"_lic_{i}.jpg"))
        c.setFont("Helvetica-Bold", 12)
        c.drawString(25 * mm, H - 25 * mm, title)
        if path:
            try:
                img = ImageReader(path)
                iw, ih = img.getSize()
                max_w, max_h = W - 50 * mm, H - 60 * mm
                scale = min(max_w / iw, max_h / ih)
                c.drawImage(img, 25 * mm, 25 * mm, width=iw * scale, height=ih * scale,
                            preserveAspectRatio=True, mask="auto")
            except Exception:
                c.setFont("Helvetica", 11)
                c.drawString(25 * mm, H - 40 * mm, "(image illisible)")
        else:
            c.setFont("Helvetica", 11)
            c.drawString(25 * mm, H - 40 * mm, "(téléchargement de l'image échoué)")
        c.showPage()
    c.save()

    # --- Fusion : page de garde + permis + contrat de location ---
    writer = PdfWriter()
    for page in PdfReader(front_pdf).pages:
        writer.add_page(page)

    contract_url = booking.get("contract_url")
    if contract_url:
        contract_path = _download(contract_url, os.path.join(tmp_dir, "_contrat.pdf"))
        if contract_path:
            try:
                for page in PdfReader(contract_path).pages:
                    writer.add_page(page)
            except Exception:
                print("⚠️ Contrat illisible, non fusionné.")
        else:
            print(f"⚠️ Téléchargement du contrat échoué. URL : {_normalize_url(contract_url)}")
    else:
        print("ℹ️ Aucun contrat (contract_url absent) pour cette location.")

    # Écriture du PDF — repli horodaté si le fichier cible est verrouillé (déjà ouvert)
    final_pdf = out_pdf
    try:
        with open(out_pdf, "wb") as f:
            writer.write(f)
    except PermissionError:
        base, ext = os.path.splitext(out_pdf)
        final_pdf = f"{base}_{datetime.now():%H%M%S}{ext}"
        print(f"⚠️ {os.path.basename(out_pdf)} verrouillé (déjà ouvert ?) — "
              f"écriture sous : {os.path.basename(final_pdf)}")
        with open(final_pdf, "wb") as f:
            writer.write(f)

    # Nettoyage des fichiers temporaires
    for name in os.listdir(tmp_dir):
        if name.startswith("_"):
            try:
                os.remove(os.path.join(tmp_dir, name))
            except OSError:
                pass
    return final_pdf


# --- Masquage de l'adresse du propriétaire sur l'avis ------------------------
_ADDR_STOPWORDS = {"RUE", "AVENUE", "AV", "BOULEVARD", "BD", "ALLEE", "ALLÉE",
                   "IMPASSE", "CHEMIN", "ROUTE", "PLACE", "QUAI", "COURS",
                   "DE", "DU", "DES", "LA", "LE", "LES", "ET", "A", "AU", "AUX"}


def redact_address_on_image(image_path, address_text, out_path):
    """Masque (caches noirs) les mots de l'adresse du propriétaire sur l'avis.
    Renvoie le chemin de l'image masquée, ou None si le masquage n'a pas pu être
    confirmé (dans ce cas, par sécurité, on n'enverra pas l'avis)."""
    try:
        import pytesseract
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    if not address_text:
        return None
    if TESSERACT_CMD:
        pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD

    # Jetons significatifs de l'adresse (on ignore les mots trop génériques)
    tokens = set()
    for tok in re.split(r"[\s,;]+", address_text.upper()):
        tok = re.sub(r"[^0-9A-ZÀ-Ÿ]", "", tok)
        if tok and tok not in _ADDR_STOPWORDS and (tok.isdigit() or len(tok) >= 3):
            tokens.add(tok)
    if not tokens:
        return None

    try:
        img = Image.open(image_path).convert("RGB")
        data = pytesseract.image_to_data(img, lang="fra",
                                         output_type=pytesseract.Output.DICT)
    except Exception:
        return None

    draw = ImageDraw.Draw(img)
    masked = 0
    for i in range(len(data["text"])):
        word = re.sub(r"[^0-9A-ZÀ-Ÿ]", "", (data["text"][i] or "").upper())
        if word and word in tokens:
            x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
            draw.rectangle([x - 2, y - 2, x + w + 2, y + h + 2], fill="black")
            masked += 1
    if masked == 0:
        return None
    try:
        img.save(out_path)
    except Exception:
        return None
    return out_path


# --- Stripe (compte plateforme) ---------------------------------------------
def create_penalty_payment_link(label, amount):
    try:
        product = stripe.Product.create(
            name=label,
            default_price_data={"unit_amount": int(round(amount * 100)), "currency": "eur"},
        )
        link = stripe.PaymentLink.create(
            line_items=[{"price": product["default_price"], "quantity": 1}]
        )
        return link.url
    except Exception as e:
        print(f"❌ Erreur Stripe : {e}")
        return None


# --- Email -------------------------------------------------------------------
def send_penalty_email(email, client_name, infraction, payment_link, attachment_path=None):
    smtp_server, smtp_port = "smtp.office365.com", 587
    msg = MIMEMultipart()
    msg["From"] = SMTP_USERNAME
    msg["To"] = email
    msg["Subject"] = f"Malovelycar - Infraction véhicule {infraction['plate']} : règlement"

    fps_line = (f"<li><b>Montant FPS :</b> {infraction['fps_amount']:.2f} €</li>"
                if infraction["type"] == "fps" else "")
    body = f"""
    <html><body>
        <p><img src="https://raw.githubusercontent.com/Lamine-admin/Thecarsociety_images/refs/heads/main/Background.JPG" alt="Background" style="width:800px;height:auto;"></p>
        <p>Bonjour <b>{client_name}</b>,</p>
        <p>Une infraction a été constatée sur le véhicule <b>{infraction['plate']}</b>
        que vous avez loué, à la date du <b>{infraction['datetime'].strftime('%d/%m/%Y à %H:%M')}</b>.</p>
        <p>Conformément à nos conditions de location, le montant suivant vous est facturé :</p>
        <ul>
            {fps_line}
            <li><b>Pénalité de gestion :</b> {infraction['penalty']:.2f} €</li>
            <li><b>Total à régler :</b> {infraction['total']:.2f} €</li>
        </ul>
        <p>Merci de procéder au règlement via le lien sécurisé ci-dessous :</p>
        <p><a href="{payment_link}" target="_blank">Régler l'infraction</a></p>
        <p>Pour toute question, contactez-nous au +33 7 53 50 82 27 ou à contact@malovelycar.com</p>
        <p>Cordialement,</p>
        <p>L'équipe Malovelycar</p>
        <p><img src="https://raw.githubusercontent.com/Lamine-admin/Thecarsociety_images/refs/heads/main/Logos_MLC_TLC_combin%C3%A9s_2.PNG" alt="Logo Malovelycar" style="width:150px;height:auto;"></p>
    </body></html>
    """
    msg.attach(MIMEText(body, "html"))

    # Pièce jointe : avis avec l'adresse du propriétaire masquée
    if attachment_path and os.path.isfile(attachment_path):
        try:
            with open(attachment_path, "rb") as f:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header("Content-Disposition",
                            f'attachment; filename="{os.path.basename(attachment_path)}"')
            msg.attach(part)
        except Exception as e:
            print(f"⚠️ Pièce jointe non ajoutée : {e}")

    try:
        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
        server.login(SMTP_USERNAME, SMTP_PASSWORD)
        server.sendmail(SMTP_USERNAME, email, msg.as_string())
        server.quit()
        print(f"📧 Email envoyé à {email}")
        return True
    except Exception as e:
        print(f"❌ Échec de l'envoi de l'email : {e}")
        return False


# --- Registre CSV ------------------------------------------------------------
def append_register(infraction, booking, payment_link, dossier_path, sent):
    os.makedirs(OUTPUT_ROOT, exist_ok=True)
    path = os.path.join(OUTPUT_ROOT, "registre_contraventions.csv")
    customer = booking.get("customer") or {}
    fields = ["traite_le", "plaque", "date_infraction", "type", "fps_amount",
              "penalite", "total", "reference", "locataire", "email",
              "lien_paiement", "dossier", "email_envoye"]
    row = {
        "traite_le": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "plaque": infraction["plate"],
        "date_infraction": infraction["datetime"].strftime("%Y-%m-%d %H:%M"),
        "type": infraction["type"],
        "fps_amount": f'{infraction["fps_amount"]:.2f}',
        "penalite": f'{infraction["penalty"]:.2f}',
        "total": f'{infraction["total"]:.2f}',
        "reference": booking.get("reference") or "",
        "locataire": customer.get("name") or "",
        "email": customer.get("email") or "",
        "lien_paiement": payment_link or "",
        "dossier": dossier_path or "",
        "email_envoye": "oui" if sent else "non",
    }
    exists = os.path.isfile(path)
    with open(path, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, delimiter=";")
        if not exists:
            w.writeheader()
        w.writerow(row)


# --- Programme principal -----------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Traitement des contraventions / FPS.")
    parser.add_argument("--image", help="Photo/scan de l'avis (OCR)")
    parser.add_argument("--plate", help="Plaque d'immatriculation")
    parser.add_argument("--datetime", help="Date/heure d'infraction (AAAA-MM-JJ HH:MM)")
    parser.add_argument("--type", choices=["contravention", "fps"], help="Type d'infraction")
    parser.add_argument("--fps-amount", type=float, help="Montant du FPS (€), si type=fps")
    parser.add_argument("--no-email", action="store_true", help="Ne pas envoyer d'email")
    parser.add_argument("--yes", action="store_true", help="Envoyer l'email sans confirmation")
    args = parser.parse_args()

    # 1) Infos infraction
    plate, infraction_dt, inf_type, fps_amount = gather_infraction(args)
    total = PENALTY_AMOUNT + (fps_amount if inf_type == "fps" else 0.0)
    infraction = {"plate": plate, "datetime": infraction_dt, "type": inf_type,
                  "fps_amount": fps_amount, "penalty": PENALTY_AMOUNT, "total": total}
    print(f"\n💶 Montant total à facturer : {total:.2f} € "
          f"(pénalité {PENALTY_AMOUNT:.2f} €"
          f"{f' + FPS {fps_amount:.2f} €' if inf_type == 'fps' else ''})")

    # 2) Recherche de la location / locataire
    print(f"\n🔎 Recherche de la location pour {plate} au {infraction_dt:%d/%m/%Y %H:%M}…")
    mini = find_booking(plate, infraction_dt)
    if not mini:
        print("❌ Aucune location trouvée pour cette plaque à cette date.")
        sys.exit(1)
    booking = get_booking_detail(mini.get("id")) or mini
    customer = booking.get("customer") or {}
    print(f"   ✓ Location {booking.get('reference','?')} — locataire : "
          f"{customer.get('name','?')} ({customer.get('email','?')})")

    customer_full = get_customer_detail(customer.get("id"))
    owner_code, owner = get_owner_details(plate)
    if owner:
        print(f"   ✓ Propriétaire : {owner.get('prenom','')} {owner.get('nom','')} "
              f"(code {owner_code or '?'})".replace("  ", " "))
    else:
        print(f"   ⚠️ Propriétaire absent de proprietaires.json (code {owner_code or 'inconnu'}) "
              f"— section propriétaire incomplète.")

    # 3-4) Dossier PDF stocké par plaque/date
    date_dir = infraction_dt.strftime("%Y-%m-%d")
    out_dir = os.path.join(OUTPUT_ROOT, plate.replace(" ", "_"), date_dir)
    os.makedirs(out_dir, exist_ok=True)
    out_pdf = os.path.join(out_dir, f"dossier_{plate.replace(' ', '_')}_{date_dir}.pdf")
    print("\n📄 Génération du dossier PDF…")
    try:
        out_pdf = build_dossier(booking, infraction, out_pdf, owner=owner, customer_full=customer_full)
        print(f"   ✓ {out_pdf}")
    except Exception as e:
        print(f"⚠️ Échec de la génération du PDF : {e}")
        out_pdf = None

    # 5) Lien de paiement Stripe (compte plateforme)
    label = f"Infraction {plate} {date_dir} ({inf_type})"
    payment_link = create_penalty_payment_link(label, total)
    if payment_link:
        print(f"\n🔗 Lien de paiement : {payment_link}")
    else:
        print("⚠️ Lien de paiement non créé.")

    # Pièce jointe : avis avec l'adresse du propriétaire masquée
    attachment = None
    if args.image:
        owner_addr = owner.get("adresse")
        if owner_addr:
            red_path = os.path.join(out_dir, "avis_adresse_masquee.jpg")
            attachment = redact_address_on_image(args.image, owner_addr, red_path)
            if attachment:
                print(f"\n🖼️ Avis avec adresse propriétaire masquée : {attachment}")
                print("   ⚠️ Ouvre ce fichier et vérifie le masquage AVANT de confirmer l'envoi.")
            else:
                print("\n⚠️ Masquage de l'adresse propriétaire non confirmé — l'avis ne sera PAS joint (sécurité).")
        else:
            print("\n⚠️ Adresse du propriétaire absente (proprietaires.json) — avis non joint (sécurité).")

    # 6) Email au locataire
    email = customer.get("email")
    sent = False
    if args.no_email:
        print("✉️ Envoi d'email désactivé (--no-email).")
    elif not email:
        print("⚠️ Pas d'email locataire — envoi impossible.")
    elif not payment_link:
        print("⚠️ Pas de lien de paiement — email non envoyé.")
    else:
        do_send = args.yes
        if not do_send:
            do_send = ask(f"Envoyer l'email à {email} ? (o/N)", "N").lower().startswith("o")
        if do_send:
            sent = send_penalty_email(email, customer.get("name", ""), infraction,
                                      payment_link, attachment_path=attachment)
        else:
            print("✉️ Email non envoyé (annulé).")

    append_register(infraction, booking, payment_link, out_pdf, sent)
    print("\n✅ Traitement terminé.")


if __name__ == "__main__":
    main()
