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
def create_payment_link(reference, product_price, quantity, cartage_price, commission_rate=0.1):
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

# # 🔹 Interface Streamlit
# def main():
#     # Configuration de la page
#     st.set_page_config(
#         page_title="Malovelycar - Payer ma Réservation",
#         page_icon="🚗",
#         layout="wide"
#     )
#     # 🔹 Bandeau image en haut de page
#     st.markdown("""
#     <div style="text-align:left; margin-bottom:1rem;">
#         <img src="https://raw.githubusercontent.com/Lamine-admin/Thecarsociety_images/main/Logos_MLC_TLC_combinés_2.PNG" 
#              alt="Bandeau Malovelycar" 
#              style="width:40%; max-height:200px; object-fit:cover; border-radius:5px;">
#     </div>
#     """, unsafe_allow_html=True)
#     # Style CSS personnalisé
#     st.markdown("""
#         <style>
#         .main {
#             padding: 2rem;
#         }
#         /* Style du bouton principal */
#         .stButton > button {
#             background-color: #8F93FF !important;   /* Couleur de fond */
#             color: white !important;                /* Couleur du texte */
#             border: none !important;
#             border-radius: 8px !important;
#             padding: 0.75rem 1.5rem !important;
#             font-weight: 600 !important;
#             font-size: 1.1rem !important;
#             transition: background-color 0.3s ease, transform 0.1s ease;
#             box-shadow: 0 4px 6px rgba(0,0,0,0.1);
#             width: 100%;
#             height: 1em;
#             margin: 2rem 0;
#             align-items: center;
#         }  
                      
#         /* Effet au survol */
#         .stButton > button:hover {
#             background-color: #6C70E0 !important;
#             cursor: pointer;
#         }
        
#         /* Effet au clic */
#         .stButton > button:active {
#             background-color: #4E52C0 !important;
#             transform: scale(0.98);
#         }
                
#         /* Style général des champs de saisie */
#         input[type="text"] {
#             width: 600px;
#             background-color: white !important;
#             border: 2px solid #ccc;
#             border-radius: 6px;
#             padding: 6px;
#             text-align: center;
#             transition: box-shadow 0.3s ease;
#             max-width: 100%;
#             margin: auto;
#             display: block;
#         }
                
#         # Zone de texte (multiligne)
#         .textarea {
#             width: 100%;
#             max-width: 90%;
#             padding: 0.5rem;
#             border-radius: 5px;
#             border: 1px solid #ccc;
#             font-size: 1rem;
#             resize: vertical;
#         }
#         .info-box {
#             background-color: #f0f2f6;
#             padding: 1rem;
#             border-radius: 5px;
#             margin: 1rem 0;
#         }
#         .success-box {
#             background-color: #d1e7dd;
#             padding: 1rem;
#             border-radius: 5px;
#             margin: 1rem 0;
#         }
#         .warning-box {
#             background-color: #fff3cd;
#             padding: 1rem;
#             border-radius: 5px;
#             margin: 1rem 0;
#         }
#         h1 {
#             color: #1E3C72;
#             margin-bottom: 2rem;
#         }
#         h2 {
#             color: #2E5090;
#             margin: 1rem 0;
#         }
#         .step-box {
#             border: 1px solid #e0e0e0;
#             border-radius: 10px;
#             padding: 1.5rem;
#             margin: 1rem 0;
#             background-color: white;
#             box-shadow: 0 2px 4px rgba(0,0,0,0.1);
#         }
#         </style>
#     """, unsafe_allow_html=True)

#     # Initialisation des variables de session
#     if 'reservation_details' not in st.session_state:
#         st.session_state.reservation_details = None
#     if 'tenant_email' not in st.session_state:
#         st.session_state.tenant_email = None
#     if 'payment_link' not in st.session_state:
#         st.session_state.payment_link = None
#     if 'email_sent' not in st.session_state:
#         st.session_state.email_sent = False
#     if 'last_reference' not in st.session_state:
#         st.session_state.last_reference = None

#     # En-tête
#     col1, col2, col3 = st.columns([1, 2, 1])
#     with col2:
#         # Titre responsive
#         st.markdown("""
#         <style>
#             .responsive-title {
#                 display: flex;
#                 justify-content: center;
#                 align-items: center;
#                 white-space: nowrap;
#                 overflow: hidden;
#                 text-overflow: ellipsis;
#                 font-size: calc(1rem + 0.5vw);
#                 max-width: 100%;
#                 padding: 0.5rem;
#                 box-sizing: border-box;
#                 font-weight: bold;
#                 color: #333;
#                 font-family: 'Roboto', sans-serif;
#                 margin-bottom: -7rem;
#             }

