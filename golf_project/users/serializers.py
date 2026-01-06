from rest_framework import serializers
from django.contrib.auth import authenticate
from django.contrib.auth.password_validation import validate_password
from django.utils import timezone
from datetime import datetime, time as dt_time
from .models import User, StaffAvailability, StaffDayAvailability

class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = (
            'id',
            'username',
            'email',
            'phone',
            'role',
            'first_name',
            'last_name',
            'email_verified',
            'phone_verified',
            'is_superuser',
            'is_staff',
            'is_paused',
            'ghl_location_id',
            'ghl_contact_id',
            'date_of_birth',
        )
        read_only_fields = (
            'id',
            'email_verified',
            'phone_verified',
            'is_superuser',
            'is_staff',
            'username',
            'ghl_location_id',
            'ghl_contact_id',
        )
        extra_kwargs = {
            'date_of_birth': {'required': False, 'allow_null': True},
        }

class StaffSerializer(serializers.ModelSerializer):
    """Serializer for creating/updating staff members by admin - auto-generates username"""
    password = serializers.CharField(write_only=True, required=False, allow_blank=True)
    
    class Meta:
        model = User
        fields = ('id', 'username', 'email', 'phone', 'role', 'first_name', 'last_name', 'email_verified', 'phone_verified', 'is_superuser', 'is_staff', 'date_of_birth', 'password', 'ghl_location_id')
        read_only_fields = ('id', 'email_verified', 'phone_verified', 'is_superuser', 'is_staff', 'username')
        extra_kwargs = {
            'email': {'required': True},
            'phone': {'required': True},
            'first_name': {'required': True},
            'last_name': {'required': True},
            'ghl_location_id': {'required': False, 'allow_blank': True},
            'date_of_birth': {'required': False, 'allow_null': True},
        }
    
    def to_internal_value(self, data):
        """Handle empty strings for date_of_birth before validation"""
        # Convert empty string to None for date_of_birth
        if 'date_of_birth' in data and (data['date_of_birth'] == '' or data['date_of_birth'] is None):
            data['date_of_birth'] = None
        return super().to_internal_value(data)
    
    def validate(self, attrs):
        """Validate that email and phone are unique"""
        email = attrs.get('email')
        phone = attrs.get('phone')
        
        # Get instance if updating
        instance = self.instance
        
        # Check for duplicate email
        if email:
            email_query = User.objects.filter(email=email)
            if instance:
                email_query = email_query.exclude(pk=instance.pk)
            if email_query.exists():
                existing_user = email_query.first()
                raise serializers.ValidationError({
                    'email': f'A user with this email already exists. (Existing user: {existing_user.first_name} {existing_user.last_name} - {existing_user.phone})'
                })
        
        # Check for duplicate phone
        if phone:
            phone_query = User.objects.filter(phone=phone)
            if instance:
                phone_query = phone_query.exclude(pk=instance.pk)
            if phone_query.exists():
                existing_user = phone_query.first()
                raise serializers.ValidationError({
                    'phone': f'A user with this phone number already exists. (Existing user: {existing_user.first_name} {existing_user.last_name} - {existing_user.email})'
                })
        
        return attrs
    
    def create(self, validated_data):
        # Extract password if provided
        password = validated_data.pop('password', None)
        
        # Auto-generate username from email (before @ symbol)
        email = validated_data.get('email')
        if email:
            username = email.split('@')[0]
        else:
            # Fallback to phone if no email
            phone = validated_data.get('phone', '')
            username = phone.replace('+', '').replace('-', '').replace(' ', '')
        
        # Ensure username is unique
        base_username = username
        counter = 1
        while User.objects.filter(username=username).exists():
            username = f"{base_username}{counter}"
            counter += 1
        
        validated_data['username'] = username
        
        # Create user with error handling
        try:
            user = User.objects.create(**validated_data)
        except Exception as e:
            # Catch any database integrity errors and provide better messages
            error_msg = str(e)
            if 'phone' in error_msg.lower() or 'unique constraint' in error_msg.lower():
                raise serializers.ValidationError({
                    'phone': 'A user with this phone number already exists. Please use a different phone number.'
                })
            elif 'email' in error_msg.lower():
                raise serializers.ValidationError({
                    'email': 'A user with this email already exists. Please use a different email address.'
                })
            else:
                raise serializers.ValidationError({
                    'non_field_errors': [f'Error creating user: {error_msg}']
                })
        
        # Set password only if provided, otherwise set a random password
        if password:
            user.set_password(password)
        else:
            # Set a random password that won't be used (OTP-based login)
            import secrets
            user.set_password(secrets.token_urlsafe(32))
        user.save()
        
        return user
    
    def update(self, instance, validated_data):
        # Don't allow username updates through this serializer
        validated_data.pop('username', None)
        
        # Update with error handling
        try:
            return super().update(instance, validated_data)
        except Exception as e:
            # Catch any database integrity errors and provide better messages
            error_msg = str(e)
            if 'phone' in error_msg.lower() or 'unique constraint' in error_msg.lower():
                raise serializers.ValidationError({
                    'phone': 'A user with this phone number already exists. Please use a different phone number.'
                })
            elif 'email' in error_msg.lower():
                raise serializers.ValidationError({
                    'email': 'A user with this email already exists. Please use a different email address.'
                })
            else:
                raise serializers.ValidationError({
                    'non_field_errors': [f'Error updating user: {error_msg}']
                })

class SignupSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, required=False, allow_blank=True, validators=[validate_password])
    password_confirm = serializers.CharField(write_only=True, required=False, allow_blank=True)
    ghl_location_id = serializers.CharField(write_only=True, required=True, allow_blank=False)
    date_of_birth = serializers.DateField(required=False, allow_null=True)
    
    class Meta:
        model = User
        fields = ('email', 'password', 'password_confirm', 'phone', 'first_name', 'last_name', 'role', 'ghl_location_id', 'date_of_birth')
        extra_kwargs = {
            'phone': {'required': True},
            'email': {'required': True},
            'username': {'required': False, 'read_only': True},
        }
    
    def to_internal_value(self, data):
        """Handle empty strings for date_of_birth before validation"""
        # Convert empty string to None for date_of_birth
        if 'date_of_birth' in data and (data['date_of_birth'] == '' or data['date_of_birth'] is None):
            data['date_of_birth'] = None
        return super().to_internal_value(data)
    
    def validate(self, attrs):
        # Only validate password if both are provided
        password = attrs.get('password', '')
        password_confirm = attrs.get('password_confirm', '')
        
        if password or password_confirm:
            # If one is provided, both must be provided
            if not password or not password_confirm:
                raise serializers.ValidationError({"password": "Both password fields are required if password is set."})
            if password != password_confirm:
                raise serializers.ValidationError({"password": "Password fields didn't match."})
        
        # Validate location_id is provided and exists
        location_id = attrs.get('ghl_location_id')
        if not location_id:
            raise serializers.ValidationError({"ghl_location_id": "Location is required."})
        
        # Check if email already exists
        if User.objects.filter(email=attrs['email']).exists():
            raise serializers.ValidationError({"email": "A user with this email already exists."})
        
        # Check if phone already exists
        if User.objects.filter(phone=attrs['phone']).exists():
            raise serializers.ValidationError({"phone": "A user with this phone number already exists."})
        
        return attrs
    
    def create(self, validated_data):
        password_confirm = validated_data.pop('password_confirm', None)
        password = validated_data.pop('password', None)
        
        # Auto-generate username from email (before @ symbol)
        email = validated_data.get('email')
        username = email.split('@')[0]
        
        # Ensure username is unique
        base_username = username
        counter = 1
        while User.objects.filter(username=username).exists():
            username = f"{base_username}{counter}"
            counter += 1
        
        validated_data['username'] = username
        user = User.objects.create(**validated_data)
        
        # Set password only if provided, otherwise set a random password (user won't use it with OTP login)
        if password:
            user.set_password(password)
        else:
            # Set a random password that won't be used (OTP-based login)
            import secrets
            user.set_password(secrets.token_urlsafe(32))
        
        user.save()
        return user

class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField(required=False, allow_blank=True)
    phone = serializers.CharField(required=False, allow_blank=True)
    password = serializers.CharField(write_only=True, required=True)
    
    def validate(self, attrs):
        email = attrs.get('email')
        phone = attrs.get('phone')
        password = attrs.get('password')
        
        if not email and not phone:
            raise serializers.ValidationError("Either email or phone number is required.")
        
        if email and phone:
            raise serializers.ValidationError("Please provide either email or phone number, not both.")
        
        user = None
        if email:
            try:
                user_obj = User.objects.get(email=email)
                user = authenticate(username=user_obj.username, password=password)
            except User.DoesNotExist:
                user = None
        elif phone:
            try:
                user_obj = User.objects.get(phone=phone)
                user = authenticate(username=user_obj.username, password=password)
            except User.DoesNotExist:
                user = None
        
        if not user:
            raise serializers.ValidationError("Invalid credentials.")
        
        if not user.is_active:
            raise serializers.ValidationError("User account is disabled.")
        
        if user.is_paused:
            raise serializers.ValidationError("Your account has been paused. Please contact support.")
        
        attrs['user'] = user
        return attrs
    
