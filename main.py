import requests # <--- New Import

# Add your Shopify store URL
SHOPIFY_STORE_URL = os.getenv("SHOPIFY_STORE_URL") # e.g., your-store-name.myshopify.com

# Your headers
headers = {
    "X-Shopify-Access-Token": SHOPIFY_API_TOKEN,
    "Content-Type": "application/json" # Added for API calls
}

# 1. NEW function to get the real order total from Shopify
def get_order_amount(order_id):
    """Fetches the total price from the Shopify Admin API."""
    # Using the Admin API to get order details
    url = f"https://{SHOPIFY_STORE_URL}/admin/api/2024-07/orders/{order_id}.json"
    
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status() # Check for errors

        order_data = response.json()
        total_price_usd = order_data["order"]["total_price_set"]["shop_money"]["amount"]
        
        # Convert the price (string) to cents (integer) for Stripe
        amount_cents = int(float(total_price_usd) * 100)
        return amount_cents
        
    except requests.exceptions.RequestException as e:
        print(f"Error fetching order {order_id}: {e}")
        return None


# 2. Update your existing /pay endpoint with metadata
@app.get("/pay")
async def pay(order_id: int):
    amount = get_order_amount(order_id)
    if not amount:
        return PlainTextResponse("Order not found or could not retrieve amount", status_code=404)
    
    # ... (rest of your existing logic)
    
    # IMPORTANT: Pass the Shopify Order ID to Stripe using metadata!
    payment_link = stripe.PaymentLink.create(
        line_items=[{
            # ... (your line items data)
        }],
        payment_method_types=["us_bank_account"],
        metadata={"shopify_order_id": str(order_id)}, # <--- The critical addition
    )
    order_links[order_id] = payment_link.url
    return RedirectResponse(order_links[order_id])