#             @media (max-width: 400px) {
#                 .responsive-title {
#                     font-size: 0.9rem;
#                 }
#             }
#         </style>
#         """, unsafe_allow_html=True)

#     # Guide rapide dans la barre latérale
#     with st.sidebar:
#         st.markdown("### 📌 Guide Rapide")
#         st.markdown("""
#         1️⃣ **Recherche de votre réservation**
#         - Entrez la référence de votre réservation reçu par mail (Ex: LOC-P0001-2024-03-0001)
#         - Les détails s'afficheront automatiquement (moins d'une minute d'attente)
                    
#         2️⃣ **Vérification**
#         - Contrôlez les informations affichées

#         3️⃣ **Règlez votre réservation et votre assurance**
#         - Cliquez sur les boutons de paiement
#         - Les informations à fournir pour souscrire à l'assurance Cartage sont affichées dans les détails de la réservation
        
#         ❓ **Besoin d'aide ?**
#         - Email : contact@malovelycar.com
#         - Tél : +33 7 53 50 82 27
#         """)

#     # 🔍 Section principale : Recherche de réservation
#     # st.markdown("### 🔍 Recherche de votre Réservation")
    
#     with st.container():
#         col1, col2 = st.columns([3, 1])
#         with col1:


#             # Afficher le texte
#             st.markdown('<div class="responsive-title">Payer ma Réservation</div>', unsafe_allow_html=True)
#             st.markdown("""
#             <style>            
#                 /* Centrage du formulaire */
#                 .form-wrapper {
#                     display: flex;
#                     flex-direction: column;
#                     align-items: center;
#                     margin-top: 1rem;
#                     padding: 0rem;
#                     margin: 2rem 0;
#                 }
            
#                 /* Effet halo au focus */
#                 input[type="text"]:focus, textarea:focus {
#                     outline: none;
#                     border-color: #8F93FF;
#                     box-shadow: 0 0 8px 2px #8F93FF;
#                 }
#                 /* Réduction de l'espace entre les champs text_input */
#                 div[data-testid="stTextInput"] {
#                     margin-bottom: -2rem;
#                 }
#             </style>
#             """, unsafe_allow_html=True)
#             st.markdown('<div class="form-wrapper">', unsafe_allow_html=True)
#             reference = st.text_input(
#                 label="Référence de réservation*",
#                 placeholder="Exemple : LOC-P0001-2024-03-0001",
#                 value=st.session_state.last_reference if st.session_state.last_reference else "",
#                 help="la référence se trouve dans l'email de confirmation de réservation que vous avez reçu"
#             )
    
#             nom_famille = st.text_input(
#                 label="Nom de famille*",
#                 placeholder="Exemple : Dupont",
#                 value=st.session_state.nom_famille if 'nom_famille' in st.session_state else ""
#             )
    
#             # Bouton activé uniquement si les deux champs sont remplis
#             if reference.strip() and nom_famille.strip():
#                 bouton_recherche = st.button("💳 Générer le lien de paiement")
#             else:
#                 st.button("💳 Générer le lien de paiement", disabled=True)
#             st.markdown('</div>', unsafe_allow_html=True)

