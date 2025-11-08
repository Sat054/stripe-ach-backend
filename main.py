from fastapi import FastAPI, Request, Header
from fastapi.responses import RedirectResponse, PlainTextResponse
import stripe
import os
import requests
import hmac
import hashlib
import base64
import json
from typing import Dict, Any

# --- 1. CORE APPLICATION INITIALIZATION ---
# This must be the first line using FastAPI
app = FastAPI()

# --- 2. CONFIGURATION & ENVIRONMENT VARIABLES ---

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
SHOPIFY_API_TOKEN = os.getenv("SHOPIFY_API_TOKEN") 
SHOPIFY_STORE_URL = os.getenv("SHOPIFY_STORE_URL")
# NEW: Required for Webhook Security - MUST be set in Render environment
SHOPIFY_WEBHOOK_SECRET = os.getenv("SHOPIFY_WEBHOOK_SECRET") 
# IMPORTANT: This must exactly match the name of your manual payment method in Shopify.
MANUAL_PAYMENT_GATEWAY_NAME = os.getenv("MANUAL_PAYMENT_GATEWAY_NAME", "Manual ACH Payment") 

stripe.api_key = STRIPE_SECRET_KEY

# Headers for Shopify Admin API Calls
headers = {
    "X-Shopify-Access-Token": SHOPIFY_API_TOKEN,
    "Content-Type": "application/json"
}

# Temporary storage for payment links (resets on server restart/deploy)
order_links: Dict[int, str] = {}


# --- 3. HELPER FUNCTIONS ---

def get_order_amount(order_id: int) -> int | None:
    """
    Fetches the total price from the Shopify Admin API for a given order ID.
    Includes robust error handling.
    """
    if not SHOPIFY_STORE_URL or not SHOPIFY_API_TOKEN:
        print("Shopify configuration missing. Cannot fetch order amount.")
        return None

    url = f"https://{SHOPIFY_STORE_URL}/admin/api/2024-07/orders/{order_id}.json"
    order_data: Dict[str, Any] = {}
    
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()

        order_data = response.json()
        
        order_info = order_data.get("order")
        if not order_info:
            print(f"ERROR: Shopify response is missing 'order' key for ID {order_id}.")
            return None
        
        # Safely access the price data using .get() chains
        total_price_usd = (
            order_info.get("total_price_set", {})
            .get("shop_money", {})
            .get("amount")
        )
        
        # Fallback
        if total_price_usd is None:
            total_price_usd = order_info.get("total_price")

        if total_price_usd is None:
            print(f"ERROR: Could not find any price data for Order {order_id}.")
            return None
        
        # Convert the price (string) to cents (integer) for Stripe
        amount_cents = int(float(total_price_usd) * 100)
        
        if amount_cents <= 0:
            print(f"WARNING: Order {order_id} has an amount of zero.")

        return amount_cents
        
    except requests.exceptions.RequestException as e:
        print(f"Error fetching order {order_id} (API failure): {e}")
        return None
    except Exception as e:
        print(f"CRITICAL PARSING ERROR for order {order_id}: {e}")
        return None

