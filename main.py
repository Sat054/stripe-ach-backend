from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, PlainTextResponse
import stripe
import os

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
app = FastAPI()

# DEMO: Use a dictionary instead of a database (reset on deploy)
order_links = {}

# Placeholder: In production, fetch the real order amount from Shopify's API
def get_order_amount(order_id):
    # Demo: Assume all orders are $25.00
    return 2500  # 2500 cents ($25.00)

@app.get("/pay")
async def pay(order_id: int):
    amount = get_order_amount(order_id)
    if not amount:
        return PlainTextResponse("Order not found", status_code=404)
    if order_id not in order_links:
        description = f"Shopify Order #{order_id}"
        payment_link = stripe.PaymentLink.create(
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {"name": description},
                    "unit_amount": amount,
                },
                "quantity": 1,
            }],
            payment_method_types=["us_bank_account"],  # ACH
        )
        order_links[order_id] = payment_link.url
    return RedirectResponse(order_links[order_id])
