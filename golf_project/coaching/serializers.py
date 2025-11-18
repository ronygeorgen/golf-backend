from rest_framework import serializers
from .models import CoachingPackage
from users.serializers import UserSerializer

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