class PhoneLoginSerializer(serializers.Serializer):
    phone = serializers.CharField()
    location_id = serializers.CharField(required=False, allow_blank=True)
    
class VerifyOTPSerializer(serializers.Serializer):
    phone = serializers.CharField()
    otp = serializers.CharField()
    location_id = serializers.CharField(required=False, allow_blank=True)

class StaffAvailabilitySerializer(serializers.ModelSerializer):
    start_time = serializers.TimeField()
    end_time = serializers.TimeField()
    day_of_week = serializers.IntegerField()
    
    class Meta:
        model = StaffAvailability
        fields = '__all__'
    
    def to_representation(self, instance):
        """Return UTC times as-is (no conversion)"""
        representation = super().to_representation(instance)
        # Format times as HH:MM for consistency
        if representation.get('start_time'):
            if isinstance(representation['start_time'], str):
                if len(representation['start_time'].split(':')) > 2:
                    representation['start_time'] = representation['start_time'][:5]
            else:
                representation['start_time'] = representation['start_time'].strftime('%H:%M')
        if representation.get('end_time'):
            if isinstance(representation['end_time'], str):
                if len(representation['end_time'].split(':')) > 2:
                    representation['end_time'] = representation['end_time'][:5]
            else:
                representation['end_time'] = representation['end_time'].strftime('%H:%M')
        return representation
    
    def to_internal_value(self, data):
        """Accept UTC times as-is (no conversion)"""
        if 'start_time' in data and data['start_time']:
            start_time_str = data['start_time']
            if isinstance(start_time_str, str):
                try:
                    time_obj = datetime.strptime(start_time_str, '%H:%M').time()
                    data['start_time'] = time_obj
                except ValueError:
                    pass
        if 'end_time' in data and data['end_time']:
            end_time_str = data['end_time']
            if isinstance(end_time_str, str):
                try:
                    time_obj = datetime.strptime(end_time_str, '%H:%M').time()
                    data['end_time'] = time_obj
                except ValueError:
                    pass
        return super().to_internal_value(data)


class StaffDayAvailabilitySerializer(serializers.ModelSerializer):
    start_time = serializers.TimeField()
    end_time = serializers.TimeField()
    date = serializers.DateField()
    
    class Meta:
        model = StaffDayAvailability
        fields = '__all__'
    
    def validate(self, attrs):
        """Check if the date/time is on a closed day"""
        date = attrs.get('date')
        start_time = attrs.get('start_time')
        
        if date and start_time:
            from admin_panel.models import ClosedDay
            from datetime import datetime as dt
            
            # Create datetime for checking
            check_datetime = timezone.make_aware(dt.combine(date, start_time))
            location_id = self.context.get('location_id') if hasattr(self, 'context') else None
            is_closed, message = ClosedDay.check_if_closed(check_datetime, location_id=location_id)
            
            if is_closed:
                raise serializers.ValidationError({
                    'date': message or "Facility is closed on this date/time. Staff availability cannot be set for closed days."
                })
        
        return attrs
    
    def to_representation(self, instance):
        """Return UTC times as-is (no conversion)"""
        representation = super().to_representation(instance)
        # Format times as HH:MM for consistency
        if representation.get('start_time'):
            if isinstance(representation['start_time'], str):
                if len(representation['start_time'].split(':')) > 2:
                    representation['start_time'] = representation['start_time'][:5]
            else:
                representation['start_time'] = representation['start_time'].strftime('%H:%M')
        if representation.get('end_time'):
            if isinstance(representation['end_time'], str):
                if len(representation['end_time'].split(':')) > 2:
                    representation['end_time'] = representation['end_time'][:5]
            else:
                representation['end_time'] = representation['end_time'].strftime('%H:%M')
        # Format date as YYYY-MM-DD
        if representation.get('date'):
            if isinstance(representation['date'], str):
                representation['date'] = representation['date']
            else:
                representation['date'] = representation['date'].strftime('%Y-%m-%d')
        return representation
    
    def to_internal_value(self, data):
        """Accept UTC times as-is (no conversion)"""
        if 'start_time' in data and data['start_time']:
            start_time_str = data['start_time']
            if isinstance(start_time_str, str):
                try:
                    time_obj = datetime.strptime(start_time_str, '%H:%M').time()
                    data['start_time'] = time_obj
                except ValueError:
                    pass
        if 'end_time' in data and data['end_time']:
            end_time_str = data['end_time']
            if isinstance(end_time_str, str):
                try:
                    time_obj = datetime.strptime(end_time_str, '%H:%M').time()
                    data['end_time'] = time_obj
                except ValueError:
                    pass
        return super().to_internal_value(data)