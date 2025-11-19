from rest_framework import serializers
from datetime import datetime
from .models import Simulator, DurationPrice, SimulatorAvailability, SimulatorCredit
from users.serializers import UserSerializer

class SimulatorSerializer(serializers.ModelSerializer):
    class Meta:
        model = Simulator
        fields = '__all__'
    
    def validate(self, attrs):
        is_coaching = attrs.get('is_coaching_bay')
        if is_coaching is None and self.instance:
            is_coaching = self.instance.is_coaching_bay
        
        hourly_price = attrs.get('hourly_price')
        if hourly_price is None and self.instance:
            hourly_price = self.instance.hourly_price
        
        if not is_coaching:
            if hourly_price is None or hourly_price <= 0:
                raise serializers.ValidationError("Normal simulators require a positive hourly price.")
        else:
            attrs['hourly_price'] = None
        
        return attrs

class DurationPriceSerializer(serializers.ModelSerializer):
    class Meta:
        model = DurationPrice
        fields = '__all__'

class SimulatorAvailabilitySerializer(serializers.ModelSerializer):
    start_time = serializers.TimeField()
    end_time = serializers.TimeField()
    
    class Meta:
        model = SimulatorAvailability
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


class SimulatorCreditSerializer(serializers.ModelSerializer):
    client_details = UserSerializer(source='client', read_only=True)
    
    class Meta:
        model = SimulatorCredit
        fields = [
            'id',
            'client',
            'client_details',
            'status',
            'reason',
            'issued_by',
            'issued_at',
            'redeemed_at',
            'source_booking',
            'notes',
        ]
        read_only_fields = [
            'status',
            'reason',
            'issued_by',
            'issued_at',
            'redeemed_at',
            'source_booking',
        ]

