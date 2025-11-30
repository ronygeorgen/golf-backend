from rest_framework import serializers
from django.utils import timezone
from datetime import timedelta
from .models import CoachingPackage, CoachingPackagePurchase, SessionTransfer, OrganizationPackageMember, TempPurchase, PendingRecipient
from users.serializers import UserSerializer
from users.models import User

class CoachingPackageSerializer(serializers.ModelSerializer):
    staff_members_details = UserSerializer(source='staff_members', many=True, read_only=True)
    
    class Meta:
        model = CoachingPackage
        fields = '__all__'
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Set staff_members field with queryset after initialization
        from users.models import User
        self.fields['staff_members'] = serializers.PrimaryKeyRelatedField(
            many=True,
            queryset=User.objects.filter(role__in=['staff', 'admin']),
            required=False,
            allow_null=True
        )
    
    def validate_staff_members(self, value):
        """Filter out null, None, or invalid staff member IDs"""
        if value is None:
            return []
        # Filter out null, None, and invalid values
        cleaned = [
            staff_id for staff_id in value 
            if staff_id is not None 
            and staff_id != 'null' 
            and staff_id != ''
        ]
        return cleaned
    
    def update(self, instance, validated_data):
        # Handle staff_members separately for ManyToMany relationship
        staff_members = validated_data.pop('staff_members', None)
        
        # Update other fields
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        
        # Update staff_members if provided
        if staff_members is not None:
            # Filter out any None values just to be safe
            cleaned_staff_members = [sm for sm in staff_members if sm is not None]
            instance.staff_members.set(cleaned_staff_members)
        
        return instance


class OrganizationPackageMemberSerializer(serializers.ModelSerializer):
    user_details = UserSerializer(source='user', read_only=True)
    
    class Meta:
        model = OrganizationPackageMember
        fields = ['id', 'phone', 'user', 'user_details', 'added_at']
        read_only_fields = ['user', 'added_at']


