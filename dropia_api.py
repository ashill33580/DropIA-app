
# dropia_api.py

from fastapi import FastAPI, HTTPException, Header, Depends, Request
from pydantic import BaseModel, Field
import openai
import os
import uvicorn
import nest_asyncio
# from google.colab import userdata # Commented out as userdata is Colab-specific
import sqlite3
import paypalrestsdk
import json
from typing import List, Optional
# from paypalcheckoutsdk.notifications.webhooks import VerificationApi, GenerateWebhookEventRequest # Commented out as paypal-checkout-sdk might not be installed
# from paypalhttp import HttpHeaders # Commented out as paypal-checkout-sdk might not be installed
# from pyngrok import ngrok # Import ngrok - Commented out as ngrok is for local testing


nest_asyncio.apply()

app = FastAPI()


# Configuration OpenAI - Read from environment variable
openai.api_key = os.getenv("OPENAI_API_KEY")
if not openai.api_key:
    # Consider using python-dotenv for local development, but for Cloud Run, direct env vars are used.
    # For local testing without dotenv, you'd need to set the env var manually before running.
    print("Attention: La variable d'environnement OPENAI_API_KEY n'est pas définie.")
    # In a production API, you might want to raise an error or handle this more gracefully
    # raise ValueError("La variable d'environnement OPENAI_API_KEY n'est pas définie.")


# Configuration PayPal - Read from environment variables
PAYPAL_CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID")
PAYPAL_CLIENT_SECRET = os.getenv("PAYPAL_CLIENT_SECRET")
PAYPAL_WEBHOOK_ID = os.getenv("PAYPAL_WEBHOOK_ID") # Read Webhook ID from env var

if not PAYPAL_CLIENT_ID or not PAYPAL_CLIENT_SECRET:
     print("Attention: Les clés API PayPal ne sont pas définies dans les variables d'environnement. L'intégration PayPal ne fonctionnera pas.")
if not PAYPAL_WEBHOOK_ID:
     print("Attention: L'ID du Webhook PayPal n'est pas défini dans les variables d'environnement. La validation des webhooks ne fonctionnera pas.")


# Database Configuration
# WARNING: SQLite on Cloud Run's ephemeral filesystem is NOT suitable for persistent production data.
# Consider migrating to Google Cloud SQL or another persistent database for production.
DATABASE_URL = "dropia.db"

