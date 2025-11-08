from fastapi import FastAPI
from fastapi.responses import RedirectResponse, PlainTextResponse
import stripe
import os
import requests
from typing import Dict, Any

# --- 1. CORE APPLICATION INITIALIZATION ---
# CRITICAL FIX: The FastAPI app MUST be initialized before any decorators (@app.get) use it.
app = FastAPI()

# --- 2. CONFIGURATION & ENVIRONMENT VARIABLES ---

# Load configuration variables from Render environment
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
SHOPIFY_API_TOKEN = os.getenv("SHOPIFY_API_TOKEN") 
SHOPIFY_STORE_URL = os.getenv("SHOPIFY_STORE_URL") # e.g., your-store-name.myshopify.com

# Initialize Stripe Client
if not STRIPE_SECRET_KEY:
    print("Warning: STRIPE_SECRET_KEY is not set.")
stripe.api_key = STRIPE_SECRET_KEY

# Headers for Shopify API Calls (Requires 'read_orders' permission on the API Token)
headers = {
    "X-Shopify-Access-Token": SHOPIFY_API_TOKEN,
    "Content-Type": "application/json"
}

# Temporary storage for payment links (resets on server restart/deploy)
order_links: Dict[int, str] = {}

# --- 3. HELPER FUNCTION (UPDATED FOR ROBUSTNESS) ---

def get_order_amount(order_id: int) -> int | None:
    """
    Fetches the total price from the Shopify Admin API for a given order ID.
    Includes robust error handling for API response structure to prevent 500 errors.
    """
    if not SHOPIFY_STORE_URL or not SHOPIFY_API_TOKEN:
        print("Shopify configuration missing. Cannot fetch order amount.")
        return None

    # Using the Admin API to get order details
    url = f"https://{SHOPIFY_STORE_URL}/admin/api/2024-07/orders/{order_id}.json"
    order_data: Dict[str, Any] = {} # Initialize outside of try block for debugging access
    
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status() # Check for HTTP errors (4xx or 5xx)

        order_data = response.json()
        
        # --- ROBUST PARSING LOGIC ---
        order_info = order_data.get("order")
        if not order_info:
            print(f"ERROR: Shopify response is missing 'order' key for ID {order_id}. Response Keys: {list(order_data.keys()) if order_data else 'None'}")
            return None
        
        # Use .get() chains to safely access deeply nested dictionary values
        total_price_usd = (
            order_info.get("total_price_set", {})
            .get("shop_money", {})
            .get("amount")
        )
        
        # Fallback for orders that might have a simpler structure
        if total_price_usd is None:
            total_price_usd = order_info.get("total_price")

        if total_price_usd is None:
            print(f"ERROR: Could not find any price data for Order {order_id}.")
            return None
        
        # Convert the price (string, e.g., "150.00") to cents (integer, e.g., 15000) for Stripe
        amount_cents = int(float(total_price_usd) * 100)
        
        if amount_cents <= 0:
            print(f"WARNING: Order {order_id} has an amount of zero.")

        return amount_cents
        
    except requests.exceptions.RequestException as e:
        print(f"Error fetching order {order_id} (API failure): {e}")
        return None
    except Exception as e:
        # Catch any final parsing errors (e.g., trying to float() a non-numeric string)
        print(f"CRITICAL PARSING ERROR for order {order_id}: {e}")
        # Log the keys to help debug the actual structure
        print(f"Response Keys: {list(order_data.keys()) if order_data else 'No response data.'}")
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
    
    # If amount is None due to an error, we return 404/400 to the user
    if amount is None or amount <= 0:
        # User sees this message; the detailed error is in the Render logs.
        return PlainTextResponse(
            f"Order {order_id} not found or could not retrieve a valid amount. Check backend logs for CRITICAL PARSING ERROR details.", 
            status_code=404
        )
    
    # Check if a link already exists
    if order_id in order_links:
        print(f"Redirecting to existing link for Order {order_id}")
        return RedirectResponse(order_links[order_id], status_code=303)

    try:
        # 2. Create the Stripe Payment Link
        payment_link = stripe.PaymentLink.create(
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "unit_amount": amount, 
                    "product_data": {
                        "name": f"Shopify Order #{order_id}",
                        "description": "ACH Payment for e-commerce purchase."
                    },
                },
                "quantity": 1,
            }],
            payment_method_types=["us_bank_account"],
            metadata={"shopify_order_id": str(order_id)},
            
            # --- !!! IMPORTANT: REPLACE THESE WITH YOUR STORE'S ACTUAL URLs !!! ---
            success_url="https://YOUR_STORE_URL/checkout/thank_you?session_id={CHECKOUT_SESSION_ID}", 
            cancel_url="https://YOUR_STORE_URL/cart"
        )
        
        # 3. Store and Redirect
        order_links[order_id] = payment_link.url
        return RedirectResponse(order_links[order_id], status_code=303)
        
    except stripe.error.StripeError as e:
        print(f"Stripe API Error: {e}")
        return PlainTextResponse(f"Payment processing failed: {e.user_message}", status_code=500)
    except Exception as e:
        print(f"An unexpected server error occurred: {e}")
        return PlainTextResponse("An unexpected server error occurred.", status_code=500)
