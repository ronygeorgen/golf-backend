from rest_framework import serializers
from .models import GHLLocation


class GHLLocationSerializer(serializers.ModelSerializer):
    is_token_valid = serializers.SerializerMethodField()
    
    class Meta:
        model = GHLLocation
        fields = [
            'id',
            'location_id',
            'company_name',
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
            'id', 'status', 'webhook_secret', 'access_token', 'refresh_token',
            'token_expires_at', 'is_token_valid', 'metadata', 'onboarded_at', 'created_at'
        ]
        extra_kwargs = {
            'access_token': {'write_only': True},
            'refresh_token': {'write_only': True},
        }
    
    def get_is_token_valid(self, obj):
        return obj.is_token_valid()


class GHLOnboardSerializer(serializers.Serializer):
    location_id = serializers.CharField(max_length=100)
    company_name = serializers.CharField(max_length=255, required=False, allow_blank=True)
    webhook_url = serializers.CharField(required=False, allow_blank=True)