# Ensure the database file exists and the table is created on startup
# This is a workaround for ephemeral storage - data will be reset on new instances
def init_db():
    conn = sqlite3.connect(DATABASE_URL)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            api_key TEXT PRIMARY KEY,
            role TEXT NOT NULL,
            subscription_status TEXT NOT NULL,
            plan TEXT,
            monthly_generations_count INTEGER DEFAULT 0,
            paypal_subscription_id TEXT, -- Added column for PayPal subscription ID
            store_assistance_used BOOLEAN DEFAULT FALSE
        )
    ''')
    conn.commit()
    conn.close()

# Initialize the database when the app starts
# NOTE: This init_db() call here will create/update the DB file in your Drive
# if you run this cell. On Cloud Run, this part runs when the container starts.
# For deployment, you need to ensure your Cloud Run service can write to /app
# or use a persistent volume if you need the DB to survive container restarts.
# For this deployment step, the focus is getting the code files to GitHub.
# init_db() # Commenting this out to avoid creating/modifying dropia.db in Drive during file generation


def get_db():
    conn = sqlite3.connect(DATABASE_URL)
    conn.row_factory = sqlite3.Row
    try:
        yield conn # Corrected 'connconng' to 'conn'
    finally:
        conn.close()

subscription_plans = {
    "Gratuit": {
        "description": "Accès limité à la génération d'idées de produits.",
        "monthly_generations_limit": 5,
        "features": ["Génération de produit de base"]
    },
    "Premium": {
        "description": "Accès illimité à la génération d'idées et fonctionnalités supplémentaires.",
        "monthly_generations_limit": -1,
        "features": ["Génération de produit illimitée", "Accès aux fonctionnalités premium", "Assistance IA à la création de boutique"],
        "paypal_plan_id": "P-14D73578B05390914NCFMMSI" # Votre ID de plan PayPal - REMPLACEZ CECI
    }
}

class ProductPrompt(BaseModel):
    niche: str
    persona: str
    num_ideas: int = Field(1, description="Le nombre d'idées de produits à générer (entre 1 et 5, selon le plan).")
    # Add optional parameters for OpenAI API
    temperature: float = Field(0.9, description="Contrôle le caractère aléatoire de la sortie (entre 0.0 et 2.0). Des valeurs plus élevées rendent la sortie plus aléatoire.")
    top_p: float = Field(1.0, description="Alternative à l'échantillonnage avec temperature. Le modèle ne considère que les tokens dont la probabilité cumulée dépasse top_p (entre 0.0 et 1.0).")
    frequency_penalty: float = Field(0.0, description="Diminue la probabilité que le modèle répète des tokens déjà présents dans la réponse (entre -2.0 et 2.0).")
    presence_penalty: float = Field(0.0, description="Diminue la probabilité que le modèle parle de nouveaux sujets (entre -2.0 et 2.0).")
    # Add optional parameter to specify desired fields
    fields: Optional[List[str]] = Field(None, description="Liste des champs souhaités dans la réponse (ex: ['nom_produit', 'description_courte']). Si vide ou nulle, tous les champs sont inclus.")

class StoreSetupPrompt(BaseModel):
    store_type: str = Field(..., description="Le type de boutique e-commerce (ex: dropshipping, marque privée, artisanale).")
    niche: str = Field(..., description="La niche principale de la boutique.")
    target_audience: str = Field(..., description="La description détaillée du public cible.")
    assistance_type: str = Field(..., description="Le type d'assistance IA demandé (ex: 'generate_about_us', 'suggest_branding', 'faq_content').")
    details: Optional[str] = Field(None, description="Détails supplémentaires ou contexte pour l'assistance demandée.")


class SubscribeRequest(BaseModel):
    plan_name: str

def get_current_user(api_key: str = Header(...), db: sqlite3.Connection = Depends(get_db)):
    cursor = db.cursor()
    cursor.execute('SELECT * FROM users WHERE api_key = ?', (api_key,))
    user = cursor.fetchone()
    if user is None:
        raise HTTPException(status_code=401, detail="Clé API invalide")
    return dict(user)

@app.post("/generate-product")
async def generate_product(data: ProductPrompt, current_user: dict = Depends(get_current_user), db: sqlite3.Connection = Depends(get_db)):
    user_plan = current_user.get("plan")
    plan_details = subscription_plans.get(user_plan)

    # Check if subscription is active (moved up for early exit)
    if current_user.get("subscription_status") != "active":
         raise HTTPException(status_code=403, detail="Abonnement inactif. Veuillez activer votre abonnement.")


    # Validate num_ideas based on plan limits (basic validation)
    # Limit free plan to 1 idea per request and check if assistance was already used
    if user_plan == "Gratuit":
        # The check for 'store_assistance_used' should apply to the *first* use of any store assistance,
        # including the single product generation allowed for free users as part of store setup help.
        # However, the prompt was for 'num_ideas', which is related to product generation, not store setup assistance.
        # Let's align this with the clarification markdown: Free users get 5 generations/month AND one *store setup assistance* (which could be a product fiche).
        # The current logic in the code seems to mix these.
        # Let's assume for now the 'store_assistance_used' flag is for the *special* store setup endpoint, not general product generation.
        # The monthly limit applies to /generate-product for ALL plans.
        # Re-evaluating the logic based on markdown cell 48b90d18:
        # - Gratuit: 5 monthly generations for /generate-product AND one-time assistance via /assist-store-setup (if that endpoint is used for the 'fiche produit ect...')
        # - Premium: Unlimited generations for /generate-product AND unlimited /assist-store-setup.

        # Based on this, the num_ideas limit validation should be separate from the store_assistance_used flag.
        # The store_assistance_used flag should be checked/set by the /assist-store-setup endpoint, not /generate-product.
        # Let's remove the store_assistance_used check from here and keep the monthly limit check.

        # Re-checking the num_ideas validation for the Free plan:
        # The markdown says "Accès limité à la génération d'idées de produits... monthly_generations_limit: 5".
        # It also says "Une seule génération complète de fiche produit" for the FREE store assistance.
        # This is slightly ambiguous. Let's assume the 5/month limit is for the /generate-product endpoint,
        # and the 'store_assistance_used' flag is for the *new* /assist-store-setup endpoint.
        # The num_ideas validation (limit 1 per request) should probably apply to the Free plan for /generate-product
        # to prevent a free user from burning their 5 generations in one request.

        if data.num_ideas > 1:
             raise HTTPException(status_code=400, detail=f"Le plan Gratuit est limité à 1 idée par requête pour la génération de produit.")
        # The check for 'store_assistance_used' for free users should be on the /assist-store-setup endpoint.


    elif plan_details and plan_details["monthly_generations_limit"] != -1 and current_user["monthly_generations_count"] + data.num_ideas > plan_details["monthly_generations_limit"]:
        remaining = plan_details["monthly_generations_limit"] - current_user["monthly_generations_count"]
        raise HTTPException(status_code=429, detail=f"Limite de génération ({plan_details['monthly_generations_limit']} par mois) atteinte pour votre plan {user_plan}. Vous pouvez encore générer {remaining} idée(s). Veuillez passer à un plan supérieur pour des générations illimitées.")


    try:
        # Define all possible fields and their descriptions for the prompt
        all_fields_description = {
            "nom_produit": "Un nom percutant, unique et facile à retenir pour ce marché",
            "description_courte": "1 à 3 phrases décrivant le produit et son principal avantage pour le persona, utilise un langage émotionnel et inclut un emoji pertinent",
            "accroche_marketing": "Une phrase courte et très attrayante pour les publicités ou les réseaux sociaux, utilise un verbe d'action",
            "avantages_client": ["Bénéfice clé 1 : Explique clairement ce que le client gagne.", "Bénéfice clé 2 : Un autre avantage concret.", "Bénéfice clé 3 : Un troisième bénéfice ou caractéristique différenciante."],
            "public_cible_specifique": "Décris en 1-2 phrases le sous-segment précis du persona ciblé par ce produit.",
            "probleme_resolu": "Décris en 1 phrase le problème spécifique que ce produit résout pour le persona.",
            "idee_prix": "Une fourchette de prix suggérée, justifiée brièvement (ex: 'Entre 30€ et 50€ - prix premium justifié par la qualité')"
        }

        # Determine which fields to include in the prompt based on the 'fields' parameter
        fields_to_include = data.fields if data.fields is not None and len(data.fields) > 0 else list(all_fields_description.keys())

        # Construct the JSON structure description for the prompt dynamically
        json_structure_description = '[
'
        json_structure_description += '  {
'
        for i, field in enumerate(fields_to_include):
            if field in all_fields_description:
                description = all_fields_description[field]
                # Handle list type fields for description
                if isinstance(description, list):
                     description_str = '[
' + ',
'.join([f'      "{item}"' for item in description]) + '
    ]'
                else:
                     description_str = f'"{description}"'
                json_structure_description += f'    "{field}": {description_str}'
                if i < len(fields_to_include) - 1:
                    json_structure_description += ',
'
                else:
                    json_structure_description += '
' # No comma after the last field
            else:
                # Handle case where a requested field is not defined
                print(f"Warning: Requested field '{field}' is not recognized and will be ignored.")
        json_structure_description += '  }
'
        json_structure_description += f'  // ... répéter pour {data.num_ideas} objets
'
        json_structure_description += ']'


        # Prompt to generate N ideas in JSON array format with specified fields
        prompt = (
            f"Tu es un expert en e-commerce avec une forte expertise en marketing de niche. "
            f"Génère {data.num_ideas} idées de produits innovantes et potentiellement très rentables, "
            f"spécifiquement conçues pour la niche '{data.niche}' et ciblant le persona détaillé suivant : '{data.persona}'. "
            "Chaque produit doit résoudre un problème ou répondre à un besoin spécifique de ce persona dans cette niche. "
            f"Fournis les informations structurées au format JSON uniquement. La sortie doit être un tableau JSON contenant {data.num_ideas} objets, chacun avec les clés suivantes et leurs valeurs correspondantes:
"
            f"{json_structure_description}" # Use the dynamically generated structure description
            "
Assure-toi que la sortie soit STRICTEMENT un tableau JSON valide et complet, SANS AUCUN texte supplémentaire avant ou après le tableau JSON."
        )

        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": prompt}],
            temperature=data.temperature, # Use parameter from request
            top_p=data.top_p, # Use parameter from request
            frequency_penalty=data.frequency_penalty, # Use parameter from request
            presence_penalty=data.presence_penalty, # Use parameter from request
            max_tokens=1500,
            n=1 # Always request 1 response from OpenAI, which should contain the JSON array
        )

        # Increment the generation count by the number of ideas requested
        new_count = current_user["monthly_generations_count"] + data.num_ideas
        cursor = db.cursor()
        cursor.execute('UPDATE users SET monthly_generations_count = ? WHERE api_key = ?', (new_count, current_user["api_key"]))
        # The store_assistance_used flag logic is moved to /assist-store-setup
        db.commit()
        print(f"Génération réussie pour l'utilisateur {current_user['api_key']}. Compteur incrémenté de {data.num_ideas}. Nouveau compteur: {new_count}")


        # Attempt to parse JSON response, handle potential errors
        try:
            ai_response_content = response.choices[0].message["content"]
            json_result = json.loads(ai_response_content)
            # Validate that the result is a list/array as requested
            if not isinstance(json_result, list):
                 print(f"Erreur: La réponse d'OpenAI n'est pas un tableau JSON comme attendu. Réponse reçue: {ai_response_content}")
                 # Attempt to salvage if it's a single JSON object instead of an array of one object
                 if isinstance(json_result, dict):
                      print("Tentative de traiter comme un objet JSON unique dans un tableau.")
                      json_result = [json_result]
                 else:
                      raise HTTPException(status_code=500, detail=f"La réponse d'OpenAI n'est pas au format tableau JSON attendu. Réponse reçue : {ai_response_content}")

            # Optional: Basic validation that the number of items matches num_ideas (OpenAI might not always comply perfectly)
            # if len(json_result) != data.num_ideas:
            #      print(f"Avertissement: Le nombre d'idées retournées par OpenAI ({len(json_result)}) ne correspond pas au nombre demandé ({data.num_ideas}).")


            return {"result": json_result}
        except json.JSONDecodeError:
            print(f"Erreur de décodage JSON de la réponse OpenAI. Réponse reçue : {ai_response_content}")
            raise HTTPException(status_code=500, detail=f"La réponse d'OpenAI n'était pas au format JSON attendu. Réponse reçue : {ai_response_content}")
        except Exception as json_err:
            print(f"Erreur inattendue lors du traitement de la réponse OpenAI : {json_err}")
            raise HTTPException(status_code=500, detail=f"Erreur interne lors du traitement de la réponse OpenAI : {json_err}")


    except Exception as e:
        print(f"Erreur lors de la génération du produit : {e}")
        raise HTTPException(status_code=500, detail=f"Une erreur est survenue lors de la génération de l'idée de produit : {str(e)}")

# New endpoint for AI store setup assistance (Premium only, Free one-time)
@app.post("/assist-store-setup")
async def assist_store_setup(data: StoreSetupPrompt, current_user: dict = Depends(get_current_user), db: sqlite3.Connection = Depends(get_db)):
    user_plan = current_user.get("plan")
    cursor = db.cursor() # Get cursor early to use in checks

    # Check if the user is on the Premium plan or Free
    if user_plan != "Premium" and user_plan != "Gratuit":
        raise HTTPException(status_code=403, detail=f"Cette fonctionnalité est réservée aux utilisateurs des plans Premium et Gratuit. Votre plan actuel est : {user_plan}.")

    # Check if the subscription is active
    if current_user.get("subscription_status") != "active":
         raise HTTPException(status_code=403, detail="Abonnement inactif. Veuillez activer votre abonnement.")

    # Check for Free plan one-time usage limit for this specific endpoint
    if user_plan == "Gratuit":
        if current_user.get("store_assistance_used", False):
             raise HTTPException(status_code=403, detail="Vous avez déjà utilisé votre assistance IA unique pour la création de boutique avec le plan Gratuit.")


    try:
        # Construct the prompt based on the assistance type requested
        base_prompt = (
            f"Tu es un expert en création de boutiques e-commerce pour la niche '{data.niche}' "
            f"et ciblant le public '{data.target_audience}'. "
            f"La boutique est de type '{data.store_type}'. "
        )

        if data.assistance_type == "generate_about_us":
            prompt = base_prompt + (
                "Génère un texte convaincant pour la page 'À propos de nous' de cette boutique. "
                "Le texte doit raconter l'histoire de la marque, expliquer sa mission, et créer un lien émotionnel avec le public cible. "
                f"Détails supplémentaires : {data.details if data.details else 'Aucun.'}"
            )
        elif data.assistance_type == "suggest_branding":
             prompt = base_prompt + (
                 "Suggère des idées de branding (nom de boutique, slogan, style visuel) pour cette boutique. "
                 "Fournis les suggestions dans un format clair et structuré. "
                 f"Détails supplémentaires : {data.details if data.details else 'Aucun.'}"
             )
        elif data.assistance_type == "faq_content":
             prompt = base_prompt + (
                 "Génère des questions-réponses courantes (FAQ) pertinentes pour les clients potentiels de cette boutique. "
                 "Structure la réponse en une liste de questions et leurs réponses. "
                 f"Détails supplémentaires : {data.details if data.details else 'Aucun.'}"
             )
        # Add more assistance types here as needed
        elif data.assistance_type == "generate_product_fiche":
             # This type is specifically for the Free user's one-time product fiche assistance
             prompt = base_prompt + (
                 "Génère une fiche produit détaillée pour un produit potentiel dans cette niche et pour ce public cible. "
                 "Inclure le nom du produit, une description détaillée, les avantages clés, une idée de prix, et une suggestion d'image. "
                 f"Détails supplémentaires : {data.details if data.details else 'Aucun.'}"
             )
        else:
            raise HTTPException(status_code=400, detail=f"Type d'assistance '{data.assistance_type}' non reconnu.")

        # Call OpenAI API
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo", # You might consider gpt-4 for more creative tasks
            messages=[{"role": "user", "content": prompt}],
            temperature=0.8, # Adjust temperature for creativity
            max_tokens=1500 # Increased max tokens based on expected response length
        )

        # If the user is on the Free plan and successfully used this endpoint, mark it as used
        if user_plan == "Gratuit":
             cursor.execute('UPDATE users SET store_assistance_used = ? WHERE api_key = ?', (True, current_user["api_key"]))
             db.commit()
             print("Assistance IA pour la création de boutique marquée comme utilisée pour l'utilisateur Gratuit.")


        # Return the generated content
        return {"result": response.choices[0].message["content"]}

    except Exception as e:
        print(f"Erreur lors de l'assistance à la création de boutique : {e}")
        raise HTTPException(status_code=500, detail=f"Une erreur est survenue lors de l'assistance à la création de boutique : {str(e)}")


@app.post("/subscribe")
async def create_subscription(data: SubscribeRequest, current_user: dict = Depends(get_current_user), db: sqlite3.Connection = Depends(get_db)):
    plan_name = data.plan_name
    plan_details = subscription_plans.get(plan_name)

    if not plan_details or "paypal_plan_id" not in plan_details:
        raise HTTPException(status_code=400, detail=f"Plan '{plan_name}' non trouvé ou non configurable via PayPal.")

    paypal_plan_id = plan_details["paypal_plan_id"]

    # Vérifiez si l'ID de plan PayPal est un placeholder
    if paypal_plan_id == "P-14D73578B05390914NCFMMSI": # REMPLACEZ CECI
        raise HTTPException(status_code=400, detail="L'ID du plan PayPal n'a pas été configuré. Veuillez créer le plan dans PayPal et mettre à jour l'ID dans le code.")


    try:

        # Configurez l'environnement PayPal (Sandbox pour les tests)
        # Utilisez LiveEnvironment pour la production
        from paypalrestsdk import Api as PayPalApi # Importation correcte de l'API

        # Assurez-vous que les clés sont définies avant d'initialiser
        if not PAYPAL_CLIENT_ID or not PAYPAL_CLIENT_SECRET:
             raise HTTPException(status_code=500, detail="Les clés API PayPal ne sont pas configurées dans les variables d'environnement du serveur.")

        # Initialiser l'API PayPal REST SDK
        paypalrestsdk.configure({
          "mode": "sandbox", # Ou "live" pour la production - ASSUREZ-VOUS QUE CECI CORRESPOND À VOS CLÉS
          "client_id": PAYPAL_CLIENT_ID,
          "client_secret": PAYPAL_CLIENT_SECRET })


        # Créez l'objet d'abonnement
        subscription = paypalrestsdk.Subscription({
            "plan_id": paypal_plan_id,
            "subscriber": {
                # Remplacez par les informations réelles de l'utilisateur si disponibles
                "name": {
                     "given_name": current_user.get("given_name", "Utilisateur"), # Assurez-vous d'ajouter ces champs à votre table users si nécessaire
                     "surname": current_user.get("surname", "Test")
                },
                "email_address": current_user.get("email", "utilisateur_test@example.com") # Assurez-vous d'ajouter un champ email à votre table users
            },
            "application_context": {
                # Remplacez par vos URLs de retour et d'annulation réelles
                "return_url": "YOUR_RETURN_URL", # URL où l'utilisateur est redirigé après approbation (succès) - REMPLACEZ CECI
                "cancel_url": "YOUR_CANCEL_URL",  # URL où l'utilisateur est redirigé après annulation - REMPLACEZ CECI
                "shipping_preference": "NO_SHIPPING_ADDRESS", # Ajustez selon besoin
                 # Inclure un identifiant unique de votre utilisateur ici pour le webhook si possible
                 # Vérifiez la documentation de l'API PayPal pour savoir où placer un custom_id ou équivalent pour les abonnements
                # "custom_id": str(current_user["api_key"]) # Ceci est un exemple, la localisation peut varier
            }
        })

        if subscription.create():
            print(f"Abonnement PayPal créé avec ID: {subscription.id}")
            cursor = db.cursor()

            # Stocker l'ID de l'abonnement PayPal et potentiellement d'autres infos (statut initial, plan demandé)
            # Assurez-vous que la colonne 'paypal_subscription_id' existe dans votre table users
            # Vous pourriez aussi stocker le statut initial comme 'pending' jusqu'au webhook d'activation
            cursor.execute('UPDATE users SET paypal_subscription_id = ?, subscription_status = ? WHERE api_key = ?', (subscription.id, 'pending', current_user["api_key"]))
            db.commit()
            print(f"PayPal Subscription ID {subscription.id} stored (status pending) for user {current_user['api_key']}")


            for link in subscription.links:
                if link.rel == "approve":
                    # Retourner l'URL d'approbation où l'utilisateur doit être redirigé
                    return {"paypal_approval_url": str(link.href)}
            # Si l'URL d'approbation n'est pas trouvée, c'est une erreur inattendue
            print("Erreur: URL d'approbation PayPal non trouvée dans la réponse de création d'abonnement.")
            raise HTTPException(status_code=500, detail="Erreur: Impossible d'obtenir l'URL d'approbation PayPal.")
        else:
             print(f"Erreur lors de la création de l'abonnement PayPal: {subscription.error}")
             # Loguez les détails de l'erreur PayPal si disponibles pour un meilleur diagnostic
             if hasattr(subscription.error, 'details'):
                 print(f"Détails de l'erreur PayPal: {subscription.error.details}")
             raise HTTPException(status_code=500, detail=f"Erreur PayPal: {subscription.error}")

    except Exception as e:
        print(f"Erreur lors de l'initiation de l'abonnement PayPal : {e}")
        # Gérer spécifiquement l'erreur si les clés API ne sont pas définies
        if "Les clés API PayPal ne sont pas configurées" in str(e):
             raise HTTPException(status_code=500, detail=str(e))
        # Gérer les erreurs liées à l'initialisation du SDK ou à la requête
        raise HTTPException(status_code=500, detail=f"Erreur lors de l'initiation de l'abonnement PayPal : {str(e)}")

@app.post("/webhooks/paypal")
async def paypal_webhook(request: Request, db: sqlite3.Connection = Depends(get_db)):
    # Pour une intégration robuste, VOUS DEVEZ VALIDER LE WEBHOOK.
    # Cela implique d'utiliser l'API de validation des webhooks de PayPal
    # en utilisant les en-têtes de la requête et le corps brut de la requête.
    # Vous aurez besoin de votre Webhook ID configuré dans PayPal.

    webhook_id = PAYPAL_WEBHOOK_ID # Votre ID de webhook PayPal
    request_headers = dict(request.headers)
    # Le corps de la requête doit être lu comme octets pour la validation
    request_body = await request.body()

    # Vérifiez si l'ID du Webhook PayPal est configuré
    if not webhook_id or webhook_id == "YOUR_PAYPAL_WEBHOOK_ID": # REMPLACEZ CECI
         print("Attention: L'ID du Webhook PayPal n'est pas configuré. Validation du webhook ignorée.")
         # Pour la production, vous devriez retourner une erreur 500 ou 400 ici si l'ID n'est pas configuré.
         # Pour les tests, nous pouvons continuer but soyez conscient que le webhook n'est PAS VALIDÉ.


    # --------- Validation de webhook (nécessite la configuration du client) ---------
    # Cette partie nécessite le client PayPal HttpClient, qui fait partie du SDK PayPal Checkout.
    # Le code original utilisait paypalrestsdk, qui peut avoir une approche légèrement différente
    # ou nécessiter l'installation du SDK checkout séparément.
    # Pour l'instant, la validation est commentée car elle nécessite une configuration et potentiellement
    # l'ajout d'un autre SDK (paypal-checkout-sdk) ou l'adaptation au SDK paypalrestsdk.
    # Si vous activez la validation, assurez-vous d'avoir le bon SDK et la bonne configuration.

    # from paypalcheckoutsdk.core import SandboxEnvironment, LiveEnvironment # Nécessite paypal-checkout-sdk
    # from paypalcheckoutsdk.notifications.webhooks import VerificationApi # Nécessite paypal-checkout-sdk
    # from paypalhttp import HttpHeaders # Nécessite paypal-checkout-sdk

    # try:
    #      # Assurez-vous d'avoir instancié le client PayPal HttpClient (voir commentaires plus haut)
    #      # Si vous utilisez paypal-checkout-sdk:
    #      # environment = SandboxEnvironment(PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET)
    #      # client = paypalcheckoutsdk.core.PayPalHttpClient(environment) # Nécessite paypal-checkout-sdk
    #      # signature_verification = VerificationApi(client)
    #      # request_data = GenerateWebhookEventRequest()
    #      # request_data.headers = HttpHeaders(request_headers)
    #      # request_data.body = request_body.decode('utf-8') # Le corps doit être une chaîne
    #      # request_data.auth_algorithm = request_headers.get("paypal-auth-algo")
    #      # request_data.cert_url = request_headers.get("paypal-cert-url")
    #      # request_data.transmission_id = request_headers.get("paypal-transmission-id")
    #      # request_data.transmission_signature = request_headers.get("paypal-transmission-sig")
    #      # request_data.transmission_time = request_headers.get("paypal-transmission-time")
    #      # request_data.webhook_id = webhook_id

    #      # response = signature_verification.verify_webhook_signature(request_data)
    #      # if response.status_code != 200:
    #      #     print("Validation de webhook échouée.")
    #      #     raise HTTPException(status_code=400, detail="Signature de webhook invalide")
    #      # else:
    #      #     print("Validation de webhook réussie.")

    # except Exception as e:
    #      print(f"Erreur lors de la validation du webhook : {e}")
    #      raise HTTPException(status_code=400, detail=f"Erreur de validation du webhook: {e}")
    # ---------------------------------------------------------------------------------------


    try:
        # Si la validation réussit (ou si vous la sautez pour le test):
        # Convertir le corps en JSON APRES validation
        event = json.loads(request_body.decode('utf-8'))
        event_type = event.get("event_type")
        resource = event.get("resource", {})

        print(f"Webhook PayPal reçu: Type = {event_type}")

        # Exemple de gestion des événements d'abonnement
        if event_type == "BILLING.SUBSCRIPTION.ACTIVATED":
            subscription_id = resource.get("id")
            # Le champ 'plan_id' est également disponible dans le resource si besoin
            # plan_id = resource.get("plan_id")
            # L'objet resource contient aussi les détails du 'subscriber' si nécessaire
            # subscriber = resource.get("subscriber", {})

            if subscription_id:
                 cursor = db.cursor()
                 # Rechercher l'utilisateur par l'ID d'abonnement PayPal stocké
                 # Dans un système de production robuste, vous pourriez aussi utiliser un 'custom_id'
                 # passé lors de la création de l'abonnement pour identifier l'utilisateur.
                 cursor.execute('SELECT * FROM users WHERE paypal_subscription_id = ?', (subscription_id,))
                 user = cursor.fetchone()
                 if user:
                      # Mettre à jour le statut de l'utilisateur dans votre BDD
                      # Assurez-vous que le plan est correct (ici on suppose 'Premium' si activé)
                      cursor.execute('UPDATE users SET subscription_status = ?, plan = ?, monthly_generations_count = ? WHERE api_key = ?', ('active', 'Premium', 0, user["api_key"]))
                      db.commit()
                      print(f"Statut d'abonnement mis à jour à 'active' pour l'utilisateur: {user['api_key']} (Abonnement PayPal ID: {subscription_id})")
                 else:
                      print(f"Webhook 'ACTIVATED' reçu pour l'abonnement {subscription_id}, but utilisateur non trouvé dans la BDD avec cet ID d'abonnement.")


        elif event_type == "BILLING.SUBSCRIPTION.CANCELLED":
             subscription_id = resource.get("id")
             if subscription_id:
                 cursor = db.cursor()
                 cursor.execute('SELECT * FROM users WHERE paypal_subscription_id = ?', (subscription_id,))
                 user = cursor.fetchone()
                 if user:
                      # Marquer l'abonnement comme inactif. Vous pourriez aussi définir une date de fin de période si PayPal la fournit.
                      cursor.execute('UPDATE users SET subscription_status = ? WHERE api_key = ?', ('inactive', user["api_key"]))
                      db.commit()
                      print(f"Statut d'abonnement mis à jour à 'inactive' pour l'utilisateur: {user['api_key']} (Abonnement PayPal ID: {subscription_id})")
                 else:
                      print(f"Webhook 'CANCELLED' reçu pour l'abonnement {subscription_id}, but utilisateur non trouvé dans la BDD avec cet ID d'abonnement.")

        # Ajoutez d'autres cas elif pour gérer d'autres types d'événements importants (ex: paiement échoué BILLING.SUBSCRIPTION.PAYMENT.FAILED, renouvellement réussi BILLING.SUBSCRIPTION.PAYMENT.APPROVED, etc.)
        # elif event_type == "BILLING.SUBSCRIPTION.SUSPENDED":
        #     # Gérer la suspension de l'abonnement
        #     pass
        # elif event_type == "BILLING.SUBSCRIPTION.EXPIRED":
        #     # Gérer l'expiration de l'abonnement
        #     pass


        # Toujours retourner une réponse 200 OK pour indiquer à PayPal que le webhook a été reçu et traité (même si l'utilisateur n'est pas trouvé, on a bien reçu le webhook)
        return {"status": "success", "received_event_type": event_type}

    except json.JSONDecodeError:
         print("Erreur de décodage JSON du corps du webhook.")
         # Retourner une erreur pour indiquer à PayPal que la requête était mal formée
         raise HTTPException(status_code=400, detail="Corps de la requête non valide ou non-JSON")
    except Exception as e:
        print(f"Erreur lors du traitement du webhook PayPal : {e}")
        # Retourner 500 Internal Server Error si le traitement interne échoue
        raise HTTPException(status_code=500, detail=f"Erreur interne lors du traitement du webhook: {str(e)}")


# Ajouter ce bloc pour exécuter l'application FastAPI avec uvicorn
if __name__ == "__main__":
    # Assurez-vous que l'application n'est pas déjà en cours d'exécution dans une autre cellule
    # car cela provoquerait une erreur (l'adresse est déjà utilisée)
    print("Démarrage de l'API FastAPI...")
    # Sur Cloud Run, le port est défini par la variable d'environnement PORT
    # Utilisez 8000 comme valeur par défaut pour les tests locaux si PORT n'est pas défini
    port = int(os.environ.get("PORT", 8000))
    try:
        # Si vous utilisez ngrok pour les tests locaux, vous le démarreriez ici
        # public_url = ngrok.connect(port).public_url
        # print(f"ngrok tunnel créé: {public_url}")
        # print(f"URL du Webhook PayPal à utiliser: {public_url}/webhooks/paypal")

        # Exécuter l'application FastAPI
        uvicorn.run(app, host="0.0.0.0", port=port)
    except Exception as e:
        print(f"Erreur lors du démarrage de l'API : {e}")
        print("Veuillez vérifier si une autre instance de l'API n'est pas déjà en cours d'exécution.")

