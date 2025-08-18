import streamlit as st
import stripe
import smtplib
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
        _driver.get('https://app.fleetee.io/thecarsociety/bookings?page=0&limit=500')
        time.sleep(20)
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
        for i, header in enumerate(headers):
            if i < len(cells):
                if header == "Début":
                    details["Début"] = cells[i].find("span", {"style": "white-space: nowrap;"}).text.strip()
                    sub = cells[i].find("div", class_="sub_value_text")
                    if sub:
                        details["Propriétaire"] = " ".join(sub.text.strip().split("] ")[1:])
                elif header == "Fin":
                    details["Fin"] = cells[i].find("span", {"style": "white-space: nowrap;"}).text.strip()
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
def create_payment_link(reference, product_price, quantity, cartage_price, commission_rate):
    initials = get_seller_initials(reference)
    account = connected_accounts.get(initials)
    if not account:
        raise ValueError(f"Initiales inconnues : {initials}")
    price_id = create_product_on_connected_account(reference, product_price, account['id'])
    commission = int(product_price * quantity * commission_rate * 100)
    cartage = int(cartage_price * 100)
    total_fee = commission + cartage
    return stripe.PaymentLink.create(
        line_items=[{'price': price_id, 'quantity': quantity}],
        metadata={'product_name': reference, 'seller_initials': initials},
        application_fee_amount=total_fee,
        stripe_account=account['id']
    ).url

# 🔹 Interface Streamlit
def main():
    # Configuration de la page
    st.set_page_config(
        page_title="Malovelycar - Payer ma Réservation",
        page_icon="🚗",
        layout="wide"
    )

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
        st.markdown("""
        <div style="text-align:center;">
            <h1 style="margin-bottom:0.5em;">Malovelycar</h1>
            <h2 style="margin-top:0;">Payer ma Réservation</h2>
        </div>
        <hr>
        """, unsafe_allow_html=True)
        # st.title("Malovelycar  \nPayer ma Réservation")
        # st.markdown("---")

    # Guide rapide dans la barre latérale
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

    # Section principale
    st.markdown("### 🔍 Recherche de votre Réservation")
    with st.container():
        col1, col2 = st.columns([3, 1])
        with col1:
            reference = st.text_input(
                "Entrez la référence de votre réservation :",
                placeholder="Ex: LOC-P0001-2024-03-0001",
                value=st.session_state.last_reference if st.session_state.last_reference else "",
                help="Vous trouverez la référence dans l'email de demande de réservation"
            )

    if reference:
        st.session_state.last_reference = reference
        
        # Affichage du spinner pendant la recherche
        with st.spinner("🔄 Recherche des détails de votre réservation (temps estimé : 1 minute)..."):
            reservation_details, errors = fetch_reservation_details(reference)
            
            if reservation_details:
                st.session_state.reservation_details = reservation_details
                
                # Vérification de la clé 'Propriétaire'
                if "Propriétaire" not in reservation_details:
                    print("⚠️ La clé 'Propriétaire' est absente des détails de réservation.")
                else:
                    print(f"Propriétaire : {reservation_details['Propriétaire']}")
                
                # Affichage des détails dans un conteneur stylisé
                with st.container():
                    st.markdown("### 📋 Détails de votre Réservation")
                    with st.expander("Voir tous les détails", expanded=True):
                        col1, col2 = st.columns(2)
                        
                        with col1:
                            st.markdown("#### 🚘 Informations Principales")
                            st.markdown(f"""
                            - **Référence:** {reservation_details['Référence']}
                            - **Statut:** {reservation_details['Statut']}
                            - **Véhicule:** {reservation_details['Bien']}
                            - **Propriétaire:** {reservation_details['Propriétaire']}
                            - **Locataire:** {reservation_details['Locataire']}
                            """)
                        
                        with col2:
                            st.markdown("#### 📅 Détails de Location")
                            st.markdown(f"""
                            - **Début:** {reservation_details['Début']}
                            - **Fin:** {reservation_details['Fin']}
                            - **Durée:** {reservation_details['Durée']}
                            - **Tarif:** {reservation_details['Tarif']}
                            """)

                # Création et envoi du lien de paiement
                if st.session_state.reservation_details:
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
                        commission_rate=0.2
                    )
                    print(f"Lien de paiement généré : {payment_link}")

                    if payment_link:
                        st.session_state.payment_link = payment_link
                        st.markdown(f"### Montant total à régler (réservation + assurance) : {abs(product_price + cartage_price):.2f} €")
                        st.markdown(
                            f'''
                            <b> Montant de votre réservation : {product_price:.2f} €</b>
                            <br>
                            <a href="{payment_link}" target="_blank">
                                <img src="https://raw.githubusercontent.com/Lamine-admin/Thecarsociety_images/main/Payer_reservation_logo.png" alt="Payer ma réservation" style="width:200px;height:auto;">
                            </a>
                                ''',
                            unsafe_allow_html=True
                        )
                        st.markdown(
                            f'''
                            <b> Montant de votre assurance Cartage : {cartage_price:.2f} €</b>
                            <br>
                            <a href="https://app.cartage.club/subscription" target="_blank">
                                <img src="https://raw.githubusercontent.com/Lamine-admin/Thecarsociety_images/main/Payer_assurance_logo.png" alt="Payer mon assurance" style="width:200px;height:auto;">
                            </a>
                            ''',
                            unsafe_allow_html=True
                        )
            else:
                st.error("❌ Aucune réservation trouvée avec cette référence")
                if errors:
                    for error in errors:
                        st.warning(error)

if __name__ == "__main__":
    main()