# 🔹 Interface Streamlit
def main():
    # Configuration de la page
    st.set_page_config(
        page_title="Malovelycar - Payer ma Réservation",
        page_icon="🚗",
        layout="wide"
    )

    # 🔹 Bandeau image en haut de page
    st.markdown("""
    <div style="text-align:left; margin-bottom:1rem;">
        <img src="https://raw.githubusercontent.com/Lamine-admin/Thecarsociety_images/main/Logos_MLC_TLC_combinés_2.PNG" 
             alt="Bandeau Malovelycar" 
             style="width:40%; max-height:200px; object-fit:cover; border-radius:5px;">
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
        # Titre responsive
        st.markdown("""
        <style>
            .responsive-title {
                display: flex;
                justify-content: center;
                align-items: center;
                white-space: nowrap;
                overflow: hidden;
                text-overflow: ellipsis;
                font-size: calc(1rem + 0.5vw);
                max-width: 100%;
                padding: 0.5rem;
                box-sizing: border-box;
                font-weight: bold;
                color: #333;
                font-family: 'Roboto', sans-serif;
                margin-bottom: -7rem;
            }

            @media (max-width: 400px) {
                .responsive-title {
                    font-size: 0.9rem;
                }
            }
        </style>
        """, unsafe_allow_html=True)
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

    # Afficher le texte
    st.markdown('<div class="responsive-title">Payer ma Réservation</div>', unsafe_allow_html=True)
    st.markdown("""
    <style>            
        /* Centrage du formulaire */
        .form-wrapper {
            display: flex;
            flex-direction: column;
            align-items: center;
            margin-top: 1rem;
            padding: 0rem;
            margin: 2rem 0;
        }
    </style>
    """, unsafe_allow_html=True)

    # Initialisation des variables
    if 'last_reference' not in st.session_state:
        st.session_state.last_reference = ""
    if 'nom_famille' not in st.session_state:
        st.session_state.nom_famille = ""

    # Formulaire
    st.markdown('<div class="form-wrapper">', unsafe_allow_html=True)

    reference = st.text_input(
        label="Référence de réservation*",
        placeholder="Exemple : LOC-P0001-2024-03-0001",
        value=st.session_state.last_reference,
        help="La référence se trouve dans l'email de confirmation de réservation que vous avez reçu"
    )

    nom_famille = st.text_input(
        label="Nom de famille*",
        placeholder="Exemple : Dupont",
        value=st.session_state.nom_famille
    )

    # Initialisation
    if 'bouton_recherche' not in st.session_state:
        st.session_state.bouton_recherche = False

    # Bouton activé uniquement si les deux champs sont remplis
    if (reference or "").strip() and (nom_famille or "").strip():
        if st.button("💳 Générer le lien de paiement"):
            st.session_state.bouton_recherche = True
    else:
        st.button("💳 Générer le lien de paiement", disabled=True)

    # Traitement après clic
    if st.session_state.bouton_recherche:
        st.session_state.last_reference = reference
        st.session_state.nom_famille = nom_famille

    # # Bouton activé uniquement si les deux champs sont remplis
    # if (reference or "").strip() and (nom_famille or "").strip():
    #     if st.button("💳 Générer le lien de paiement"):
    #         st.session_state.bouton_recherche = True
    # else:
    #     st.button("💳 Générer le lien de paiement", disabled=True)
    # st.markdown('</div>', unsafe_allow_html=True)



    # if st.session_state.bouton_recherche == True:
    #     st.session_state.last_reference = reference
    #     st.session_state.nom_famille = nom_famille

        with st.spinner("Génération du lien de paiement en cours (temps estimé : 30 secondes)..."):
            reservation_details, errors = fetch_reservation_details(reference)

            if reservation_details:
                # Vérification du nom de famille
                if nom_famille.lower() in reservation_details['Locataire'].lower():
                    st.session_state.reservation_details = reservation_details
                # Affichage des détails dans un conteneur stylisé
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
                            commission_rate=0.0
                        )
                        print(f"Lien de paiement généré : {payment_link}")

                        if payment_link:
                            st.session_state.payment_link = payment_link
                            st.markdown(f"### Montant total à régler (réservation + assurance) : {abs(product_price + cartage_price):.2f} €")

                            safe_payment_link = quote(payment_link, safe=':/?&=')

                            st.markdown(
                                f'''
                                <div style="margin-bottom:1em;">
                                    <div><strong>Montant de votre réservation : {product_price:.2f} €</strong></div>
                                    <div>
                                        <a href="{safe_payment_link}" target="_blank">
                                            <img src="https://raw.githubusercontent.com/Lamine-admin/Thecarsociety_images/main/Payer_reservation_logo.png" alt="Payer ma réservation" style="width:200px;height:auto;">
                                        </a>
                                    </div>
                                </div>
                                ''',
                                unsafe_allow_html=True
                            )

                            st.markdown(
                                '''
                                <div style="margin-bottom:1em;">
                                    <div><strong>Montant de votre assurance Cartage : {:.2f} €</strong></div>
                                    <div>
                                        <a href="https://app.cartage.club/subscription" target="_blank">
                                            <img src="https://raw.githubusercontent.com/Lamine-admin/Thecarsociety_images/main/Payer_assurance_logo.png" alt="Payer mon assurance" style="width:200px;height:auto;">
                                        </a>
                                    </div>
                                </div>
                                '''.format(cartage_price),
                                unsafe_allow_html=True
                            )

                else:
                    st.error("❌ Votre réservation est introuvable. Veuillez vérifier les informations que vous avez saisies et réessayer.")
            else:
                st.error("❌ Votre réservation est introuvable. Veuillez vérifier les informations que vous avez saisies et réessayer.")
                for error in errors:
                    st.warning(error)                

if __name__ == "__main__":
    main()