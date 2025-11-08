from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, PlainTextResponse
import stripe
import os
import requests
from typing import Dict

# --- 1. CORE APPLICATION INITIALIZATION ---
# THIS MUST BE THE FIRST LINE OF CODE AFTER IMPORTS TO PREVENT 'NameError'
app = FastAPI()

# --- 2. CONFIGURATION & ENVIRONMENT VARIABLES ---

# Critical Environment Variables (Set these on Render Dashboard)
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
SHOPIFY_API_TOKEN = os.getenv("SHOPIFY_API_TOKEN") # e.g., shpca_...
SHOPIFY_STORE_URL = os.getenv("SHOPIFY_STORE_URL") # e.g., your-store-name.myshopify.com

# Initialize Stripe Client
if not STRIPE_SECRET_KEY:
    # If the key is missing, raise an error or use a placeholder in development
    print("Warning: STRIPE_SECRET_KEY is not set.")
stripe.api_key = STRIPE_SECRET_KEY

# Headers for Shopify API Calls
headers = {
    "X-Shopify-Access-Token": SHOPIFY_API_TOKEN,
    "Content-Type": "application/json"
}

# Temporary storage for payment links (This will reset on every deployment/restart!)
# For a production app, this should use a proper database (like Firestore).
order_links: Dict[int, str] = {}

# --- 3. HELPER FUNCTION ---

def get_order_amount(order_id: int) -> int | None:
    """
    Fetches the total price from the Shopify Admin API for a given order ID.
    Returns the amount in cents (integer) for Stripe, or None on failure.
    """
    if not SHOPIFY_STORE_URL or not SHOPIFY_API_TOKEN:
        print("Shopify configuration missing. Cannot fetch order amount.")
        return None

    # Using the Admin API to get order details (using the 2024-07 version)
    url = f"https://{SHOPIFY_STORE_URL}/admin/api/2024-07/orders/{order_id}.json"
    
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status() # Check for HTTP errors (4xx or 5xx)

        order_data = response.json()
        
        # Accessing the total price in the shop's currency (USD in this case)
        # Note: This path might need adjustment based on your specific Shopify API response structure.
        total_price_usd = order_data["order"]["total_price_set"]["shop_money"]["amount"]
        
        # Convert the price (string, e.g., "150.00") to cents (integer, e.g., 15000) for Stripe
        amount_cents = int(float(total_price_usd) * 100)
        return amount_cents
        
    except requests.exceptions.RequestException as e:
        # Log the error and return None
        print(f"Error fetching order {order_id} from Shopify: {e}")
        return None
    except KeyError as e:
        print(f"Error parsing Shopify response for order {order_id}. Missing key: {e}")
        return None


# --- 4. ENDPOINTS ---

@app.get("/")
def read_root():
    """Simple health check endpoint for Render deployment status."""
    return {"status": "ok", "message": "Stripe ACH Service is running successfully"}


@app.get("/pay")
async def pay(order_id: int):
    """
    Generates a Stripe Payment Link for a Shopify order using ACH (US Bank Account).
    Redirects the user directly to the payment link.
    """
    # 1. Fetch the order amount
    amount = get_order_amount(order_id)
    
    if amount is None or amount <= 0:
        return PlainTextResponse(
            f"Order {order_id} not found or could not retrieve a valid amount.", 
            status_code=404
        )
    
    # Check if a link already exists for this order (to avoid creating duplicates)
    if order_id in order_links:
        print(f"Redirecting to existing link for Order {order_id}")
        return RedirectResponse(order_links[order_id])

    try:
        # 2. Create the Stripe Payment Link
        payment_link = stripe.PaymentLink.create(
            # Define the item being purchased
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "unit_amount": amount, # Amount in cents
                    "product_data": {
                        "name": f"Shopify Order #{order_id}",
                        "description": "ACH Payment for e-commerce purchase."
                    },
                },
                "quantity": 1,
            }],
            # Enable US Bank Account (ACH) payments
            payment_method_types=["us_bank_account"],
            
            # Use metadata to link the payment back to the Shopify Order
            metadata={"shopify_order_id": str(order_id)},
            
            # Define success/cancellation behavior
            success_url="YOUR_SUCCESS_URL_HERE?session_id={CHECKOUT_SESSION_ID}",
            cancel_url="YOUR_CANCEL_URL_HERE"
        )
        
        # 3. Store and Redirect
        order_links[order_id] = payment_link.url
        return RedirectResponse(order_links[order_id], status_code=303)
        
    except stripe.error.StripeError as e:
        print(f"Stripe API Error: {e}")
        return PlainTextResponse(f"Payment processing failed: {e.user_message}", status_code=500)
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        return PlainTextResponse("An unexpected server error occurred.", status_code=500)
