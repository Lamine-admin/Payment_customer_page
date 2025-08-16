import stripe
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from connected_accounts_list import connected_accounts # Importer les comptes connectés depuis le fichier
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
from decouple import config
import streamlit as st
import os
import sys
import logging
import time
from functools import lru_cache

# 🔹 Configuration du logger
logger = logging.getLogger("ConsoleLogger")
logger.setLevel(logging.DEBUG)

# Forcer un redémarrage de l'application
print("🔄 Redémarrage de l'application...")

file_handler = logging.FileHandler("console_and_errors.log", encoding="utf-8")
console_handler = logging.StreamHandler(sys.stdout)  # sys.stdout supporte souvent UTF-8
file_handler.setLevel(logging.DEBUG)
console_handler.setLevel(logging.DEBUG)

file_formatter = logging.Formatter('%(asctime)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
console_formatter = logging.Formatter('%(message)s')

file_handler.setFormatter(file_formatter)
console_handler.setFormatter(console_formatter)

logger.addHandler(file_handler)
logger.addHandler(console_handler)

# 🔹 Remplacement de print par logger
def my_print(message):
    logger.info(message)

print = my_print

# 🔹 Chargement des clés API et credentials
try:
    stripe.api_key = config('STRIPE_API_KEY')
    smtp_username = config('SMTP_USERNAME')
    smtp_password = config('SMTP_PASSWORD')
    fleetee_username = config('FLEETEE_USERNAME')
    fleetee_password = config('FLEETEE_PASSWORD')
except Exception as e:
    st.error("❌ Erreur de configuration : Veuillez vérifier les variables d'environnement")
    st.stop()

# 🔹 Fonction pour initialiser le navigateur (mise en cache)
@st.cache_resource
def get_browser():
    try:
        print("Initialisation du navigateur...")
        options = webdriver.ChromeOptions()
        options.add_argument('--headless')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--disable-gpu')
        options.add_argument('--window-size=1920,1080')
        options.add_argument('--disable-extensions')
        options.add_argument('--disable-infobars')
        options.add_argument('--disable-notifications')
        options.add_argument('--disable-popup-blocking')
        options.add_argument('--disable-blink-features=AutomationControlled')
        options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36')
        
        # Utilise la détection automatique de la version
        service = Service(ChromeDriverManager(version="120.0.6099.224").install())
        _driver = webdriver.Chrome(service=service, options=options)
        print("Navigateur initialisé avec succès")
        return _driver
    except Exception as e:
        print(f"❌ Erreur d'initialisation du navigateur : {e}")
        return None

# 🔹 Connexion à Fleetee (mise en cache)
@st.cache_data(ttl=3600)  # Cache pour 1 heure
def login_to_fleetee(_driver):  # Ajout du underscore pour éviter le hachage
    try:
        print("Connexion à Fleetee...")
        _driver.get('https://app.fleetee.io/login')
        
        # Attendre que les champs de connexion soient chargés
        email_field = WebDriverWait(_driver, 10).until(
            EC.presence_of_element_located((By.ID, "email"))
        )
        password_field = _driver.find_element(By.ID, "outlined-adornment-password")
        
        # Remplir les champs
        email_field.send_keys(fleetee_username)
        password_field.send_keys(fleetee_password)
        password_field.send_keys(webdriver.Keys.RETURN)
        
        # Attendre que la page de tableau de bord soit chargée
        WebDriverWait(_driver, 10).until(
            EC.url_contains("/dashboard")
        )
        return True
    except Exception as e:
        print(f"Erreur de connexion à Fleetee : {e}")
        return False

# 🔹 Fonction pour récupérer les détails de réservation (mise en cache)
@st.cache_data(ttl=300)  # Cache pour 5 minutes
def fetch_reservation_details(reference):
    errors = []
    _driver = get_browser()
    if _driver is None:
        errors.append("Le service de récupération automatique est temporairement indisponible. Merci de contacter le support ou réessayer plus tard.")
        return None, errors

    try:
        # Connexion à Fleetee si nécessaire
        if not login_to_fleetee(_driver):
            errors.append("Échec de la connexion à Fleetee")
            return None, errors

        print("Accès à la page des réservations...")
        # Utiliser une limite élevée pour afficher toutes les références en une fois
        _driver.get('https://app.fleetee.io/thecarsociety/bookings?page=0&limit=500')
        
        # Attendre que la page soit chargée
        WebDriverWait(_driver, 10).until(
            EC.presence_of_element_located((By.XPATH, "//span[contains(text(), 'Terminée')]"))
        )

        # Attendre que la page soit complètement chargée
        print("Attente du chargement complet de la page...")
        time.sleep(5)  # Attendre 5 secondes pour s'assurer que toutes les données sont chargées
        
        # Vérifier que la page est bien chargée en cherchant un élément spécifique
        try:
            WebDriverWait(_driver, 10).until(
                EC.presence_of_element_located((By.CLASS_NAME, "pointer table_row_link"))
            )
            print("Page des réservations chargée avec succès")
        except Exception as e:
            print(f"Attention : La page n'est peut-être pas complètement chargée : {e}")
            time.sleep(3)  # Attendre encore 3 secondes si nécessaire

        html_content = _driver.page_source
        soup = BeautifulSoup(html_content, 'html.parser')

        # Afficher tous les spans avec la classe 'pointer table_row_link' pour débogage
        # print("\nRecherche de tous les spans avec la classe 'pointer table_row_link':")
        all_spans = soup.find_all('span', {'class': 'pointer table_row_link'})
        print(f"Nombre total de références trouvées : {len(all_spans)}")
        for span in all_spans:
            print(f"Span trouvé: {span.text.strip()}")

        # Recherche spécifique de la référence dans les spans avec la classe 'pointer table_row_link'
        print(f"\nRecherche de la référence : {reference}")
        specific_table = soup.find('span', {'class': 'pointer table_row_link'}, string=reference)
        
        if not specific_table:
            print(f"Référence non trouvée : {reference}")
            print("\nContenu de la page (premiers 1000 caractères):")
            print(html_content[:1000])
            errors.append(f"Aucune donnée trouvée pour la référence : {reference}")
            return None, errors

        print(f"Référence trouvée dans le span: {specific_table.text.strip()}")

        # Trouver la ligne parente
        row = specific_table.find_parent("tr")
        if not row:
            print("Ligne parente non trouvée")
            errors.append(f"Ligne introuvable pour la référence : {reference}")
            return None, errors

        # Extraire les détails
        cells = row.find_all("td")
        if not cells:
            print("Cellules non trouvées dans la ligne")
            errors.append("Format de données incorrect")
            return None, errors

        # Afficher les cellules trouvées pour débogage
        print(f"\nNombre de cellules trouvées : {len(cells)}")
        for i, cell in enumerate(cells):
            print(f"Cellule {i}: {cell.text.strip()}")

        # Créer le dictionnaire de détails
        headers = ["Référence", "Statut", "Bien", "Locataire", "Début", "Fin", "Durée", "Tarif", "Balance"]
        details = {}
        
        for i, header in enumerate(headers):
            if i < len(cells):
                if header == "Début":
                    # Extraction de la date et heure de début
                    start_date_span = cells[i].find("span", {"style": "white-space: nowrap;"})
                    if start_date_span:
                        details["Début"] = start_date_span.text.strip()
                        print(f"Début : {details['Début']}")

                    # Extraction des informations supplémentaires (Propriétaire)
                    sub_text = cells[i].find("div", class_="sub_value_text")
                    if sub_text:
                        raw_owner_info = sub_text.text.strip()
                        owner_info_cleaned = " ".join(raw_owner_info.split("] ")[1:])
                        details["Propriétaire"] = owner_info_cleaned
                        print(f"Propriétaire : {details['Propriétaire']}")

                elif header == "Fin":
                    # Extraction de la date et heure de fin
                    end_date_span = cells[i].find("span", {"style": "white-space: nowrap;"})
                    if end_date_span:
                        details["Fin"] = end_date_span.text.strip()
                        print(f"Fin : {details['Fin']}")
                else:
                    # Extraction générique pour les autres colonnes
                    details[header] = cells[i].text.strip()

        print(f"\nDétails extraits : {details}")
        return details, errors

    except Exception as e:
        print(f"Erreur générale : {e}")
        errors.append(f"Erreur générale : {e}")
        return None, errors

# 🔹 Fonction pour récupérer l'email du locataire (mise en cache)
@st.cache_data(ttl=300)  # Cache pour 5 minutes
def get_tenant_email(tenant_name):
    errors = []
    _driver = get_browser()
    
    try:
        # Connexion à Fleetee si nécessaire
        if not login_to_fleetee(_driver):
            errors.append("Échec de la connexion à Fleetee")
            print("🔴 Connexion échouée. Impossible de continuer.")
            return None, errors
        print("🟢 Connexion à Fleetee réussie.")

        # Accéder à la page des clients
        print("Accès à la page des clients...")
        _driver.get('https://app.fleetee.io/thecarsociety/customers?page=0&limit=500')
        
        # Attendre que la page soit chargée
        WebDriverWait(_driver, 20).until(
            EC.presence_of_element_located((By.XPATH, "//span[contains(text(), 'Email')]"))
        )
        print("🟢 Page des clients chargée.")

        # Analyse du contenu HTML
        html_content = _driver.page_source
        with open("clients_debug.html", "w", encoding="utf-8") as f:
            f.write(html_content)
        print("Contenu HTML écrit dans 'clients_debug.html' pour débogage.")

        soup = BeautifulSoup(html_content, 'html.parser')

        # Analyse et séparation du nom complet
        print(f"Analyse du nom complet : {tenant_name}")
        tenant_name_parts = tenant_name.split(" ")
        if len(tenant_name_parts) < 2:
            print(f"Format du nom complet invalide pour : {tenant_name}")
            errors.append(f"Format du nom complet invalide : {tenant_name}")
            return None, errors

        first_name = tenant_name_parts[0]
        last_name = " ".join(tenant_name_parts[1:])  # Gère les noms composés
        print(f"Prénom : {first_name}, Nom : {last_name}")

        # Recherche de la ligne contenant le prénom et le nom
        print("Recherche du prénom et du nom dans la table des clients...")
        rows = soup.find_all("tr")  # Toutes les lignes de la table

        for row in rows:
            cells = row.find_all("td")
            if not cells:
                continue  # Passer les lignes sans cellules

            # Extraction des colonnes associées
            print("Extraction des informations associées au prénom...")
            details = {}
            headers = [
            "Nom", "Prénom", "Type", "Date de Naissance", "Email", "Téléphone"
        ]

            for i, cell in enumerate(cells):
                if i < len(headers):  # Ne pas dépasser la longueur des headers
                    span = cell.find("span")
                    value = span.text.strip() if span else ""  # Récupérer le texte du span
                    details[headers[i]] = value
                    print(f"{headers[i]} : {value}")

            # Vérifier si la ligne correspond au locataire
            if details.get("Prénom") == first_name and details.get("Nom") == last_name:
                # Extraire l'adresse e-mail
                tenant_email = details.get("Email")
                if tenant_email:
                    print(f"🟢 Email trouvé : {tenant_email}")
                    return tenant_email, errors
                else:
                    print(f"🔴 Erreur : Adresse e-mail introuvable pour le locataire : {first_name} {last_name}")
                    errors.append(f"Adresse e-mail introuvable pour : {first_name} {last_name}")
                    return None, errors

        # Si aucun résultat n'est trouvé
        print(f"🔴 Aucune correspondance trouvée pour le locataire : {first_name} {last_name}")
        errors.append(f"Aucune correspondance trouvée pour : {first_name} {last_name}")
        return None, errors

    except Exception as e:
        errors.append(f"Erreur générale : {e}")
        print(f"🔴 Erreur générale : {e}")
        return None, errors

# 🔹 Fonction pour extraire les initiales du vendeur à partir du nom du produit
@lru_cache(maxsize=100)
def get_seller_initials(reference):
    if not reference:
        st.error("❌ La référence ne peut pas être vide")
        return None
    parts = reference.split('-')
    if len(parts) >= 2:
        return parts[1]
    else:
        st.error(f"❌ Format de référence incorrect : {reference}. Le format attendu est XXX-YY-ZZZ")
        return None

# 🔹 Fonction pour créer un produit Stripe dans un compte connecté (mise en cache)
@st.cache_data(ttl=3600)  # Cache pour 1 heure
def create_product_on_connected_account(product_name, product_price, connected_account_id):
    try:
        print(f"Création du produit '{product_name}' pour le compte connecté : {connected_account_id}")
        product = stripe.Product.create(
            name=product_name,
            default_price_data={
                'unit_amount': int(abs(product_price) * 100),
                'currency': 'eur'
            },
            stripe_account=connected_account_id
        )
        print(f"Produit créé avec succès : {product['name']} (ID : {product['id']})")
        return product['default_price']
    except Exception as e:
        print(f"Erreur lors de la création du produit : {e}")
        return None

# 🔹 Fonction pour créer un lien de paiement Stripe
def create_payment_link(reference, product_price, quantity, cartage_price=0, commission_rate=0.2):
    seller_initials = get_seller_initials(reference)
    seller_account_info = connected_accounts.get(seller_initials)

    if not seller_account_info:
        raise ValueError(f"Aucun compte vendeur trouvé pour les initiales : {seller_initials}")

    price_id = create_product_on_connected_account(reference, product_price, seller_account_info['id'])

    # Calcul de la commission de la plateforme
    commission_amount = int(product_price * quantity * commission_rate * 100)
    print(f"Commission de la plateforme : {commission_amount / 100:.2f} €")
    
    # Calcul du montant Cartage à déduire
    cartage_amount = int(cartage_price * 100)
    print(f"Montant Cartage à déduire : {cartage_amount / 100:.2f} €")
    
    # Montant total à déduire (commission + cartage)
    total_deduction = commission_amount + cartage_amount
    print(f"Montant total à déduire : {total_deduction / 100:.2f} €")

    # Création du lien de paiement avec des frais d'application détaillés
    payment_link = stripe.PaymentLink.create(
        line_items=[{
            'price': price_id,
            'quantity': quantity,
        }],
        metadata={
            'product_name': reference,
            'seller_initials': seller_initials,
            'cartage_amount': cartage_amount / 100,  # Stockage du montant Cartage dans les métadonnées
            'commission_amount': commission_amount / 100  # Stockage du montant commission dans les métadonnées
        },
        application_fee_amount=total_deduction,  # Déduction du montant total (commission + cartage)
        application_fee_percent=None,  # Désactiver le pourcentage de frais d'application
        stripe_account=seller_account_info['id']
    )
    
    print(f"Lien de paiement généré : {payment_link.url}")
    return payment_link.url

# 🔹 Fonction pour envoyer un email avec le lien de paiement
def send_payment_link(email, client_name, product_name, payment_link, product_price, car_identity, days_number, cartage_price, owner_info, start_date, end_date):
    """
    Envoie un e-mail contenant le lien de paiement et des détails de réservation.
    """
    smtp_server = 'smtp.office365.com'
    smtp_port = 587
    total_price = abs(product_price) + cartage_price  # Calcul de la différence
    smtp_username = config('SMTP_USERNAME')
    smtp_password = config('SMTP_PASSWORD')

    try:
        # Vérification de l'adresse e-mail avant l'envoi
        if not email or "@" not in email:
            print("Erreur : Adresse e-mail invalide.")
            return False

        msg = MIMEMultipart()
        msg['From'] = smtp_username
        msg['To'] = email
        msg['Subject'] = f"Malovelycar - {product_name} : Votre lien de paiement"

        # Contenu de l'e-mail
        body = f"""
        <html>
        <body>
            <p><img src="https://raw.githubusercontent.com/Lamine-admin/Thecarsociety_images/main/Background.JPG" alt="Background" style="width:800px;height:auto;"></p>
            <p>Bonjour <b>{client_name}</b>,</p>
            <p>Merci d'avoir choisi Malovelycar !</p>
            <p>Le montant total à régler (réservation + assurance) est de : <b>{total_price:.2f} €</b>.</p>
            <p>Cliquez sur "Payer ma réservation" pour effectuer le paiement de <b>{product_price:.2f} €</b> de votre réservation n°<b>{product_name}</b> du véhicule <b>{car_identity}</b> du <b>{owner_info}</b> :</p>
            <p><a href="{payment_link}"><img src="https://raw.githubusercontent.com/Lamine-admin/Thecarsociety_images/main/Payer_reservation_logo.png" alt="Lien de paiement" style="width:200px;height:auto;"></a></p>
            <p>Puis cliquez sur "Payer mon assurance" et renseignez les informations ci-dessous (temps estimé : 2 minutes) pour effectuer le paiement de <b>{cartage_price:.2f} €</b> de votre assurance :</p>
            <p><a href=" https://app.cartage.club/subscription"><img src="https://raw.githubusercontent.com/Lamine-admin/Thecarsociety_images/main/Payer_assurance_logo.png" alt="Lien de paiement" style="width:200px;height:auto;"></a></p>
            <p>1. Votre email ;</p>
            <p>2. Vos infos personnelles (prénom, nom, date de naissance, email et code postale) ;</p>
            <p>3. La plaque d'immatriculation du véhicule loué : <b>{car_identity}</b> ;</p>
            <p>4. Les infos du propriétaire (mail : contact@malovelycar.com, Prénom et NOM : <b>{owner_info}</b>) ;</p>
            <p>5. Les dates d'utilisation : du <b>{start_date}</b> au <b>{end_date}</b> ;</p>
            <p>6. Signez puis payez.</p>
            <p>Merci de votre confiance !</p>
            <p>Cordialement,</p>
            <p>L'équipe Malovelycar</p>
            <p><img src="https://raw.githubusercontent.com/Lamine-admin/Thecarsociety_images/main/Logos_MLC_TLC_combinés_2.PNG" alt="Logo Malovelycar" style="width:150px;height:auto;"></p>
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
        - Entrez la référence de votre réservation (Ex: LOC-P0001-2024-03-0001)
        - Les détails s'afficheront automatiquement
        
        2️⃣ **Vérification**
        - Contrôlez les informations
        
        3️⃣ **Règlez votre réservation**
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
                
                # # Récupération de l'email directement à partir de la page des clients
                # with st.spinner("📧 Recherche de votre adresse email..."):
                #     tenant_name = reservation_details['Locataire']
                #     tenant_email, errors = get_tenant_email(tenant_name)  # Cette fonction retourne tenant_email = details.get("Email")
                    
                #     if tenant_email:
                #         # L'email a été trouvé et correspond bien à celui extrait dans get_tenant_email
                #         st.session_state.tenant_email = tenant_email
                #         # st.success(f"📧 Votre adresse email : {tenant_email}")
                #     else:
                #         # Gestion des erreurs : afficher des messages détaillés
                #         st.error("❌ Impossible de récupérer votre adresse email.")
                #         if errors:
                #             for error in errors:
                #                 st.warning(f"🔍 Détail de l'erreur : {error}")
                
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
                        commission_rate=0.2
                    )
                    print(f"Lien de paiement généré : {payment_link}")

                    if payment_link:
                        st.session_state.payment_link = payment_link
                        st.markdown(f"### Montant total à régler (réservation + assurance) : {abs(product_price + cartage_price):.2f} €")
                        # st.markdown(f"[Cliquez ici pour payer votre réservation]({payment_link}) : {product_price:.2f} €")
                        # st.markdown(f"[Cliquez ici pour souscrire et payer votre assurance Cartage](https://app.cartage.club/subscription) : {cartage_price:.2f} €")
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
                        # Section d'envoi d'email
                        # st.markdown("### 📤 Envoi du Lien de Paiement")
                        
                        # if not st.session_state.email_sent:
                        #     st.info("ℹ️ Le lien de paiement est prêt à être envoyé au locataire.")
                            
                        #     if st.button("📤 Envoyer le lien au locataire", key="send_email", help="Cliquez pour envoyer le lien de paiement par email"):
                        #         with st.spinner("📨 Envoi de l'email en cours..."):
                        #             try:
                        #                 owner_info = reservation_details.get("Propriétaire", "Propriétaire inconnu")
                        #                 send_payment_link(
                        #                     email=st.session_state.tenant_email,
                        #                     client_name=reservation_details['Locataire'],
                        #                     product_name=reference,
                        #                     payment_link=st.session_state.payment_link,
                        #                     product_price=product_price,
                        #                     car_identity=reservation_details['Bien'],
                        #                     days_number=days_number,
                        #                     cartage_price=cartage_price,
                        #                     owner_info=owner_info,  # Utilisation de la valeur par défaut si absent
                        #                     start_date=reservation_details['Début'],
                        #                     end_date=reservation_details['Fin']
                        #                 )
                        #                 st.session_state.email_sent = True
                        #                 st.success("✅ Email envoyé avec succès !")
                        #                 st.balloons()
                        #             except Exception as e:
                        #                 st.error(f"❌ Erreur lors de l'envoi de l'email : {str(e)}")
                        # else:
                        #     st.success("✅ L'email a déjà été envoyé pour cette réservation")
            else:
                st.error("❌ Aucune réservation trouvée avec cette référence")
                if errors:
                    for error in errors:
                        st.warning(error)

if __name__ == "__main__":
    main()