def update_shopify_order_note(order_id: int, note: str) -> bool:
    """Updates the customer-facing note on the Shopify order with the payment link."""
    if not SHOPIFY_STORE_URL or not SHOPIFY_API_TOKEN:
        print("Shopify configuration missing. Cannot update order.")
        return False
        
    url = f"https://{SHOPIFY_STORE_URL}/admin/api/2024-07/orders/{order_id}.json"
    
    payload = {
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

async def verify_webhook(request: Request, hmac_header: str) -> bool:
    """Validates the request signature to ensure it came from Shopify."""
    if not SHOPIFY_WEBHOOK_SECRET:
        print("WARNING: SHOPIFY_WEBHOOK_SECRET is not set. Skipping validation. DANGEROUS IN PRODUCTION!")
        return True # Temporarily allows testing without the secret
        
    data = await request.body()
    
    # Calculate the expected HMAC signature
    digest = hmac.new(
        SHOPIFY_WEBHOOK_SECRET.encode('utf-8'),
        data,
        hashlib.sha256
    ).digest()
    
    calculated_hmac = base64.b64encode(digest).decode()
    
    # Compare the calculated signature with the one from the header
    return hmac.compare_digest(hmac_header, calculated_hmac)


# --- 4. ENDPOINTS ---

@app.get("/")
def read_root():
    """Simple health check endpoint for Render deployment status."""
    return {"status": "ok", "message": "Stripe ACH Service is running successfully"}


@app.get("/pay")
async def pay(order_id: int):
    """
    Manually Generates a Stripe Payment Link and redirects the user (for testing/manual use).
    """
    amount = get_order_amount(order_id)
    
    if amount is None or amount <= 0:
        return PlainTextResponse(
            f"Order {order_id} not found or could not retrieve a valid amount. Check backend logs for details.", 
            status_code=404
        )
    
    if order_id in order_links:
        return RedirectResponse(order_links[order_id], status_code=303)

    try:
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
        )
        
        order_links[order_id] = payment_link.url
        return RedirectResponse(order_links[order_id], status_code=303)
        
    except stripe.error.StripeError as e:
        return PlainTextResponse(f"Payment processing failed: {e.user_message}", status_code=500)
    except Exception as e:
        return PlainTextResponse("An unexpected server error occurred.", status_code=500)


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
    # 1. SECURITY: Verify the webhook signature
    if not await verify_webhook(request, x_shopify_hmac_sha256):
        print("Webhook signature failed verification.")
        return PlainTextResponse("Unauthorized", status_code=401)
        
    # 2. Extract Data
    try:
        data = await request.json()
        order_id = data.get("id")
        gateway = data.get("gateway")
        
        if not order_id or not gateway:
            return PlainTextResponse("Missing order ID or gateway in payload.", status_code=400)
            
        print(f"Received webhook for Order ID: {order_id}, Gateway: {gateway}")

    except Exception as e:
        print(f"Error processing webhook JSON: {e}")
        return PlainTextResponse("Invalid JSON payload.", status_code=400)

    # 3. BUSINESS LOGIC: Check if it's the target payment method
    if gateway != MANUAL_PAYMENT_GATEWAY_NAME:
        # If it's a credit card or PayPal, we do nothing.
        return PlainTextResponse(f"Payment gateway is not '{MANUAL_PAYMENT_GATEWAY_NAME}'. Processing ignored.", status_code=200)

    # 4. Generate Link (reusing existing logic/helpers)
    try:
        amount = get_order_amount(order_id)
        if amount is None or amount <= 0:
            return PlainTextResponse(f"Could not retrieve valid amount for order {order_id}.", status_code=200)

        # Check for existing link (to avoid API rate limits/duplicates)
        if order_id in order_links:
            payment_link_url = order_links[order_id]
        else:
            payment_link = stripe.PaymentLink.create(
                line_items=[{
                    "price_data": {
                        "currency": "usd", "unit_amount": amount, 
                        "product_data": {"name": f"Shopify Order #{order_id}", "description": "ACH Payment Link."}
                    },
                    "quantity": 1,
                }],
                payment_method_types=["us_bank_account"],
                metadata={"shopify_order_id": str(order_id)},
            )
            payment_link_url = payment_link.url
            order_links[order_id] = payment_link_url

        # 5. Update Shopify Order
        note_text = f"Thank you for choosing Manual ACH. Please complete your payment here:\n{payment_link_url}"
        
        # This will update the order's customer-facing note
        if update_shopify_order_note(order_id, note_text):
            # Shopify expects a 200 OK response to confirm successful processing.
            return PlainTextResponse("Payment link generated and order updated.", status_code=200)
        else:
            # Still return 200 so Shopify doesn't retry, but log the failure.
            return PlainTextResponse("Link generated, but failed to update order.", status_code=200)

    except stripe.error.StripeError as e:
        print(f"Stripe API Error during webhook processing: {e}")
        # Return 200 to prevent continuous retries, but note the error.
        return PlainTextResponse(f"Stripe error: {e.user_message}", status_code=200)
    except Exception as e:
        print(f"Unhandled error in webhook: {e}")
        return PlainTextResponse("Unhandled server error.", status_code=200)
