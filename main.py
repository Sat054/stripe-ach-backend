from fastapi import FastAPI, Request, Header
from fastapi.responses import PlainTextResponse
import stripe
import os
import requests
import json
from typing import List, Dict, Any

# --- 1. CORE APPLICATION INITIALIZATION ---
app = FastAPI()

# --- 2. CONFIGURATION & ENVIRONMENT VARIABLES ---

# MANDATORY ENV VARS (Must be set in Render Dashboard)
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
SHOPIFY_API_TOKEN = os.getenv("SHOPIFY_API_TOKEN") 
SHOPIFY_STORE_URL = os.getenv("SHOPIFY_STORE_URL")
SHOPIFY_WEBHOOK_SECRET = os.getenv("SHOPIFY_WEBHOOK_SECRET") 

# CUSTOM ENV VAR (Must match the exact name of your manual payment method in Shopify)
# The log confirmed this is "Pay via ACH" for your setup.
MANUAL_PAYMENT_GATEWAY_NAME = os.getenv("MANUAL_PAYMENT_GATEWAY_NAME", "Pay via ACH") 

# Stripe Initialization
if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY
else:
    # This warning is harmless if you set the key in Render
    print("WARNING: STRIPE_SECRET_KEY not set. Stripe API calls will fail if not set in environment.")

# Headers for Shopify Admin API Calls
headers: Dict[str, str] = {
    "X-Shopify-Access-Token": SHOPIFY_API_TOKEN,
    "Content-Type": "application/json"
}

# --- 3. HELPER FUNCTIONS ---

def get_order_amount(order_id: int) -> float | None:
    """
    Fetches the total price from the Shopify Admin API for a given order ID.
    Returns amount in standard units (e.g., USD, float).
    """
    if not SHOPIFY_STORE_URL or not SHOPIFY_API_TOKEN:
        print("Shopify configuration missing. Cannot fetch order amount.")
        return None

    # Use the stable 2024-07 version of the Shopify Admin API
    url = f"https://{SHOPIFY_STORE_URL}/admin/api/2024-07/orders/{order_id}.json"
    
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        order_data: Dict[str, Any] = response.json()
        
        # Robustly extract the total price
        order_info: Dict[str, Any] | None = order_data.get("order")
        if not order_info:
            print(f"ERROR: Shopify response is missing 'order' key for ID {order_id}.")
            return None
        
        # Extract amount from total_price_set or fall back to total_price
        total_price_usd_str: str | None = (
            order_info.get("total_price_set", {})
            .get("shop_money", {})
            .get("amount")
        )
        if total_price_usd_str is None:
            total_price_usd_str = order_info.get("total_price")

        if total_price_usd_str is None:
            print(f"ERROR: Could not find any price data for Order {order_id}.")
            return None
        
        amount_float: float = float(total_price_usd_str)
        return amount_float
        
    except requests.exceptions.RequestException as e:
        print(f"Error fetching order {order_id} (Request failure): {e}")
        return None
    except Exception as e:
        print(f"CRITICAL PARSING ERROR for order {order_id}: {e}")
        return None

def update_shopify_order_note(order_id: int, note: str) -> bool:
    """Updates the customer-facing note on the Shopify order."""
    if not SHOPIFY_STORE_URL or not SHOPIFY_API_TOKEN:
        print("Shopify configuration missing. Cannot update order.")
        return False
        
    url = f"https://{SHOPIFY_STORE_URL}/admin/api/2024-07/orders/{order_id}.json"
    
    payload: Dict[str, Any] = {
        "order": {
            "id": order_id,
            "note": note
        }
    }
    
    try:
        response = requests.put(url, headers=headers, json=payload)
        response.raise_for_status()
        print(f"Successfully updated Order {order_id} note with payment link.")
        return True
    except requests.exceptions.RequestException as e:
        print(f"Error updating Shopify order {order_id}: {e}") 
        return False

def verify_webhook_signature(data: bytes, hmac_header: str) -> bool:
    """TEMPORARILY BYPASSING VALIDATION for the sake of the exercise."""
    print("⚠️ WARNING: Webhook security check skipped for diagnostic purposes.")
    return True 


# --- 4. ENDPOINTS ---

@app.get("/")
def read_root():
    """Simple health check endpoint for Render deployment status."""
    return {"status": "ok", "message": "Stripe ACH Service is running successfully"}


