import streamlit as st
import stripe
import smtplib
import re
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
from decouple import config
from functools import lru_cache
import time
import logging
import sys
from urllib.parse import quote


# 🔹 Logger configuration
logger = logging.getLogger("ConsoleLogger")
logger.setLevel(logging.DEBUG)
file_handler = logging.FileHandler("console_and_errors.log", encoding="utf-8")
console_handler = logging.StreamHandler(sys.stdout)
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(message)s'))
console_handler.setFormatter(logging.Formatter('%(message)s'))
logger.addHandler(file_handler)
logger.addHandler(console_handler)
print = logger.info

# 🔹 API Keys
try:
    stripe.api_key = config('STRIPE_API_KEY')
    smtp_username = config('SMTP_USERNAME_TLC')
    smtp_password = config('SMTP_PASSWORD_TLC')
    fleetee_username = config('FLEETEE_USERNAME')
    fleetee_password = config('FLEETEE_PASSWORD')
except Exception:
    st.error("❌ Erreur de configuration : variables d'environnement manquantes.")
    st.stop()

# 🔹 Initialisation du navigateur
def get_browser():
    try:
        chrome_options = Options()
        chrome_options.add_argument('--headless')
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--disable-gpu')
        chrome_options.add_argument('--window-size=1920,1080')
        chrome_options.add_argument('--disable-extensions')
        chrome_options.add_argument('--disable-infobars')
        chrome_options.add_argument('--disable-notifications')
        chrome_options.add_argument('--disable-popup-blocking')
        chrome_options.add_argument('--disable-blink-features=AutomationControlled')
        chrome_options.add_argument('--user-agent=Mozilla/5.0 ...')
        service = Service()
        return webdriver.Chrome(service=service, options=chrome_options)
    except Exception as e:
        print(f"❌ Erreur navigateur : {e}")
        return None

# 🔹 Connexion à Fleetee
def login_to_fleetee(_driver):
    try:
        _driver.get('https://app.fleetee.io/login')
        WebDriverWait(_driver, 10).until(EC.presence_of_element_located((By.ID, "email"))).send_keys(fleetee_username)
        _driver.find_element(By.ID, "outlined-adornment-password").send_keys(fleetee_password)
        _driver.find_element(By.ID, "outlined-adornment-password").send_keys(webdriver.Keys.RETURN)
        WebDriverWait(_driver, 10).until(EC.url_contains("/dashboard"))
        return True
    except Exception as e:
        print(f"Erreur Fleetee : {e}")
        return False

# 🔹 Récupération des détails de réservation
def fetch_reservation_details(reference):
    errors = []
    _driver = get_browser()
    if not _driver:
        errors.append("Navigateur indisponible.")
        return None, errors

    try:
        if not login_to_fleetee(_driver):
            errors.append("Connexion Fleetee échouée.")
            return None, errors
        # Chargement de la page avec moins de données
        _driver.get('https://app.fleetee.io/thecarsociety/bookings?page=0&limit=20')

        # Attente dynamique de l'apparition des lignes de réservation
        try:
            WebDriverWait(_driver, 10).until(
                EC.presence_of_element_located((By.CLASS_NAME, "pointer.table_row_link"))
            )
        except Exception:
            errors.append("Les données de réservation n'ont pas été chargées à temps.")
            return None, errors

        html = _driver.page_source
        soup = BeautifulSoup(html, 'html.parser')

        span = soup.find('span', {'class': 'pointer table_row_link'}, string=reference)
        if not span:
            errors.append(f"Référence introuvable : {reference}")
            return None, errors

        row = span.find_parent("tr")
        cells = row.find_all("td")
        headers = ["Référence", "Statut", "Bien", "Locataire", "Début", "Fin", "Durée", "Tarif", "Balance"]
        details = {}

        def extraire_infos_vehicule(bien_str):
            pattern = r"\](\w+)\s([\w\-éÉèÈêÊëËïÏîÎàÀçÇ]+).*?\[([A-Z]{2}-\d{3}-[A-Z]{2})\]"
            match = re.search(pattern, bien_str)
            if match:
                return {
                    "Marque": match.group(1),
                    "Modèle": match.group(2),
                    "Plaque": match.group(3)
                }
            return {}


        for i, header in enumerate(headers):
            if i < len(cells):
                if header == "Début":
                    details["Début"] = cells[i].find("span", {"style": "white-space: nowrap;"}).text.strip()
                    sub = cells[i].find("div", class_="sub_value_text")
                    if sub:
                        details["Propriétaire"] = " ".join(sub.text.strip().split("] ")[1:])
                elif header == "Fin":
                    details["Fin"] = cells[i].find("span", {"style": "white-space: nowrap;"}).text.strip()
                elif header == "Bien":
                    bien_text = cells[i].text.strip()
                    details["Bien"] = bien_text
                    infos_vehicule = extraire_infos_vehicule(bien_text)
                    details.update(infos_vehicule)
                else:
                    details[header] = cells[i].text.strip()
        return details, errors

    except Exception as e:
        errors.append(f"Erreur générale : {e}")
        return None, errors
    finally:
        _driver.quit()

