from fastapi import FastAPI, Request, Header
from fastapi.responses import PlainTextResponse
import stripe
import os
import requests
import json
from typing import List, Dict, Any, Optional

# --- 1. CORE APPLICATION INITIALIZATION ---
app = FastAPI(title="Shopify ACH Stripe Link Generator")

# --- 2. CONFIGURATION & ENVIRONMENT VARIABLES ---

# --- CRITICAL API KEYS & URLS (MUST BE SET) ---
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
SHOPIFY_API_TOKEN = os.getenv("SHOPIFY_API_TOKEN")
SHOPIFY_STORE_URL = os.getenv("SHOPIFY_STORE_URL")
SHOPIFY_WEBHOOK_SECRET = os.getenv("SHOPIFY_WEBHOOK_SECRET") 
STRIPE_WEBHOOK_SECRET_PAYMENT = os.getenv("STRIPE_WEBHOOK_SECRET_PAYMENT") # New secret for Stripe webhooks

# --- CUSTOM PAYMENT CONFIGURATION (MUST BE SET) ---
MANUAL_PAYMENT_GATEWAY_NAME = os.getenv("MANUAL_PAYMENT_GATEWAY_NAME", "Pay via ACH")

# Stripe Initialization
if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY
else:
    print("WARNING: STRIPE_SECRET_KEY not set. Stripe API calls will fail.")

# Headers for Shopify Admin API Calls
headers: Dict[str, str] = {
    "X-Shopify-Access-Token": SHOPIFY_API_TOKEN,
    "Content-Type": "application/json"
}

# --- 3. HELPER FUNCTIONS ---

def get_order_amount(order_id: int) -> Optional[float]:
    """Fetches the total price from the Shopify Admin API for a given order ID."""
    if not SHOPIFY_STORE_URL or not SHOPIFY_API_TOKEN:
        print("Shopify configuration missing. Cannot fetch order amount.")
        return None

    # Simplified API version for stability
    url = f"https{"://"}{SHOPIFY_STORE_URL}/admin/api/2024-07/orders/{order_id}.json"

    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        order_data: Dict[str, Any] = response.json()
        order_info: Optional[Dict[str, Any]] = order_data.get("order")
        
        if not order_info: return None
        
        total_price_usd_str: Optional[str] = (
            order_info.get("total_price_set", {})
            .get("shop_money", {})
            .get("amount")
        )
        if total_price_usd_str is None:
            total_price_usd_str = order_info.get("total_price")

        if total_price_usd_str is None: return None

        return float(total_price_usd_str)

    except Exception as e:
        print(f"Error fetching order {order_id}: {e}")
        return None

def update_shopify_order_note(order_id: int, note: str) -> bool:
    """Updates the customer-facing note on the Shopify order."""
    if not SHOPIFY_STORE_URL or not SHOPIFY_API_TOKEN:
        return False

    url = f"https{"://"}{SHOPIFY_STORE_URL}/admin/api/2024-07/orders/{order_id}.json"

    payload: Dict[str, Any] = {
        "order": {
            "id": order_id,
            "note": note
        }
    }

    try:
        requests.put(url, headers=headers, json=payload).raise_for_status()
        print(f"Successfully updated Order {order_id} note with payment link.")
        return True
    except requests.exceptions.RequestException as e:
        print(f"Error updating Shopify order {order_id}: {e}")
        return False

def verify_shopify_webhook_signature(data: bytes, hmac_header: Optional[str]) -> bool:
    """
    *** WARNING: THIS IS THE INSECURE BYPASSED VERSION ***
    For testing, this function returns True. You should secure this in production.
    """
    print("⚠️ WARNING: Shopify Webhook security check is disabled.")
    return True

def mark_shopify_order_paid(order_id: int, amount: str) -> bool:
    """Marks a Shopify order as paid by creating a transaction."""
    if not SHOPIFY_STORE_URL or not SHOPIFY_API_TOKEN:
        print("Shopify configuration missing. Cannot mark order paid.")
        return False

    url = f"https{"://"}{SHOPIFY_STORE_URL}/admin/api/2024-07/orders/{order_id}/transactions.json"

    # The payload for marking an order paid (creating a transaction)
    payload: Dict[str, Any] = {
        "transaction": {
            "kind": "capture",          # 'capture' means finalizing the payment
            "status": "success",        # The payment was successful on Stripe
            "amount": amount,           # The total amount paid (e.g., "100.00")
            "gateway": "Stripe ACH",    # The name that will appear on the Shopify order
            "currency": "USD"           # Ensure this matches your store currency
        }
    }

    try:
        response = requests.post(url, headers=headers, json=payload)
        response.raise_for_status()
        print(f"Transaction created successfully for Order {order_id}. Order is now marked Paid.")
        return True
    except requests.exceptions.RequestException as e:
        print(f"Error marking Shopify order {order_id} paid: {e}")
        try:
            print(f"Shopify API Error details: {response.json()}")
        except:
            pass
        return False


# --- 4. ENDPOINTS ---

@app.get("/")
def read_root():
    """Simple health check endpoint for Render deployment status."""
    return {"status": "ok", "message": "Stripe ACH Service is running successfully"}


