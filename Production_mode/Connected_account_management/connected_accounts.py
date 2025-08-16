import stripe
from decouple import config

# Clé API secrète de Stripe
stripe.api_key = config('STRIPE_API_KEY')  # Clé API Stripe principale

# Fonction pour récupérer les comptes connectés et les stocker dans un fichier
def save_connected_accounts():
    accounts = stripe.Account.list()
    connected_accounts = {}
    
    # Créer une liste des comptes connectés et l'inverser
    accounts_list = [account for account in accounts.auto_paging_iter()]
    accounts_list.reverse()  # Inverser l'ordre de la liste des comptes

    index = 1
    for account in accounts_list:
        seller_initials = f'P{index:04d}'  # Format des initiales (PXXXX)
        products = stripe.Product.list(expand=['data.default_price'], stripe_account=account.id)
        product_list = [{'id': product.id, 'name': product.name, 'price_id': product.default_price.id if product.default_price else None} for product in products.auto_paging_iter()]
        connected_accounts[seller_initials] = {
            'id': account.id,
            'email': account.email,
            'products': product_list
        }
        print(f"Initials: {seller_initials}, Account ID: {account.id}, Email: {account.email}, Products: {product_list}")
        index += 1

    # Sauvegarde dans un fichier
    with open('connected_accounts_list.py', 'w') as f:
        f.write('connected_accounts = ')
        f.write(str(connected_accounts))

if __name__ == "__main__":
    save_connected_accounts()