class CoachingPackagePurchaseSerializer(serializers.ModelSerializer):
    package_details = CoachingPackageSerializer(source='package', read_only=True)
    client_details = UserSerializer(source='client', read_only=True)
    original_owner_details = UserSerializer(source='original_owner', read_only=True)
    recipient_name = serializers.SerializerMethodField()
    organization_members = OrganizationPackageMemberSerializer(many=True, read_only=True)
    member_phones = serializers.ListField(
        child=serializers.CharField(),
        write_only=True,
        required=False,
        help_text="List of phone numbers for organization package members"
    )
    
    class Meta:
        model = CoachingPackagePurchase
        fields = [
            'id',
            'client',
            'client_details',
            'package',
            'package_details',
            'purchase_name',
            'sessions_total',
            'sessions_remaining',
            'notes',
            'purchase_type',
            'recipient_phone',
            'recipient_name',
            'gift_status',
            'gift_token',
            'original_owner',
            'original_owner_details',
            'package_status',
            'purchased_at',
            'updated_at',
            'gift_expires_at',
            'organization_members',
            'member_phones',
        ]
        read_only_fields = [
            'client', 'sessions_remaining', 'purchased_at', 'updated_at',
            'gift_token', 'original_owner', 'package_status'
        ]
    
    def get_recipient_name(self, obj):
        """Get recipient name if user exists"""
        if obj.recipient_phone:
            try:
                user = User.objects.get(phone=obj.recipient_phone)
                return user.get_full_name() or user.username
            except User.DoesNotExist:
                return None
        return None
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Make sessions_total optional - it will be set automatically from package
        if 'sessions_total' in self.fields:
            self.fields['sessions_total'].required = False
    
    def validate(self, attrs):
        package = attrs.get('package')
        sessions_total = attrs.get('sessions_total')
        purchase_type = attrs.get('purchase_type', 'normal')
        recipient_phone = attrs.get('recipient_phone')
        request = self.context.get('request')
        
        if not package:
            raise serializers.ValidationError("Package is required.")
        
        purchase_name = attrs.get('purchase_name')

        if not purchase_name or not purchase_name.strip():
            attrs['purchase_name'] = package.title if package else 'Coaching Package'
        else:
            attrs['purchase_name'] = purchase_name.strip()

        # Validate gift purchase
        if purchase_type == 'gift':
            if not recipient_phone:
                raise serializers.ValidationError({
                    'recipient_phone': 'Recipient phone number is required for gift purchases.'
                })
            
            # Check if recipient exists
            try:
                recipient = User.objects.get(phone=recipient_phone)
                # Don't allow gifting to yourself
                if request and request.user == recipient:
                    raise serializers.ValidationError({
                        'recipient_phone': 'You cannot gift a package to yourself.'
                    })
            except User.DoesNotExist:
                raise serializers.ValidationError({
                    'recipient_phone': 'Recipient with this phone number does not exist in the system.'
                })
        
        # Validate organization purchase
        if purchase_type == 'organization':
            member_phones = attrs.get('member_phones', [])
            if not member_phones or len(member_phones) == 0:
                raise serializers.ValidationError({
                    'member_phones': 'At least one member phone number is required for organization purchases.'
                })
            
            # Validate phone format and store (allow non-existing users)
            validated_phones = []
            existing_user_phones = []
            for phone in member_phones:
                phone = phone.strip()
                if not phone:
                    continue
                # Basic phone validation (non-empty, reasonable length)
                if len(phone) < 10 or len(phone) > 15:
                    raise serializers.ValidationError({
                        'member_phones': f'Invalid phone number format: {phone}. Phone must be 10-15 digits.'
                    })
                validated_phones.append(phone)
                # Check if user exists (optional - for setting user field)
                try:
                    User.objects.get(phone=phone)
                    existing_user_phones.append(phone)
                except User.DoesNotExist:
                    pass  # Non-existing users are allowed
            
            # Store validated phones for later use in create()
            attrs['_validated_member_phones'] = validated_phones
            attrs['_existing_user_phones'] = existing_user_phones
        
        # Clients cannot override sessions_total. Admins may optionally set one.
        if not sessions_total or (request and getattr(request.user, 'role', None) == 'client'):
            attrs['sessions_total'] = package.session_count
            sessions_total = attrs['sessions_total']
        
        if sessions_total < 1:
            raise serializers.ValidationError("sessions_total must be at least 1.")
        
        attrs['sessions_remaining'] = attrs['sessions_total']
        
        # Set gift-related fields
        if purchase_type == 'gift':
            attrs['gift_status'] = 'pending'
            attrs['original_owner'] = request.user if request else None
            # Gift expires in 30 days
            attrs['gift_expires_at'] = timezone.now() + timedelta(days=30)
        else:
            attrs['gift_status'] = None
            attrs['package_status'] = 'active'
        
        return attrs
    
    def create(self, validated_data):
        # Handle extra kwargs from serializer.save() calls (like client=recipient)
        # This is needed because DRF passes extra kwargs to create()
        purchase_type = validated_data.get('purchase_type', 'normal')
        member_phones = validated_data.pop('member_phones', [])
        validated_member_phones = validated_data.pop('_validated_member_phones', [])
        existing_user_phones = validated_data.pop('_existing_user_phones', [])
        
        # Generate gift token if it's a gift purchase
        if purchase_type == 'gift':
            instance = CoachingPackagePurchase(**validated_data)
            instance.gift_token = instance.generate_gift_token()
            instance.save()
            return instance
        
        # Handle organization purchase - create members
        instance = super().create(validated_data)
        
        if purchase_type == 'organization':
            from .models import PendingRecipient
            
            # Add purchaser as a member
            purchaser_phone = instance.client.phone
            OrganizationPackageMember.objects.get_or_create(
                package_purchase=instance,
                phone=purchaser_phone,
                defaults={'user': instance.client}
            )
            
            # Add other members
            for phone in validated_member_phones:
                if phone != purchaser_phone:  # Don't duplicate purchaser
                    try:
                        member_user = User.objects.get(phone=phone)
                        # User exists - create member with user reference
                        OrganizationPackageMember.objects.get_or_create(
                            package_purchase=instance,
                            phone=phone,
                            defaults={'user': member_user}
                        )
                    except User.DoesNotExist:
                        # User doesn't exist - create member without user and create PendingRecipient
                        OrganizationPackageMember.objects.get_or_create(
                            package_purchase=instance,
                            phone=phone
                        )
                        # Create PendingRecipient for signup conversion
                        PendingRecipient.objects.get_or_create(
                            package=instance.package,
                            buyer=instance.client,
                            recipient_phone=phone,
                            purchase_type='organization',
                            defaults={'status': 'pending'}
                        )
        
        return instance