# 🔹 Initiales du vendeur
@lru_cache(maxsize=100)
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

# 🔹 Création du lien de paiement
def create_payment_link(reference, product_price, quantity, cartage_price, commission_rate=0.1):
    initials = get_seller_initials(reference)
    account = st.secrets["connected_accounts"][initials]
    account_id = account["id"]
    account_email = account["email"]
    price_id = create_product_on_connected_account(reference, product_price, account['id'])
    commission = int(product_price * quantity * commission_rate * 100)
    total_fee = commission
    try:
        return stripe.PaymentLink.create(
            line_items=[{'price': price_id, 'quantity': quantity}],
            metadata={'product_name': reference, 'seller_initials': initials},
            application_fee_amount=total_fee,
            stripe_account=account['id']
        ).url
    except stripe.error.InvalidRequestError as e:
        if "Must provide price or price_data" in str(e) or "Invalid non-negative integer" in str(e):
            st.warning("ℹ️ Le paiement pour cette réservation a déjà été effectué ou le lien n'est plus valide.")
            return None
        else:
            st.error(f"❌ Erreur Stripe : {e}")
            return None

def get_tenant_info(tenant_name):
    errors = []
    _driver = get_browser()
    
    try:
        if not login_to_fleetee(_driver):
            errors.append("Échec de la connexion à Fleetee")
            return None, errors

        _driver.get('https://app.fleetee.io/thecarsociety/customers?page=0&limit=200')

        WebDriverWait(_driver, 10).until(
            EC.presence_of_element_located((By.XPATH, "//span[contains(text(), 'Email')]"))
        )

        html_content = _driver.page_source
        soup = BeautifulSoup(html_content, 'html.parser')

        tenant_name_parts = tenant_name.split(" ")
        if len(tenant_name_parts) < 2:
            errors.append(f"Format du nom complet invalide : {tenant_name}")
            return None, errors

        first_name = tenant_name_parts[0]
        last_name = " ".join(tenant_name_parts[1:])

        rows = soup.find_all("tr")
        for row in rows:
            cells = row.find_all("td")
            if not cells:
                continue

            headers = ["Nom", "Prénom", "Type", "Date de naissance", "Email", "Téléphone"]
            details = {}

            for i, cell in enumerate(cells):
                if i < len(headers):
                    span = cell.find("span")
                    value = span.text.strip() if span else ""
                    details[headers[i]] = value

            if details.get("Prénom") == first_name and details.get("Nom") == last_name:
                tenant_info = {
                    "prenom": details.get("Prénom"),
                    "nom": details.get("Nom"),
                    "email": details.get("Email"),
                    "date_de_naissance": details.get("Date de naissance"),
                    "telephone": details.get("Téléphone")
                }
                                # Extraire l'adresse e-mail
                tenant_email = details.get("Email")
                if tenant_email:
                    print(f"🟢 Email trouvé : {tenant_email}")
                    return tenant_email, errors
                else:
                    print(f"🔴 Erreur : Adresse e-mail introuvable pour le locataire : {first_name} {last_name}")
                    errors.append(f"Adresse e-mail introuvable pour : {first_name} {last_name}")
                    return None, errors
                return tenant_info, errors

        errors.append(f"Aucune correspondance trouvée pour : {first_name} {last_name}")
        return None, errors

    except Exception as e:
        errors.append(f"Erreur générale : {e}")
        return None, errors

    finally:
        _driver.quit()