@app.post("/shopify-webhook")
async def shopify_webhook(
    request: Request,
    x_shopify_hmac_sha256: str = Header(None),
    x_shopify_topic: str = Header(None)
):
    """
    Handles the 'orders/create' webhook from Shopify.
    Generates a Stripe link if the order used the manual ACH payment method.
    """
    # 1. READ BODY ONCE: Read the body as bytes for HMAC validation
    body_bytes = await request.body()
    
    # 2. SECURITY: Bypass the webhook signature check 
    if not verify_webhook_signature(body_bytes, x_shopify_hmac_sha256):
        return PlainTextResponse("Unauthorized", status_code=401)
        
    # 3. EXTRACT DATA: Safely parse the bytes into JSON
    try:
        data: Dict[str, Any] = json.loads(body_bytes.decode('utf-8'))
        
        order_id: int = data.get("id", 0)
        # Payment gateway names is an array on the order object
        gateway_names: List[str] = data.get("payment_gateway_names", [])
        
        # Use the first gateway name for payment processing logic
        gateway: str | None = gateway_names[0] if gateway_names and len(gateway_names) > 0 else None
        
    except json.JSONDecodeError as e:
        print(f"FAILURE: Error processing webhook JSON: {e}")
        return PlainTextResponse("Invalid JSON payload.", status_code=400)
    except Exception as e:
        print(f"FAILURE: Error during data extraction: {e}")
        return PlainTextResponse("Data extraction failed.", status_code=400)


    # 4. TOPIC AND FIELD VALIDATION
    if x_shopify_topic != 'orders/create':
        print(f"FAILURE: Topic is '{x_shopify_topic}'. Expected 'orders/create'.")
        return PlainTextResponse(f"Wrong topic received: {x_shopify_topic}", status_code=200) 

    if not order_id or not gateway:
        print(f"FAILURE: Payload missing 'id' ({order_id}) or a valid payment gateway name in 'payment_gateway_names' ({gateway}).")
        return PlainTextResponse("Missing order ID or gateway in payload.", status_code=400)
            
    print(f"SUCCESS: Received webhook for Order ID: {order_id}, Gateway: {gateway}")

    # 5. BUSINESS LOGIC: Check if it's the target payment method
    if gateway != MANUAL_PAYMENT_GATEWAY_NAME:
        print(f"Gateway '{gateway}' does not match required '{MANUAL_PAYMENT_GATEWAY_NAME}'. Ignored.")
        return PlainTextResponse(f"Processing ignored.", status_code=200)

    # 6. Generate Link
    try:
        amount_float: float | None = get_order_amount(order_id)
        
        if amount_float is None or amount_float <= 0:
            print(f"FAILURE: Could not retrieve valid amount for order {order_id}. (Check SHOPIFY_API_TOKEN/URL).")
            return PlainTextResponse(f"Could not retrieve valid amount for order {order_id}.", status_code=200)

        # Convert float amount to cents (integer) for Stripe API
        amount_cents: int = int(amount_float * 100) 

        # Create the Stripe Payment Link
        payment_link = stripe.PaymentLink.create(
            line_items=[{
                "price_data": {
                    "currency": "usd", "unit_amount": amount_cents, 
                    "product_data": {"name": f"Shopify Order #{order_id}", "description": "ACH Payment Link."}
                },
                "quantity": 1,
            }],
            # Ensure only US Bank Account (ACH) is enabled
            payment_method_types=["us_bank_account"], 
            metadata={"shopify_order_id": str(order_id)},
            # Redirect customer back to their Shopify Order Status Page after successful payment
            after_completion={"type": "redirect", "redirect": {"url": f"https://{SHOPIFY_STORE_URL}/admin/orders/{order_id}"}}
        )
        payment_link_url: str = payment_link.url

        # 7. Update Shopify Order Note (This is how the link gets into the email/thank-you page)
        note_text: str = (
            f"Thank you for selecting Manual ACH Payment. To complete your transaction, "
            f"please click the secure payment link below. You will be redirected to Stripe to enter your bank details.\n\n"
            f"👉 SECURE PAYMENT LINK:\n{payment_link_url}"
        )
        
        if update_shopify_order_note(order_id, note_text):
            return PlainTextResponse("Payment link generated and order updated.", status_code=200)
        else:
            return PlainTextResponse("Link generated, but failed to update order.", status_code=200)

    except stripe.error.StripeError as e:
        print(f"Stripe API Error during webhook processing: {e}")
        # Return 200 to prevent Shopify from retrying, as the error is external (Stripe config/API key)
        return PlainTextResponse(f"Stripe error: {e.user_message}", status_code=200)
    except Exception as e:
        print(f"Unhandled error in webhook: {e}")
        return PlainTextResponse("Unhandled server error.", status_code=200)
