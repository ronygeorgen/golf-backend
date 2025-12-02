from rest_framework import serializers
from decimal import Decimal
from coaching.models import CoachingPackagePurchase
from simulators.models import SimulatorCredit
from users.models import User


class ClientLookupMixin:
    def _resolve_client(self, attrs):
        client = None
        client_id = attrs.get('client_id')
        identifier = attrs.get('client_identifier')
        
        if client_id:
            client = User.objects.filter(id=client_id).first()
        if not client and identifier:
            lookup = identifier.strip()
            client = User.objects.filter(email__iexact=lookup).first()
            if not client and lookup.isdigit():
                client = User.objects.filter(phone=lookup).first()
        if not client:
            raise serializers.ValidationError("Client not found. Provide a valid client_id or client_identifier (email/phone).")
        attrs['client'] = client
        return attrs


class CoachingSessionAdjustmentSerializer(serializers.Serializer, ClientLookupMixin):
    client_id = serializers.IntegerField(required=False)
    client_identifier = serializers.CharField(required=False, allow_blank=True)
    package_purchase_id = serializers.IntegerField(required=False)
    package_id = serializers.IntegerField(required=False)
    session_count = serializers.IntegerField(min_value=1, default=1)
    simulator_hours = serializers.DecimalField(max_digits=6, decimal_places=2, min_value=Decimal('0'), required=False, default=Decimal('0'))
    note = serializers.CharField(required=False, allow_blank=True)
    
    def validate(self, attrs):
        attrs = self._resolve_client(attrs)
        client = attrs['client']
        purchase = None
        purchase_id = attrs.get('package_purchase_id')
        package_id = attrs.get('package_id')
        
        if purchase_id:
            purchase = CoachingPackagePurchase.objects.filter(id=purchase_id, client=client).first()
            if not purchase:
                raise serializers.ValidationError("Package purchase not found for this client.")
        elif package_id:
            purchase = CoachingPackagePurchase.objects.filter(
                client=client,
                package_id=package_id
            ).order_by('-purchased_at').first()
            if not purchase:
                raise serializers.ValidationError("The client does not have an active purchase for the selected package.")
        else:
            purchase = CoachingPackagePurchase.objects.filter(
                client=client
            ).order_by('-purchased_at').first()
            if not purchase:
                raise serializers.ValidationError("No purchases found for this client. Provide a package_id or purchase reference.")
        
        attrs['purchase'] = purchase
        return attrs


class SimulatorCreditGrantSerializer(serializers.Serializer, ClientLookupMixin):
    client_id = serializers.IntegerField(required=False)
    client_identifier = serializers.CharField(required=False, allow_blank=True)
    hours = serializers.DecimalField(max_digits=6, decimal_places=2, min_value=Decimal('0.01'), required=False)
    token_count = serializers.IntegerField(min_value=1, required=False)  # Deprecated, kept for backward compatibility
    reason = serializers.ChoiceField(choices=SimulatorCredit.Reason.choices, default=SimulatorCredit.Reason.MANUAL)
    note = serializers.CharField(required=False, allow_blank=True)
    
    def validate(self, attrs):
        attrs = self._resolve_client(attrs)
        # If hours not provided, use token_count (backward compatibility) or default to 1 hour
        if 'hours' not in attrs or attrs['hours'] is None:
            if 'token_count' in attrs and attrs['token_count'] is not None:
                attrs['hours'] = float(attrs['token_count'])  # Convert count to hours for backward compatibility
            else:
                attrs['hours'] = 1.0  # Default to 1 hour
        return attrs

