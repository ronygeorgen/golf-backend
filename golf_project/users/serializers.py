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
    class Meta:
        model = User
        fields = ('id', 'username', 'email', 'phone', 'role', 'first_name', 'last_name', 'email_verified', 'phone_verified', 'is_superuser', 'is_staff', 'date_of_birth')
        read_only_fields = ('id', 'email_verified', 'phone_verified', 'is_superuser', 'is_staff', 'username')
        extra_kwargs = {
            'email': {'required': True},
            'phone': {'required': True},
            'first_name': {'required': True},
            'last_name': {'required': True},
        }
    
    def create(self, validated_data):
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
        
        # Create user without password (admin can set it later if needed)
        user = User.objects.create(**validated_data)
        
        # Set a default password (can be changed later)
        # Using phone as default password for now, but should be changed on first login
        user.set_password(validated_data.get('phone', 'default123'))
        user.save()
        
        return user
    
    def update(self, instance, validated_data):
        # Don't allow username updates through this serializer
        validated_data.pop('username', None)
        return super().update(instance, validated_data)

class SignupSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, required=True, validators=[validate_password])
    password_confirm = serializers.CharField(write_only=True, required=True)
    ghl_location_id = serializers.CharField(write_only=True, required=False, allow_blank=True, allow_null=True)
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
        if attrs['password'] != attrs['password_confirm']:
            raise serializers.ValidationError({"password": "Password fields didn't match."})
        
        # Check if email already exists
        if User.objects.filter(email=attrs['email']).exists():
            raise serializers.ValidationError({"email": "A user with this email already exists."})
        
        # Check if phone already exists
        if User.objects.filter(phone=attrs['phone']).exists():
            raise serializers.ValidationError({"phone": "A user with this phone number already exists."})
        
        return attrs
    
    def create(self, validated_data):
        validated_data.pop('password_confirm')
        password = validated_data.pop('password')
        
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
        user.set_password(password)
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
            is_closed, message = ClosedDay.check_if_closed(check_datetime)
            
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