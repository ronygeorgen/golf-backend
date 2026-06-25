from rest_framework import serializers
from .models import GHLLocation


class GHLLocationSerializer(serializers.ModelSerializer):
    is_token_valid = serializers.SerializerMethodField()
    logo_url = serializers.SerializerMethodField()

    class Meta:
        model = GHLLocation
        fields = [
            'id',
            'location_id',
            'company_name',
            'timezone',
            'logo',
            'logo_url',
            # Invoice / contact details
            'contact_phone',
            'support_email',
            'business_id',
            'status',
            'webhook_url',
            'webhook_secret',
            'access_token',
            'refresh_token',
            'token_expires_at',
            'is_token_valid',
            'metadata',
            'onboarded_at',
            'created_at',
        ]
        read_only_fields = [
            'id', 'status', 'webhook_secret', 'token_expires_at',
            'is_token_valid', 'metadata', 'onboarded_at', 'created_at',
            'logo_url',
        ]
        extra_kwargs = {
            'access_token': {'write_only': True},
            'refresh_token': {'write_only': True},
            'logo': {'required': False},
        }

    def get_is_token_valid(self, obj):
        return obj.is_token_valid()

    def get_logo_url(self, obj):
        """Return the absolute URL for the logo if it exists."""
        if not obj.logo:
            return None
            
        from django.conf import settings
        request = self.context.get('request')
        
        if request:
            url = request.build_absolute_uri(obj.logo.url)
            # Fallback if reverse proxy headers aren't passing the real domain
            if 'localhost' in url and getattr(settings, 'BACKEND_BASE_URL', '') and 'localhost' not in settings.BACKEND_BASE_URL:
                return f"{settings.BACKEND_BASE_URL}{obj.logo.url}"
            return url
            
        # Fallback if no request in context
        if getattr(settings, 'BACKEND_BASE_URL', ''):
            return f"{settings.BACKEND_BASE_URL}{obj.logo.url}"
            
        return obj.logo.url


class GHLOnboardSerializer(serializers.Serializer):
    location_id = serializers.CharField(max_length=100)
    company_name = serializers.CharField(max_length=255, required=False, allow_blank=True)
    webhook_url = serializers.CharField(required=False, allow_blank=True)
