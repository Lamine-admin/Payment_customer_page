import streamlit as st
import stripe
import requests
from decouple import config
from urllib.parse import quote
import logging

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
    fleetee_username = config('FLEETEE_USERNAME')
    fleetee_password = config('FLEETEE_PASSWORD')
    fleetee_api_key = config('FLEETEE_API_KEY')
except Exception:
    st.error("❌ Erreur de configuration : variables d'environnement manquantes.")
    st.stop()

# 🔹 Utilitaire pour requêtes API Fleetee
FLEETEE_API_BASE = "https://api.fleetee.io"

def fleetee_api_get(endpoint, params=None):
    headers = {
        "x-api-key": "68wVogXBjdkHSEYCzMVaLVnMuUEp785WSP6TVH20",
        "secret-key": "sk_77dc45c5e9ded921fe328b3087b15527"
    }
    url = f"{FLEETEE_API_BASE}{endpoint}"
    response = requests.get(url, headers=headers, params=params)
    print(response.status_code)
    print(response.text)
    if response.status_code == 200:
        return response.json(), None
    else:
        return None, f"Erreur API Fleetee: {response.status_code} {response.text}"

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
        "Balance": booking.get("total_amount"),
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

# 🔹 Interface Streamlit
def main():
    st.set_page_config(
        page_title="Malovelycar - Payer ma Réservation",
        page_icon="🚗",
        layout="wide"
    )

    # Bandeau image
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
        
        st.markdown('<div class="responsive-title">Payer ma Réservation</div>', unsafe_allow_html=True)

    # Sidebar
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

    # Formulaire
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

    # Traitement après clic
    if st.session_state.bouton_recherche:
        st.session_state.last_reference = reference
        st.session_state.nom_famille = nom_famille

        with st.spinner("Génération du lien de paiement en cours (temps estimé : 20 secondes)..."):
            reservation_details, errors = fetch_reservation_details(reference)
        payment_link = None
        if errors:
            st.error("\n".join(errors))
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

            try:
                days_number = int(str(reservation_details['Durée']).replace(" jours", "").strip())
                cartage_price = days_number * 5
                product_price = abs(float(str(reservation_details['Balance']).replace("€", "").replace(",", ".").strip()))
                quantity = 1
            except ValueError as e:
                st.error(f"Erreur lors du calcul du tarif : {e}")
                return

            payment_link = create_payment_link(
                reference=reservation_details['Référence'],
                product_price=product_price,
                quantity=quantity,
                cartage_price=cartage_price,
                commission_rate=0.3
            )

        if payment_link:
            st.session_state.payment_link = payment_link
            st.markdown(f"### Montant de la réservation à régler : {abs(product_price):.2f} €")
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

if __name__ == "__main__":
    main()