@app.post("/shopify-webhook")
async def shopify_webhook(
    request: Request,
    x_shopify_hmac_sha256: Optional[str] = Header(None),
    x_shopify_topic: Optional[str] = Header(None)
):
    """
    Handles the 'orders/create' webhook from Shopify: Generates Stripe Link.
    """
    body_bytes = await request.body()

    if not verify_shopify_webhook_signature(body_bytes, x_shopify_hmac_sha256):
        return PlainTextResponse("Unauthorized", status_code=401)

    try:
        data: Dict[str, Any] = json.loads(body_bytes.decode('utf-8'))
        order_id: int = data.get("id", 0)
        gateway_names: List[str] = data.get("payment_gateway_names", [])
        gateway: Optional[str] = gateway_names[0] if gateway_names and len(gateway_names) > 0 else None
    except:
        return PlainTextResponse("Data extraction failed.", status_code=400)

    if x_shopify_topic != 'orders/create':
        return PlainTextResponse(f"Wrong topic received: {x_shopify_topic}", status_code=200)

    if not order_id or gateway != MANUAL_PAYMENT_GATEWAY_NAME:
        print(f"Gateway '{gateway}' does not match required '{MANUAL_PAYMENT_GATEWAY_NAME}'. Ignored.")
        return PlainTextResponse(f"Processing ignored.", status_code=200)

    print(f"SUCCESS: Received webhook for Order ID: {order_id}, Gateway: {gateway}. Generating link...")

    # Generate Link
    try:
        amount_float: Optional[float] = get_order_amount(order_id)
        if amount_float is None or amount_float <= 0:
            return PlainTextResponse(f"Could not retrieve valid amount for order {order_id}.", status_code=200)

        amount_cents: int = int(amount_float * 100)

        payment_link = stripe.PaymentLink.create(
            line_items=[{
                "price_data": {
                    "currency": "usd", "unit_amount": amount_cents,
                    "product_data": {"name": f"Shopify Order #{order_id}", "description": "ACH Payment Link."}
                },
                "quantity": 1,
            }],
            payment_method_types=["us_bank_account"],
            # CRITICAL: Attach the Shopify Order ID to the metadata for retrieval later
            metadata={"shopify_order_id": str(order_id)}, 
            after_completion={"type": "redirect", "redirect": {"url": f"https{"://"}{SHOPIFY_STORE_URL}/admin/orders/{order_id}"}}
        )
        payment_link_url: str = payment_link.url

        # Update Shopify Order Note (This is where the customer gets the link)
        note_text: str = (
            f"Thank you for selecting Manual ACH Payment. To complete your transaction, "
            f"please click the secure payment link below. You will be redirected to Stripe to enter your bank details.\n\n"
            f"👉 SECURE PAYMENT LINK:\n{payment_link_url}"
        )
        update_shopify_order_note(order_id, note_text)
        
        return PlainTextResponse("Payment link generated and order updated.", status_code=200)

    except stripe.error.StripeError as e:
        print(f"Stripe API Error during webhook processing: {e}")
        return PlainTextResponse(f"Stripe error: {e.user_message}", status_code=200)
    except Exception as e:
        print(f"Unhandled error in webhook: {e}")
        return PlainTextResponse("Unhandled server error.", status_code=200)


@app.post("/stripe-webhook")
async def stripe_webhook(request: Request, stripe_signature: Optional[str] = Header(None)):
    """
    Listens for Stripe 'checkout.session.completed' event to mark a Shopify order as paid.
    """
    if not STRIPE_WEBHOOK_SECRET_PAYMENT:
        print("ERROR: STRIPE_WEBHOOK_SECRET_PAYMENT not set. Cannot verify webhook.")
        return PlainTextResponse("Server Misconfiguration", status_code=500)

    payload = await request.body()
    
    # VERIFY SIGNATURE (CRITICAL SECURITY STEP)
    try:
        event = stripe.Webhook.construct_event(
            payload, stripe_signature, STRIPE_WEBHOOK_SECRET_PAYMENT
        )
    except Exception as e:
        print(f"Stripe Webhook Error: Invalid signature or payload: {e}")
        return PlainTextResponse("Invalid signature or payload", status_code=400)

    # PROCESS EVENT
    if event['type'] == 'checkout.session.completed':
        session = event['data']['object']
        
        if session.get("payment_status") == "paid":
            try:
                # Retrieve the Shopify Order ID from the metadata
                shopify_order_id = session['metadata'].get("shopify_order_id")
                amount_total_cents = session.get("amount_total", 0)
                
                if shopify_order_id:
                    # Convert cents to dollars (e.g., 10000 -> 100.00)
                    amount_float = amount_total_cents / 100.0 
                    amount_str = f"{amount_float:.2f}"
                    
                    if mark_shopify_order_paid(int(shopify_order_id), amount_str):
                        print(f"✅ Stripe Payment Success: Shopify Order {shopify_order_id} marked as Paid.")
                    else:
                        print(f"❌ Stripe Payment Success: FAILED to mark Shopify Order {shopify_order_id} as paid.")
                        
                else:
                    print("Error: Could not find shopify_order_id in session metadata. Order status not updated.")
            except Exception as e:
                print(f"Error processing completed session: {e}")

    # Return a 200 to Stripe immediately to acknowledge receipt
    return PlainTextResponse("Event received and processed.", status_code=200)
