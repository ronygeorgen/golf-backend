from rest_framework import viewsets, permissions
from rest_framework.decorators import action
from rest_framework.response import Response
from .models import Banner
from .serializers import BannerSerializer

class IsAdminOrSuperUser(permissions.BasePermission):
    def has_permission(self, request, view):
        return request.user and request.user.is_authenticated and (request.user.role in ['admin', 'superadmin'] or request.user.is_superuser)

class BannerViewSet(viewsets.ModelViewSet):
    queryset = Banner.objects.all().order_by('-created_at')
    serializer_class = BannerSerializer
    permission_classes = [IsAdminOrSuperUser]

    def get_permissions(self):
        if self.action in ['active']:
            return [permissions.AllowAny()]
        return [IsAdminOrSuperUser()]

    @action(detail=False, methods=['get'])
    def active(self, request):
        banner = Banner.objects.filter(is_active=True).first()
        if banner:
            serializer = self.get_serializer(banner)
            return Response(serializer.data)
        return Response({}) # Return empty object if no active banner