# 🔹 Fonction pour envoyer un email avec le lien de paiement
def send_payment_link(email, client_name, product_name, payment_link, product_price, car_identity, days_number, cartage_price, owner_info, start_date, end_date):
    """
    Envoie un e-mail contenant le lien de paiement et des détails de réservation.
    """
    smtp_server = 'smtp.office365.com'
    smtp_port = 587
    total_price = abs(product_price) + cartage_price  # Calcul de la différence
    smtp_username = config('SMTP_USERNAME_TLC')
    smtp_password = config('SMTP_PASSWORD_TLC')

    try:
        # Vérification de l'adresse e-mail avant l'envoi
        if not email or "@" not in email:
            print("Erreur : Adresse e-mail invalide.")
            return False

        msg = MIMEMultipart()
        msg['From'] = smtp_username
        msg['To'] = email
        msg['Subject'] = f"Thecarsociety - {product_name} : Votre lien de paiement"

        # Contenu de l'e-mail
        body = f"""
        <html>
        <body>
            <p><img src="https://raw.githubusercontent.com/Lamine-admin/Thecarsociety_images/main/Banniere_thecarsociety.png" alt="Background" style="width:800px;height:auto;"></p>
            <p>Bonjour <b>{client_name}</b>,</p>
            <p>Merci d'avoir choisi Thecarsociety pour le paiement de votre réservation !</p>
            <p>Cliquez sur "Payer ma réservation" pour effectuer le paiement de <b>{product_price:.2f} €</b> de votre réservation n°<b>{product_name}</b> du véhicule <b>{car_identity}</b> du <b>{owner_info}</b> :</p>
            <p><a href="{payment_link}"><img src="https://raw.githubusercontent.com/Lamine-admin/Thecarsociety_images/main/Payer_reservation_logo_new.png" alt="Lien de paiement" style="width:200px;height:auto;"></a></p>
            <p>Pour finir, vous pouvez souscrire à la complémentaire d'assurance CARTAGE à 5€/jour afin de n'avoir aucune franchise à payer en cas de sinistre. Cliquez sur "Payer ma protection" et suivez les étapes indiquées :</p>
            <p><a href=" https://app.cartage.club/subscription"><img src="https://raw.githubusercontent.com/Lamine-admin/Thecarsociety_images/main/Payer_protection_logo_new.png" alt="Lien de paiement" style="width:200px;height:auto;"></a></p>
            <p>(Contactez le propriétaire du véhicule pour obtenir son adresse email ainsi que la plaque d'immatriculation de son véhicule)</p>
            <p>Merci de votre confiance !</p>
            <p>Cordialement,</p>
            <p>L'équipe Thecarsociety</p>
            <p><img src="https://raw.githubusercontent.com/Lamine-admin/Thecarsociety_images/main/Thecarsociety_logo.png" alt="Logo Thecarsociety" style="width:150px;height:auto;"></p>
        </body>
        </html>
        """
        msg.attach(MIMEText(body, 'html'))

        # Connexion au serveur SMTP et envoi de l'e-mail
        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
        server.login(smtp_username, smtp_password)
        server.sendmail(smtp_username, email, msg.as_string())
        server.quit()
        print("Lien de paiement envoyé avec succès par e-mail.")
        return True
    except Exception as e:
        print(f"Erreur lors de l'envoi du mail : {e}")
        return False

