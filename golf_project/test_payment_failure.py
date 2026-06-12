import os
import django
import argparse

# Setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'golf_project.settings')
django.setup()

from square_payments.views import SquareWebhookView
from rest_framework.test import APIRequestFactory
from square_payments.models import MemberSubscription

def test_webhook(event_type, subscription_id):
    factory = APIRequestFactory()
    
    if event_type == 'invoice.scheduled_charge_failed':
        payload = {
            "type": "invoice.scheduled_charge_failed",
            "data": {
                "object": {
                    "invoice": {
                        "subscription_id": subscription_id,
                        "id": "inv_fake_failed_123"
                    }
                }
            }
        }
    elif event_type == 'subscription.canceled':
        payload = {
            "type": "subscription.canceled",
            "data": {
                "object": {
                    "subscription": {
                        "id": subscription_id
                    }
                }
            }
        }
    else:
        print("Unknown event type")
        return

    request = factory.post('/api/square/webhook/', payload, format='json')
    view = SquareWebhookView.as_view()
    response = view(request)
    
    print(f"Sent '{event_type}' webhook for subscription: {subscription_id}")
    print(f"Backend responded with: {response.status_code}")
    print("--------------------------------------------------")

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Test Square Payment Failures')
    parser.add_argument('subscription_id', type=str, help='The Square Subscription ID')
    parser.add_argument('--action', choices=['fail', 'cancel'], required=True, 
                        help='Type "fail" to simulate a temporary charge failure, or "cancel" to simulate a final cancellation after retries.')
    
    args = parser.parse_args()
    
    try:
        sub = MemberSubscription.objects.get(square_subscription_id=args.subscription_id)
        print(f"Found Subscription for user {sub.client.username}. Current status: {sub.status}")
    except MemberSubscription.DoesNotExist:
        print("Warning: Subscription ID not found in database, but sending webhook anyway...")
        
    if args.action == 'fail':
        test_webhook('invoice.scheduled_charge_failed', args.subscription_id)
        print("Result: Check your terminal running the Django server. You should see a PAYMENT FAILED warning log.")
        print("Note: The subscription status remains active because Square usually retries payments.")
    elif args.action == 'cancel':
        test_webhook('subscription.canceled', args.subscription_id)
        print("Result: The subscription should now be marked as 'canceled' in your database.")
