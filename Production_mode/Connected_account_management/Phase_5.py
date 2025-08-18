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
        time.sleep(5)
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

# 🔹 Envoi de l’e-mail
def send_payment_link(email, client_name, product_name, payment_link, product_price, car_identity, days_number, cartage_price, owner_info, start_date, end_date):
    try:
        if not email or "@" not in email:
            print("Email invalide.")
            return False
        msg = MIMEMultipart()
        msg['From'] = smtp_username
        msg['To'] = email
        msg['Subject'] = f"Malovelycar - {product_name} : Votre lien de paiement"
        body = f"""<html><body><p>Bonjour <b>{client_name}</b>, ...</p></body></html>"""
        msg.attach(MIMEText(body, 'html'))
        server = smtplib.SMTP('smtp.office365.com', 587)
        server.starttls()
        server.login(smtp_username, smtp_password)
        server.sendmail(smtp_username, email, msg.as_string())
        server.quit()
        return True
    except Exception as e:
        print(f"Erreur email : {e}")
        return False

# 🔹 Interface Streamlit
def main():
    st.set_page_config(page_title="Malovelycar - Payer ma Réservation", page_icon="🚗", layout="wide")
    st.markdown("<style>/* Ton CSS ici */</style>", unsafe_allow_html=True)

    for key in ['reservation_details', 'payment_link', 'last_reference']:
        st.session_state.setdefault(key, None)

    st.markdown("### 🔍 Recherche de votre Réservation")
    reference = st.text_input("Entrez la référence :", value=st.session_state.last_reference or "")
    if reference:
        st.session_state.last_reference = reference
        with st.spinner("Recherche en cours..."):
            details, errors = fetch_reservation_details(reference)
            if details:
                st.session_state.reservation_details = details
                st.write(details)
                try:
                    days = int(details.get('Durée', '0').replace(" jours", "").strip())
                    cartage_price = days * 5
                    raw_price = details.get('Balance', '0').replace("€", "").replace(",", ".").strip()
                    product_price = abs(float(raw_price)) - cartage_price
                    link = create_payment_link(reference, product_price, 1, cartage_price, 0.2)
                    st.session_state.payment_link = link
                    st.markdown(f"### 💳 Montant total à régler : {product_price + cartage_price:.2f} €")
                    st.markdown(f"""<a href="{link}" target="_blank"><img src="https://raw.githubusercontent.com/Lamine-admin/Thecarsociety_images/main/Payer_reservation_logo