class SessionTransferSerializer(serializers.ModelSerializer):
    from_user_details = UserSerializer(source='from_user', read_only=True)
    to_user_details = UserSerializer(source='to_user', read_only=True)
    package_purchase_details = CoachingPackagePurchaseSerializer(source='package_purchase', read_only=True)
    recipient_name = serializers.SerializerMethodField()
    
    class Meta:
        model = SessionTransfer
        fields = [
            'id',
            'from_user',
            'from_user_details',
            'to_user_phone',
            'to_user',
            'to_user_details',
            'recipient_name',
            'package_purchase',
            'package_purchase_details',
            'session_count',
            'transfer_status',
            'transfer_token',
            'notes',
            'created_at',
            'updated_at',
            'expires_at',
        ]
        read_only_fields = [
            'from_user', 'to_user', 'transfer_token', 'transfer_status',
            'created_at', 'updated_at'
        ]
    
    def get_recipient_name(self, obj):
        """Get recipient name if user exists"""
        if obj.to_user_phone:
            try:
                user = User.objects.get(phone=obj.to_user_phone)
                return user.get_full_name() or user.username
            except User.DoesNotExist:
                return None
        return None
    
    def validate(self, attrs):
        package_purchase = attrs.get('package_purchase')
        session_count = attrs.get('session_count')
        to_user_phone = attrs.get('to_user_phone')
        request = self.context.get('request')
        
        if not package_purchase:
            raise serializers.ValidationError("Package purchase is required.")
        
        # Validate package ownership
        if request and package_purchase.client != request.user:
            raise serializers.ValidationError({
                'package_purchase': 'You can only transfer sessions from your own packages.'
            })
        
        # Validate session count
        if session_count < 1:
            raise serializers.ValidationError({
                'session_count': 'Session count must be at least 1.'
            })
        
        if session_count > package_purchase.sessions_remaining:
            raise serializers.ValidationError({
                'session_count': f'Cannot transfer more sessions than available. Available: {package_purchase.sessions_remaining}'
            })
        
        # Check if recipient exists
        if to_user_phone:
            try:
                recipient = User.objects.get(phone=to_user_phone)
                # Don't allow transferring to yourself
                if request and request.user == recipient:
                    raise serializers.ValidationError({
                        'to_user_phone': 'You cannot transfer sessions to yourself.'
                    })
            except User.DoesNotExist:
                raise serializers.ValidationError({
                    'to_user_phone': 'Recipient with this phone number does not exist in the system.'
                })
        
        # Check if package can be transferred
        if not package_purchase.can_be_transferred:
            raise serializers.ValidationError({
                'package_purchase': 'This package cannot be transferred. It may be depleted, completed, or have a pending gift.'
            })
        
        # Transfer expires in 30 days
        attrs['expires_at'] = timezone.now() + timedelta(days=30)
        
        return attrs
    
    def create(self, validated_data):
        instance = SessionTransfer(**validated_data)
        instance.transfer_token = instance.generate_transfer_token()
        instance.save()
        return instance


class TempPurchaseSerializer(serializers.ModelSerializer):
    package_details = CoachingPackageSerializer(source='package', read_only=True)
    
    class Meta:
        model = TempPurchase
        fields = [
            'temp_id',
            'package',
            'package_details',
            'buyer_phone',
            'purchase_type',
            'recipients',
            'created_at',
            'expires_at',
        ]
        read_only_fields = ['temp_id', 'created_at', 'expires_at']
    
    def validate_recipients(self, value):
        """Validate recipients list"""
        if not isinstance(value, list):
            raise serializers.ValidationError("Recipients must be a list of phone numbers.")
        # Remove duplicates and empty strings
        cleaned = list(set([phone.strip() for phone in value if phone and phone.strip()]))
        return cleaned
    
    def validate(self, attrs):
        purchase_type = attrs.get('purchase_type', 'normal')
        recipients = attrs.get('recipients', [])
        
        if purchase_type == 'gift':
            if not recipients or len(recipients) != 1:
                raise serializers.ValidationError({
                    'recipients': 'Gift purchases require exactly one recipient phone number.'
                })
        elif purchase_type == 'organization':
            if not recipients or len(recipients) == 0:
                raise serializers.ValidationError({
                    'recipients': 'Organization purchases require at least one member phone number.'
                })
        elif purchase_type == 'normal':
            if recipients:
                raise serializers.ValidationError({
                    'recipients': 'Normal purchases should not have recipients.'
                })
        
        return attrs


class PendingRecipientSerializer(serializers.ModelSerializer):
    package_details = CoachingPackageSerializer(source='package', read_only=True)
    buyer_details = UserSerializer(source='buyer', read_only=True)
    
    class Meta:
        model = PendingRecipient
        fields = [
            'id',
            'package',
            'package_details',
            'buyer',
            'buyer_details',
            'recipient_phone',
            'purchase_type',
            'status',
            'temp_purchase',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['status', 'created_at', 'updated_at']

