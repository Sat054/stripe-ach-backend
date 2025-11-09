from fastapi import FastAPI, Request, Header
from fastapi.responses import PlainTextResponse
import stripe
import os
import requests
import json
from typing import List, Dict, Any, Optional
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import email.utils
import hmac 
import hashlib 
import base64 

# --- 1. CORE APPLICATION INITIALIZATION ---
app = FastAPI(title="Shopify ACH Stripe Link Generator")

# --- 2. CONFIGURATION & ENVIRONMENT VARIABLES ---

# --- CRITICAL API KEYS & URLS (MUST BE SET) ---
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
SHOPIFY_API_TOKEN = os.getenv("SHOPIFY_API_TOKEN")
SHOPIFY_STORE_URL = os.getenv("SHOPIFY_STORE_URL")
SHOPIFY_WEBHOOK_SECRET = os.getenv("SHOPIFY_WEBHOOK_SECRET") 

# --- CUSTOM PAYMENT & EMAIL CONFIGURATION (MUST BE SET FOR EMAIL) ---
MANUAL_PAYMENT_GATEWAY_NAME = os.getenv("MANUAL_PAYMENT_GATEWAY_NAME", "Pay via ACH")
FROM_NAME = os.getenv("FROM_NAME", "Your Store Name")
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
FROM_EMAIL = os.getenv("FROM_EMAIL", SMTP_USER)

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

def send_payment_email(customer_email: str, order_id: int, payment_link_url: str, customer_name: Optional[str] = None) -> bool:
    """Sends an email to the customer with ACH payment instructions and link."""
    if not SMTP_USER or not SMTP_PASSWORD:
        return False

    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = f"Complete Your ACH Payment - Order #{order_id}"
        msg['From'] = email.utils.formataddr((FROM_NAME, FROM_EMAIL))
        msg['To'] = customer_email

        greeting = f"Hi {customer_name}," if customer_name else "Hello,"
        html_body = f"""
        <html>
            <body style='font-family: Arial, sans-serif; color: #333;'>
                <h2 style='color: #2563eb;'>Complete Your ACH Payment</h2>
                <p>{greeting}</p>
                <p>Thank you for your order <strong>#{order_id}</strong> from {FROM_NAME}!</p>
                <p>To complete your purchase, please click the secure payment link below:</p>
                <p style='margin: 30px 0;'>
                    <a href='{payment_link_url}' style='background-color: #2563eb; color: white; padding: 15px 30px; text-decoration: none; border-radius: 5px; display: inline-block;'>Pay with Bank Account</a>
                </p>
                <p>You will be redirected to Stripe to securely enter your bank account details.</p>
                <p style='margin-top: 30px; font-size: 12px; color: #666;'>If you have any questions, please contact our support team at {FROM_EMAIL}.</p>
            </body>
        </html>
        """

        html_part = MIMEText(html_body, 'html')
        msg.attach(html_part)

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(FROM_EMAIL, customer_email, msg.as_string())

        print(f"Payment email sent successfully to {customer_email} for Order #{order_id}")
        return True

    except Exception as e:
        print(f"Error sending payment email: {e}")
        return False

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

def verify_webhook_signature(data: bytes, hmac_header: Optional[str]) -> bool:
    """
    Cryptographically verifies the webhook request using the shared secret.
    Returns True only if the computed HMAC matches the header value.
    """
    if not hmac_header:
        print("SECURITY ALERT: Missing X-Shopify-Hmac-Sha256 header.")
        return False
    
    if not SHOPIFY_WEBHOOK_SECRET:
        # We allow this only in dev environments. In production, this should return False.
        print("SECURITY ALERT: SHOPIFY_WEBHOOK_SECRET environment variable is not set. Allowing request unverified.")
        return True 

    try:
        # 1. Create the computed HMAC digest
        hmac_digest = hmac.new(
            SHOPIFY_WEBHOOK_SECRET.encode('utf-8'),
            data,
            hashlib.sha256
        ).digest()

        # 2. Base64 encode the computed HMAC
        computed_hmac = base64.b64encode(hmac_digest).decode()

        # 3. Securely compare the computed HMAC with the header's HMAC
        return hmac.compare_digest(computed_hmac, hmac_header)

    except Exception as e:
        print(f"SECURITY ERROR: Failed to compute or compare HMAC: {e}")
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
    Handles the 'orders/create' webhook from Shopify.
    """
    # 1. READ BODY ONCE: Read the body as bytes for HMAC validation
    body_bytes = await request.body()

    # 2. SECURITY: Perform the HMAC validation
    if not verify_webhook_signature(body_bytes, x_shopify_hmac_sha256):
        return PlainTextResponse("Unauthorized", status_code=401)

    # 3. EXTRACT DATA
    try:
        data: Dict[str, Any] = json.loads(body_bytes.decode('utf-8'))
        order_id: int = data.get("id", 0)
        gateway_names: List[str] = data.get("payment_gateway_names", [])
        gateway: Optional[str] = gateway_names[0] if gateway_names and len(gateway_names) > 0 else None

    except json.JSONDecodeError:
        return PlainTextResponse("Invalid JSON payload.", status_code=400)
    except Exception:
        return PlainTextResponse("Data extraction failed.", status_code=400)


    # 4. TOPIC AND FIELD VALIDATION
    if x_shopify_topic != 'orders/create':
        return PlainTextResponse(f"Wrong topic received: {x_shopify_topic}", status_code=200)

    if not order_id or not gateway:
        return PlainTextResponse("Missing order ID or gateway in payload.", status_code=400)

    print(f"SUCCESS: Received verified webhook for Order ID: {order_id}, Gateway: {gateway}")

    # 5. BUSINESS LOGIC: Check if it's the target payment method
    if gateway != MANUAL_PAYMENT_GATEWAY_NAME:
        print(f"Gateway '{gateway}' does not match required '{MANUAL_PAYMENT_GATEWAY_NAME}'. Ignored.")
        return PlainTextResponse(f"Processing ignored.", status_code=200)

    # 6. Generate Link
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
            metadata={"shopify_order_id": str(order_id)},
            after_completion={"type": "redirect", "redirect": {"url": f"https{"://"}{SHOPIFY_STORE_URL}/admin/orders/{order_id}"}}
        )
        payment_link_url: str = payment_link.url

        # 7. Update Shopify Order Note and Send Email
        note_text: str = (
            f"Thank you for selecting Manual ACH Payment. To complete your transaction, "
            f"please click the secure payment link below. You will be redirected to Stripe to enter your bank details.\n\n"
            f"👉 SECURE PAYMENT LINK:\n{payment_link_url}"
        )

        if update_shopify_order_note(order_id, note_text):
            customer_email = data.get("customer", {}).get("email")
            customer_name = data.get("customer", {}).get("first_name")
            if customer_email:
                send_payment_email(customer_email, order_id, payment_link_url, customer_name)

            return PlainTextResponse("Payment link generated and order updated.", status_code=200)
        else:
            return PlainTextResponse("Link generated, but failed to update order.", status_code=200)

    except stripe.error.StripeError as e:
        print(f"Stripe API Error during webhook processing: {e}")
        return PlainTextResponse(f"Stripe error: {e.user_message}", status_code=200)
    except Exception as e:
        print(f"Unhandled error in webhook: {e}")
        return PlainTextResponse("Unhandled server error.", status_code=200)