def main():
    # Configuration de la page
    st.set_page_config(
        page_title="TheCarSociety - Gestion des Paiements",
        page_icon="🚗",
        layout="wide"
    )

    # # 🔹 Bandeau image
    st.markdown("""
    <div style="text-align:left; margin-bottom:1rem;">
        <img src="https://raw.githubusercontent.com/Lamine-admin/Thecarsociety_images/main/Thecarsociety_logo.png" 
             alt="Bandeau Thecarsociety" 
             style="width:40%; max-height:200px; object-fit:cover; border: none;">
    </div>
    """, unsafe_allow_html=True)
    
    # Style CSS personnalisé
    st.markdown("""
        <style>
        .main {
            padding: 2rem;
        }
        .stButton>button {
            width: 100%;
            border-radius: 5px;
            height: 3em;
            font-weight: bold;
        }
        .info-box {
            background-color: #f0f2f6;
            padding: 1rem;
            border-radius: 5px;
            margin: 1rem 0;
        }
        .success-box {
            background-color: #d1e7dd;
            padding: 1rem;
            border-radius: 5px;
            margin: 1rem 0;
        }
        .warning-box {
            background-color: #fff3cd;
            padding: 1rem;
            border-radius: 5px;
            margin: 1rem 0;
        }
        h1 {
            color: #1E3C72;
            margin-bottom: 2rem;
        }
        h2 {
            color: #2E5090;
            margin: 1rem 0;
        }
        .step-box {
            border: 1px solid #e0e0e0;
            border-radius: 10px;
            padding: 1.5rem;
            margin: 1rem 0;
            background-color: white;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        </style>
    """, unsafe_allow_html=True)

    # Initialisation des variables de session
    if 'reservation_details' not in st.session_state:
        st.session_state.reservation_details = None
    if 'tenant_email' not in st.session_state:
        st.session_state.tenant_email = None
    if 'payment_link' not in st.session_state:
        st.session_state.payment_link = None
    if 'email_sent' not in st.session_state:
        st.session_state.email_sent = False
    if 'last_reference' not in st.session_state:
        st.session_state.last_reference = None

    # En-tête
    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        st.markdown("<h1 style='text-align: center;'>Générer un lien de Paiement</h1>", unsafe_allow_html=True)
        st.markdown("---")


    # Guide rapide dans la barre latérale
    with st.sidebar:
        st.markdown("### 📌 Guide Rapide")
        st.markdown("""
        1️⃣ **Recherche de réservation**
        - Entrez la référence de la réservation (Ex: LOC-P0001-2024-03-0001)
        - Les détails s'afficheront automatiquement
        
        2️⃣ **Vérification**
        - Contrôlez les informations
        - Vérifiez l'email du locataire
        
        3️⃣ **Envoi du lien**
        - Cliquez sur le bouton d'envoi
        - Attendez la confirmation
        
        ❓ **Besoin d'aide ?**
        - Email : support@thecarsociety.fr
        - Tél : +33 7 53 50 82 27
        """)

    # 🔹 Formulaire
    if 'last_reference' not in st.session_state:
        st.session_state.last_reference = ""
    if 'nom_famille' not in st.session_state:
        st.session_state.nom_famille = ""
    if 'bouton_recherche' not in st.session_state:
        st.session_state.bouton_recherche = False

    reference = st.text_input(
        label="Référence de réservation*",
        placeholder="Exemple : LOC-P0001-2024-03-0001",
        value=st.session_state.last_reference,
        help="Vous pouvez retrouver la référence dans la section liste des locations du logiciel FLEETEE ou directement dans le mail de \"Nouvelle demande de location\" que vous avez reçu"
    )

    nom_famille = st.text_input(
        label="Nom de famille du locataire*",
        placeholder="Exemple : Dupont",
        value=st.session_state.nom_famille,
        help="Renseignez le nom de famille du locataire"
    )

    if reference and nom_famille:
        if st.button("🔍 Rechercher la réservation"):
            st.session_state.bouton_recherche = True
    else:
        st.button("🔍 Rechercher la réservation", disabled=True)

    # 🔹 Traitement après clic
    if st.session_state.bouton_recherche:
            st.session_state.last_reference = reference
            st.session_state.nom_famille = nom_famille

            with st.spinner("Recherche des détails de la réservation en cours..."):
                reservation_details, errors = fetch_reservation_details(reference)

                if reservation_details and nom_famille.lower() in reservation_details['Locataire'].lower():
                    st.session_state.reservation_details = reservation_details

                    with st.container():
                        st.markdown("### 📋 Détails de votre Réservation")
                        col1, col2 = st.columns(2)
                        with col1:
                            st.write("#### 🚘 Informations Principales")
                            st.write(f"- **Référence :** {reservation_details['Référence']}")
                            st.write(f"- **Statut :** {reservation_details['Statut']}")
                            st.write(f"- **Véhicule :** {reservation_details['Bien']}")
                            st.write(f"- **Propriétaire :** {reservation_details['Propriétaire']}")
                            st.write(f"- **Locataire :** {reservation_details['Locataire']}")
                        with col2:
                            st.write("#### 📅 Détails de Location")
                            st.write(f"- **Début:** {reservation_details['Début']}")
                            st.write(f"- **Fin:** {reservation_details['Fin']}")
                            st.write(f"- **Durée:** {reservation_details['Durée']}")
                            st.write(f"- **Tarif:** {reservation_details['Tarif']}")
                
            # Récupération de l'email directement à partir de la page des clients
            with st.spinner("📧 Recherche de l'email du locataire..."):
                    tenant_name = reservation_details['Locataire']
                    tenant_email, errors = get_tenant_info(tenant_name)  # Utiliser get_tenant_info existant qui retourne tenant_email, errors
                    
                    if tenant_email:
                        # L'email a été trouvé et correspond bien à celui extrait dans get_tenant_info
                        st.session_state.tenant_email = tenant_email
                        st.success(f"📧 Email du locataire : {tenant_email}")
                    else:
                        # Gestion des erreurs : afficher des messages détaillés
                        st.error("❌ Impossible de récupérer l'email du locataire à partir de la page des clients.")
                        if errors:
                            for error in errors:
                                st.warning(f"🔍 Détail de l'erreur : {error}")
                
                # Création et envoi du lien de paiement
            if st.session_state.tenant_email:
                    # Nettoyer et convertir le tarif
                    try:
                        cartage_days_number = reservation_details.get('Durée', '0')
                        car_identity = reservation_details.get('Bien', None)
                        cleaned_cartage = cartage_days_number.replace(" jours", "").strip()
                        days_number = int(cleaned_cartage)
                        cartage_price = days_number * 5
                        raw_price = reservation_details.get('Balance', '0')
                        cleaned_price = raw_price.replace("€", "").replace(",", ".").strip()
                        product_price_abs = float(cleaned_price)
                        # product_price = abs(product_price_abs)
                        product_price = abs(product_price_abs) - cartage_price
                        quantity = 1  # Quantité fixée à 1
                        print(f"Le prix du produit est de {product_price:.2f} €")
                    except ValueError as e:
                        print(f"Erreur lors de la conversion du tarif : {e}")
                        exit()

                    # Créer le lien de paiement
                    payment_link = create_payment_link(
                        reference=reservation_details.get('Référence', 'Produit'),
                        product_price=product_price,
                        quantity=quantity,  # Quantité fixée à 1
                        cartage_price=cartage_price,
                        commission_rate=0.0
                    )

                    st.session_state.payment_link = payment_link
                    st.markdown(f"### 🎯 Lien de Paiement Généré : [Cliquez ici]({payment_link})", help="Vous pouvez copier le lien de paiement et le partager directement à votre locataire")
                        # Section d'envoi d'email
                        # st.markdown("### 📤 Envoi du Lien de Paiement")
                    st.info("ℹ️ Assurez-vous que le lien de paiement est correct avant de l'envoyer.")

                    if st.button("📤 Envoyer le lien à votre locataire", key="send_email"):
                            with st.spinner("📨 Envoi de l'email en cours..."):
                                try:
                                    owner_info = reservation_details.get("Propriétaire", "Propriétaire inconnu")
                                    send_payment_link(
                                            email=st.session_state.tenant_email,
                                            client_name=reservation_details['Locataire'],
                                            product_name=reference,
                                            payment_link=st.session_state.payment_link,
                                            product_price=product_price,
                                            car_identity=reservation_details['Bien'],
                                            days_number=days_number,
                                            cartage_price=cartage_price,
                                            owner_info=owner_info,  # Utilisation de la valeur par défaut si absent
                                            start_date=reservation_details['Début'],
                                            end_date=reservation_details['Fin']
                                        )
                                    st.session_state.email_sent = True
                                    st.success("✅ Email envoyé avec succès !")
                                    st.balloons()
                                except Exception as e:
                                    st.error(f"❌ Erreur lors de l'envoi de l'email : {str(e)}")
            elif st.session_state.email_sent:
                st.success("✅ L'email a déjà été envoyé pour cette réservation")
            else:
                    st.error("❌ Aucune réservation trouvée avec cette référence")
            if errors:
                    for error in errors:
                        st.warning(error)

if __name__ == "__main__":
    main()
