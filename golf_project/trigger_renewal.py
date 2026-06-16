import os
import django
import sys
import argparse
from datetime import timedelta
from django.utils import timezone

# Setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'golf_project.settings')
django.setup()

from square_payments.models import MemberSubscription
from square_payments.views import SquareWebhookView
from coaching.models import SimulatorPackagePurchase, CoachingPackagePurchase

def simulate_renewal(subscription_id):
    try:
        sub = MemberSubscription.objects.get(square_subscription_id=subscription_id)
    except MemberSubscription.DoesNotExist:
        print(f"❌ Error: Could not find a MemberSubscription with ID {subscription_id}")
        return

    print(f"✅ Found subscription for user: {sub.client.username}")
    print(f"📦 Package: {sub.package.title}")
    
    # Check current hours
    purchase = sub.purchase
    if purchase:
        print(f"⏳ Current remaining hours: {purchase.hours_remaining} / {purchase.hours_total}")
    
    # 1. Manually update the subscription to simulate a month has passed
    old_end = sub.current_period_end
    new_start = timezone.now()
    new_end = new_start + timedelta(days=30)
    
    # 2. Reset the hours directly (mimicking what the webhook handler does for invoice.payment_made)
    print("\n🔄 Simulating Square 'invoice.payment_made' Webhook...")
    
    if purchase:
        purchase.hours_remaining = purchase.hours_total
        purchase.save(update_fields=['hours_remaining'])
        print(f"✨ Reset hours to: {purchase.hours_remaining} / {purchase.hours_total}")
    elif sub.coaching_purchase:
        # In case it's a coaching package
        cp = sub.coaching_purchase
        cp.sessions_remaining = cp.sessions_total
        cp.save(update_fields=['sessions_remaining'])
        print(f"✨ Reset sessions to: {cp.sessions_remaining} / {cp.sessions_total}")
        
    sub.current_period_start = new_start
    sub.current_period_end = new_end
    sub.save(update_fields=['current_period_start', 'current_period_end'])
    
    print(f"📅 Moved billing period forward: Ends on {sub.current_period_end.strftime('%B %d, %Y')}")
    print("\n✅ Auto-pay renewal simulation complete! Refresh your frontend to see the reset hours.")

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Simulate a Square Subscription Renewal (Auto-Pay)')
    parser.add_argument('subscription_id', type=str, help='The Square Subscription ID (e.g., af70e3a2-...)')
    
    args = parser.parse_args()
    simulate_renewal(args.subscription_id)
