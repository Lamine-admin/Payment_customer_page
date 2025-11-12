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

from connected_accounts_list import connected_accounts



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
    smtp_username = config('SMTP_USERNAME')
    smtp_password = config('SMTP_PASSWORD')
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
    price_id = create_product_on_connected_account(reference, product_price, account_id)
    commission = int(product_price * quantity * commission_rate * 100)
    total_fee = commission
    try:
        return stripe.PaymentLink.create(
            line_items=[{'price': price_id, 'quantity': quantity}],
            metadata={'product_name': reference, 'seller_initials': initials},
            application_fee_amount=total_fee,
            stripe_account=account_id
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
                return tenant_info, errors

        errors.append(f"Aucune correspondance trouvée pour : {first_name} {last_name}")
        return None, errors

    except Exception as e:
        errors.append(f"Erreur générale : {e}")
        return None, errors

    finally:
        _driver.quit()

# 🔹 Interface Streamlit
def main():
    st.set_page_config(
        page_title="Malovelycar - Payer ma Réservation",
        page_icon="🚗",
        layout="wide"
    )

    # # 🔹 Bandeau image
    st.markdown("""
    <div style="text-align:left; margin-bottom:1rem;">
        <img src="https://raw.githubusercontent.com/Lamine-admin/Thecarsociety_images/main/Logos_MLC_TLC_combinés_2.PNG" 
             alt="Bandeau Malovelycar" 
             style="width:40%; max-height:200px; object-fit:cover; border: none;">
    </div>
    """, unsafe_allow_html=True)

    # Style CSS
    st.markdown("""
    <style>
        .form-wrapper {
            display: flex;
            flex-direction: column;
            align-items: center;
            margin-top: 1rem;
            padding: 0rem;
            margin: 2rem 0;
            width: 100%;
        }
        .form-wrapper label {
            font-weight: bold;
            margin-bottom: 0.3rem;
            display: block;
            text-align: left;
            width: 600px;
            max-width: 100%;
        }
        .form-wrapper input[type="text"] {
            width: 600px;
            max-width: 100%;
            background-color: white !important;
            border: 2px solid #ccc;
            border-radius: 6px;
            padding: 6px;
            text-align: center;
            transition: box-shadow 0.3s ease;
            margin-bottom: 1rem;
        }
        input[type="text"]:focus {
            outline: none;
            border-color: #8F93FF;
            box-shadow: 0 0 8px 2px #8F93FF;
        }
        .stButton > button {
            background-color: #8F93FF !important;
            color: white !important;
            border: none !important;
            border-radius: 8px !important;
            padding: 0.75rem 1.5rem !important;
            font-weight: 600 !important;
            font-size: 1.1rem !important;
            transition: background-color 0.3s ease, transform 0.1s ease;
            box-shadow: 0 4px 6px rgba(0,0,0,0.1);
            width: 100%;
            margin: 2rem 0;
        }
        .stButton > button:hover {
            background-color: #6C70E0 !important;
            cursor: pointer;
        }
        .stButton > button:active {
            background-color: #4E52C0 !important;
            transform: scale(0.98);
        }
    </style>
    """, unsafe_allow_html=True)

    # En-tête
    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        st.markdown("""
        <style>
            .responsive-title {
                display: flex;
                justify-content: center;
                align-items: center;
                white-space: nowrap;
                overflow: hidden;
                text-overflow: ellipsis;
                font-size: calc(1rem + 1.5vw);
                max-width: 100%;
                padding: 0.5rem;
                box-sizing: border-box;
                font-weight: bold;
                color: #333;
            }
        
            @media (max-width: 400px) {
                .responsive-title {
                    font-size: 0.9rem;
                }
            }
        </style>
        """, unsafe_allow_html=True)
        
        # Afficher le texte
        st.markdown('<div class="responsive-title">Payer ma Réservation</div>', unsafe_allow_html=True)

    # 🔹 Sidebar
    with st.sidebar:
        st.markdown("### 📌 Guide Rapide")
        st.markdown("""
         1️⃣ **Recherche de votre réservation**
         - Entrez la référence de votre réservation reçu par mail (Ex: LOC-P0001-2024-03-0001)
         - Les détails s'afficheront automatiquement (moins d'une minute d'attente)
                    
         2️⃣ **Vérification**
         - Contrôlez les informations affichées

         3️⃣ **Règlez votre réservation et votre assurance**
         - Cliquez sur les boutons de paiement
         - Les informations à fournir pour souscrire à l'assurance Cartage sont affichées dans les détails de la réservation
        
         ❓ **Besoin d'aide ?**
         - Email : contact@malovelycar.com
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
        help="La référence se trouve dans l'email de confirmation que vous avez reçu"
    )

    nom_famille = st.text_input(
        label="Nom de famille*",
        placeholder="Exemple : Dupont",
        value=st.session_state.nom_famille
    )

    if reference.strip() and nom_famille.strip():
        if st.button("💳 Générer le lien de paiement"):
            st.session_state.bouton_recherche = True
    else:
        st.button("💳 Générer le lien de paiement", disabled=True)

    # 🔹 Traitement après clic
    if st.session_state.bouton_recherche:
            st.session_state.last_reference = reference
            st.session_state.nom_famille = nom_famille

            with st.spinner("Génération du lien de paiement en cours (temps estimé : 30 secondes)..."):
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

                # 🔹 Calculs et lien de paiement
                    try:
                        days_number = int(reservation_details['Durée'].replace(" jours", "").strip())
                        cartage_price = days_number * 5
                        product_price = abs(float(reservation_details['Balance'].replace("€", "").replace(",", ".").strip()))
                        quantity = 1
                    except ValueError as e:
                        st.error(f"Erreur lors du calcul du tarif : {e}")
                        return

                    payment_link = create_payment_link(
                        reference=reservation_details['Référence'],
                        product_price=product_price - cartage_price,
                        quantity=quantity,
                        cartage_price=cartage_price,
                        commission_rate=0.1
                    )

            if payment_link:
                st.session_state.payment_link = payment_link
                st.markdown(f"### Montant de la réservation à régler : {abs(product_price- cartage_price):.2f} €")
                safe_payment_link = quote(payment_link, safe=':/?&=')

                st.markdown(f'''
                <div style="margin-bottom:1em;">
                    <div>
                        <a href="{safe_payment_link}" target="_blank">
                        <button style="background-color:#8F93FF; color:white; padding:0.75rem 1.5rem; border:none; border-radius:6px; font-size:1rem; font-weight:bold; cursor:pointer;">
                            Payer ma réservation
                        </button>
                        </a>
                    </div>
                </div>
                ''', unsafe_allow_html=True)

                st.markdown("---")
                st.markdown("### 🛡️ Souscrire à la protection complète Cartage ?")
                st.markdown(f'''
                            <div style="margin-bottom:1em;">
                                <div><strong>Montant de votre protection complète Cartage : {cartage_price:.2f} €</strong></div>
                            </div>
                            ''', unsafe_allow_html=True)
                if st.button("Générer les informations à renseigner pour votre protection complète Cartage"):
                    with st.spinner("Génération des informations de la protection complète Cartage à renseigner (temps estimé : 30 secondes)..."):
                        tenant_info, info_errors = get_tenant_info(reservation_details['Locataire'])

                        if tenant_info:
                            cartage_start_url = "https://app.cartage.club/share?source=cartage-home-header"
                            st.markdown("### 🔗 Formulaire protection sur-assurance Cartage")
                            st.markdown(f'''
                            <div style="border:1px solid #ccc; padding:1rem; border-radius:8px; background-color:#f9f9f9;">
                                <p><strong>1. Informations à renseigner :</strong></p>
                                <ul>
                                    <li><strong>Infos Client :</strong>
                                        <ul>
                                            <li>Prénom : {tenant_info['prenom']}</li>
                                            <li>Nom : {tenant_info['nom']}</li>
                                            <li>Email : {tenant_info['email']}</li>
                                            <li>Date de naissance : {tenant_info['date_de_naissance']}</li>
                                            <li>Téléphone : {tenant_info['telephone']}</li>
                                        </ul>
                                    </li>
                                    <li><strong>Infos Véhicule :</strong>
                                        <ul>
                                            <li>Plaque d'immatriculation : {reservation_details.get('Plaque', 'N/A')}</li>
                                            <li>Marque : {reservation_details.get('Marque', 'N/A')}</li>
                                            <li>Modèle : {reservation_details.get('Modèle', 'N/A')}</li>
                                        </ul>
                                    </li>
                                    <li><strong>Infos Propriétaire :</strong>
                                        <ul>
                                            <li>Prénom et NOM : {reservation_details['Propriétaire']}</li>
                                            <li>Email : contact@malovelycar.com</li>
                                        </ul>
                                    </li>
                                    <li><strong>Infos Dates :</strong>
                                        <ul>
                                            <li>Début : {reservation_details['Début']}</li>
                                            <li>Durée : {reservation_details['Durée']}</li>
                                        </ul>
                                    </li>
                                </ul>
                                <p><em>Ces informations vous seront demandées étape par étape sur Cartage.</em></p>
                                <hr>
                                <p><strong>2. Cliquez sur le bouton ci-dessous pour accéder au formulaire Cartage et payer votre protection :</strong></p>
                                <div>
                                    <a href="{cartage_start_url}">
                                        <button style="background-color:#8F93FF; color:white; padding:0.75rem 1.5rem; border:none; border-radius:8px; font-size:1rem; font-weight:bold; cursor:pointer;">
                                            Payer ma protection Cartage
                                        </button>
                                    </a>
                                </div>
                            </div>
                            ''', unsafe_allow_html=True)
                        else:
                            st.error("❌ Impossible de récupérer les informations du client.")
                            for err in info_errors:
                                st.warning(err)

if __name__ == "__main__":
    main()