from decimal import Decimal, ROUND_HALF_UP
from rest_framework import viewsets, status
from rest_framework.pagination import PageNumberPagination
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.views import APIView
from rest_framework import serializers
from django.db import transaction
from django.db.models import Q, Sum, F
from django.utils import timezone
from django.conf import settings
from datetime import datetime, timedelta
import logging
from .models import Booking, TempBooking
from .serializers import BookingSerializer, BookingCreateSerializer
from users.models import User
from users.utils import get_location_id_from_request
from simulators.models import Simulator, SimulatorCredit
from coaching.models import (
    SimulatorPackagePurchase,
    CoachingPackagePurchase, 
    OrganizationPackageMember
)

logger = logging.getLogger(__name__)

class TenPerPagePagination(PageNumberPagination):
    page_size = 10
    page_query_param = 'page'
    page_size_query_param = None


class FivePerPagePagination(PageNumberPagination):
    page_size = 5
    page_query_param = 'page'
    page_size_query_param = None


class BookingViewSet(viewsets.ModelViewSet):
    serializer_class = BookingSerializer
    permission_classes = [IsAuthenticated]
    lock_window = timedelta(hours=24)
    pagination_class = TenPerPagePagination
    
    def get_permissions(self):
        """
        Override permissions to allow unauthenticated access for check_coaching_availability
        when phone parameter is provided (for guest users).
        """
        # Check if this is the check_coaching_availability action
        # Check both action name and path to be safe
        phone = self.request.query_params.get('phone')
        is_check_availability = (
            self.action == 'check_coaching_availability' or 
            'check_coaching_availability' in str(self.request.path)
        )
        if phone and is_check_availability:
            return [AllowAny()]
        return [IsAuthenticated()]
    
    def _check_special_event_conflict(self, check_datetime):
        """
        Check if a datetime conflicts with any active special event.
        Returns (has_conflict, event_title) tuple.
        """
        from special_events.models import SpecialEvent
        
        location_id = get_location_id_from_request(self.request)
        active_events = SpecialEvent.objects.filter(is_active=True)
        if location_id:
            active_events = active_events.filter(location_id=location_id)
        
        for event in active_events:
            if event.conflicts_with_datetime(check_datetime):
                return (True, event.title)
        return (False, None)
    
    def _check_closed_day(self, check_datetime):
        """
        Check if a datetime is on a closed day.
        Returns (is_closed, message) tuple.
        """
        from admin_panel.models import ClosedDay
        location_id = get_location_id_from_request(self.request)
        return ClosedDay.check_if_closed(check_datetime, location_id=location_id)
    
    def get_queryset(self):
        user = self.request.user
        location_id = get_location_id_from_request(self.request)
        
        # Admins, staff, and superadmins can see all bookings (filtered by location)
        if user.role in ['admin', 'staff', 'superadmin']:
            queryset = Booking.objects.all()
            if location_id:
                queryset = queryset.filter(location_id=location_id)
        else:
            # Clients can only see their own bookings
            queryset = Booking.objects.filter(client=user)
            if location_id:
                queryset = queryset.filter(location_id=location_id)
        
        # Apply filters
        status_filter = self.request.query_params.get('status')
        if status_filter:
            if status_filter == 'tpi_assessment':
                # Filter for TPI assessment bookings
                queryset = queryset.filter(is_tpi_assessment=True, booking_type='coaching')
            else:
                queryset = queryset.filter(status=status_filter)
        
        booking_type = self.request.query_params.get('booking_type')
        if booking_type:
            queryset = queryset.filter(booking_type=booking_type)
        
        start_date = self.request.query_params.get('start_date')
        end_date = self.request.query_params.get('end_date')
        if start_date and end_date:
            queryset = queryset.filter(
                start_time__date__gte=start_date,
                start_time__date__lte=end_date
            )
        
        # Search functionality - search by client name, phone, or email
        search = self.request.query_params.get('search')
        if search:
            queryset = queryset.filter(
                Q(client__first_name__icontains=search) |
                Q(client__last_name__icontains=search) |
                Q(client__phone__icontains=search) |
                Q(client__email__icontains=search)
            )
        
        # For admin dashboard - recent bookings
        recent = self.request.query_params.get('recent')
        if recent:
            queryset = queryset.order_by('-created_at')[:10]
        else:
            # Default ordering: latest created bookings first (by created_at descending)
            queryset = queryset.order_by('-created_at')
        
        return queryset.select_related(
            'client', 'simulator', 'coach', 'coaching_package'
        ).prefetch_related()
    
    def get_serializer_context(self):
        context = super().get_serializer_context()
        context['location_id'] = get_location_id_from_request(self.request)
        return context
    
    def get_serializer_class(self):
        if self.action in ['create', 'update']:
            return BookingCreateSerializer
        return BookingSerializer
    
    def create(self, request, *args, **kwargs):
        """Override create to handle temp booking creation for paid simulator bookings"""
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        # Initialize temp booking response marker
        self._temp_booking_response = None
        self._created_bookings = None
        
        try:
            self.perform_create(serializer)
            
            # Check if temp booking was created (for payment flow)
            if hasattr(self, '_temp_booking_response') and self._temp_booking_response:
                return Response(self._temp_booking_response, status=status.HTTP_200_OK)
            
            # Check if multiple bookings were created
            if hasattr(self, '_created_bookings') and self._created_bookings:
                # Return all created bookings
                booking_serializer = BookingSerializer(self._created_bookings, many=True)
                headers = self.get_success_headers(booking_serializer.data)
                return Response(booking_serializer.data, status=status.HTTP_201_CREATED, headers=headers)
            
            # Normal booking creation - use BookingSerializer to include all fields (including location_id)
            booking_instance = serializer.instance
            if booking_instance:
                booking_serializer = BookingSerializer(booking_instance)
                headers = self.get_success_headers(booking_serializer.data)
                return Response(booking_serializer.data, status=status.HTTP_201_CREATED, headers=headers)
            # Fallback to original serializer if instance not available
            headers = self.get_success_headers(serializer.data)
            return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)
        except Exception as e:
            # Check if this is a temp booking redirect exception (fallback)
            if hasattr(e, 'detail') and isinstance(e.detail, dict) and 'temp_id' in e.detail:
                return Response(e.detail, status=status.HTTP_200_OK)
            # Re-raise other exceptions
            raise
    
    def _find_optimal_simulator(self, start_time, end_time):
        """Assign the best simulator by filling gaps and balancing usage."""
        location_id = get_location_id_from_request(self.request)
        active_simulators = Simulator.objects.filter(
            is_active=True,
            is_coaching_bay=False
        )
        if location_id:
            active_simulators = active_simulators.filter(location_id=location_id)
        active_simulators = active_simulators.order_by('bay_number')
        
        best_choice = None
        for simulator in active_simulators:
            conflict_exists = Booking.objects.filter(
                simulator=simulator,
                start_time__lt=end_time,
                end_time__gt=start_time,
                status__in=['confirmed', 'completed'],
                booking_type='simulator'
            ).exists()
            
            if conflict_exists:
                continue
            
            previous_booking = Booking.objects.filter(
                simulator=simulator,
                end_time__lte=start_time,
                booking_type='simulator',
                status__in=['confirmed', 'completed']
            ).order_by('-end_time').first()
            
            next_booking = Booking.objects.filter(
                simulator=simulator,
                start_time__gte=end_time,
                booking_type='simulator',
                status__in=['confirmed', 'completed']
            ).order_by('start_time').first()
            
            gap_before = (start_time - previous_booking.end_time).total_seconds() / 60 if previous_booking else 24 * 60
            gap_after = (next_booking.start_time - end_time).total_seconds() / 60 if next_booking else 24 * 60
            day_usage = Booking.objects.filter(
                simulator=simulator,
                start_time__date=start_time.date(),
                status__in=['confirmed', 'completed']
            ).count()
            
            # Lower scores are preferred. Encourage filling tight gaps first, then balancing day usage.
            score = gap_before + gap_after + (day_usage * 15)
            
            choice = (score, day_usage, simulator.bay_number, simulator)
            if not best_choice or choice < best_choice:
                best_choice = choice
        
        return best_choice[-1] if best_choice else None
    
    def _find_multiple_available_simulators(self, start_time, end_time, count):
        """
        Find multiple available simulators for a given time slot.
        Returns a list of available simulators (up to count).
        """
        location_id = get_location_id_from_request(self.request)
        active_simulators = Simulator.objects.filter(
            is_active=True,
            is_coaching_bay=False
        )
        if location_id:
            active_simulators = active_simulators.filter(location_id=location_id)
        active_simulators = active_simulators.order_by('bay_number')
        
        available_simulators = []
        for simulator in active_simulators:
            if len(available_simulators) >= count:
                break
                
            conflict_exists = Booking.objects.filter(
                simulator=simulator,
                start_time__lt=end_time,
                end_time__gt=start_time,
                status__in=['confirmed', 'completed'],
                booking_type='simulator'
            ).exists()
            
            if not conflict_exists:
                available_simulators.append(simulator)
        
        return available_simulators if len(available_simulators) >= count else []

    def _determine_target_user(self):
        """Determine which user is the target for this booking"""
        # Default to current user
        target_user = self.request.user
        
        # If admin/staff/superadmin, check if a client_id/customer_id is provided
        if target_user.role in ['admin', 'staff', 'superadmin'] and hasattr(self, 'request'):
            client_id = self.request.data.get('client') or self.request.data.get('client_id')
            if client_id:
                try:
                    target_user = User.objects.get(id=client_id)
                except User.DoesNotExist:
                    raise serializers.ValidationError(f"User with ID {client_id} not found.")
        
        return target_user
    
    def _consume_package_session(self, package, use_organization=False, user=None):
        """
        Consume a session from a package purchase.
        
        Args:
            package: The CoachingPackage to consume from
            use_organization: If True, use organization packages; if False, use personal/gifted packages
            user: The user to consume the session from (defaults to request.user)
        """
        user = user or self.request.user
        
        if use_organization:
            # Find organization packages where user is a member
            # First get the IDs to avoid DISTINCT with FOR UPDATE issue
            member_purchase_ids = CoachingPackagePurchase.objects.filter(
                package=package,
                purchase_type='organization',
                sessions_remaining__gt=0,
                package_status='active',
                organization_members__phone=user.phone
            ).distinct().values_list('id', flat=True)
            
            if not member_purchase_ids:
                raise serializers.ValidationError(
                    "You do not have access to any organization packages for the selected package."
                )
            
            # Now use select_for_update on the specific IDs (first-come-first-served)
            purchase = CoachingPackagePurchase.objects.select_for_update().filter(
                id__in=member_purchase_ids
            ).order_by('purchased_at').first()
            
            if not purchase:
                raise serializers.ValidationError(
                    "No available organization packages found."
                )
        else:
            # Find an active package purchase for the user
            # Include normal purchases and accepted gifts
            purchase = CoachingPackagePurchase.objects.select_for_update().filter(
                client=user,
                package=package,
                sessions_remaining__gt=0,
                package_status='active'
            ).exclude(
                # Exclude pending gifts and organization packages
                gift_status='pending'
            ).exclude(
                purchase_type='organization'
            ).order_by('purchased_at').first()
            
            if not purchase:
                raise serializers.ValidationError(
                    "You do not have any remaining sessions for the selected package."
                )
        
        # Use the consume_session method which handles status updates
        purchase.consume_session(1)
        return purchase
    
    def _get_total_available_simulator_hours(self, use_organization=False, user=None):
        """
        Get total available simulator hours from all sources:
        - Simulator credits
        - Combo packages (coaching packages with simulator hours)
        - Simulator-only packages
        
        Args:
            use_organization: If True, also include organization packages where user is a member
            user: The user to check hours for (defaults to request.user)
            
        Returns:
            Decimal: Total available hours
        """
        from decimal import Decimal
        
        user = user or self.request.user
        total = Decimal('0')
        
        # 1. Simulator credits
        credits = SimulatorCredit.objects.filter(
            client=user,
            status=SimulatorCredit.Status.AVAILABLE
        ).aggregate(total=Sum('hours_remaining'))['total'] or Decimal('0')
        total += credits
        
        # 2. Combo packages (coaching packages with simulator hours)
        base_qs = CoachingPackagePurchase.objects.filter(
            simulator_hours_remaining__gt=0,
            package_status='active'
        ).exclude(gift_status='pending')
        
        if use_organization:
            org_purchase_ids = OrganizationPackageMember.objects.filter(
                Q(phone=user.phone) | Q(user=user)
            ).values_list('package_purchase_id', flat=True)
            
            combo_purchases = base_qs.filter(
                Q(client=user) | 
                Q(id__in=org_purchase_ids, purchase_type='organization')
            )
        else:
            combo_purchases = base_qs.filter(
                client=user
            ).exclude(purchase_type='organization')
        
        combo_hours = combo_purchases.aggregate(
            total=Sum('simulator_hours_remaining')
        )['total'] or Decimal('0')
        total += combo_hours
        
        # 3. Simulator-only packages
        sim_base_qs = SimulatorPackagePurchase.objects.filter(
            hours_remaining__gt=0,
            package_status='active'
        ).exclude(gift_status='pending')
        
        if use_organization:
            # For simulator-only packages, check if user is the client
            sim_purchases = sim_base_qs.filter(client=user)
        else:
            sim_purchases = sim_base_qs.filter(client=user)
        
        sim_hours = sim_purchases.aggregate(
            total=Sum('hours_remaining')
        )['total'] or Decimal('0')
        total += sim_hours
        
        return total
    
    def _consume_package_simulator_hours(self, duration_minutes, use_organization=False, booking_start_time=None, user=None):
        """
        Consume simulator hours from packages (combo or simulator-only).
        Priority: combo packages first, then non-restricted simulator packages, then restricted packages.
        
        Args:
            duration_minutes: Duration of the booking in minutes
            use_organization: If True, also check organization packages where user is a member
            booking_start_time: datetime object for the booking start time (required for checking restrictions)
            user: The user to consume hours from (defaults to request.user)
            
        Returns:
            Purchase object (CoachingPackagePurchase or SimulatorPackagePurchase) if hours were consumed, None otherwise
            
        Raises:
            serializers.ValidationError: If a restricted package limit has been exceeded
        """
        from decimal import Decimal
        from coaching.models import SimulatorPackageUsage
        from django.utils import timezone
        
        user = user or self.request.user
        hours_needed = Decimal(str(duration_minutes)) / Decimal('60')
        
        # First, try combo packages (coaching packages with simulator hours)
        base_qs = CoachingPackagePurchase.objects.select_for_update().filter(
            simulator_hours_remaining__gt=0,
            package_status='active'
        ).exclude(gift_status='pending')
        
        if use_organization:
            org_purchase_ids = OrganizationPackageMember.objects.filter(
                Q(phone=user.phone) | Q(user=user)
            ).values_list('package_purchase_id', flat=True)
            
            purchase = base_qs.filter(
                Q(client=user) | 
                Q(id__in=org_purchase_ids, purchase_type='organization')
            ).order_by('purchased_at').first()
        else:
            purchase = base_qs.filter(
                client=user
            ).exclude(purchase_type='organization').order_by('purchased_at').first()
        
        if purchase and purchase.simulator_hours_remaining >= hours_needed:
            purchase.consume_simulator_hours(hours_needed)
            return purchase
        
        # If no combo package or not enough hours, try simulator-only packages
        # Prioritize packages without restrictions
        sim_base_qs = SimulatorPackagePurchase.objects.select_for_update().filter(
            hours_remaining__gt=0,
            package_status='active'
        ).exclude(gift_status='pending').filter(client=user)
        
        # Check expiry date
        today = timezone.now().date()
        sim_base_qs = sim_base_qs.filter(
            Q(expiry_date__isnull=True) | Q(expiry_date__gte=today)
        )
        
        # Separate packages with and without restrictions
        packages_without_restrictions = []
        packages_with_restrictions = []
        
        for sim_purchase in sim_base_qs.order_by('purchased_at'):
            if not sim_purchase.package.has_time_restrictions:
                packages_without_restrictions.append(sim_purchase)
            else:
                packages_with_restrictions.append(sim_purchase)
        
        # First, try packages without restrictions
        for sim_purchase in packages_without_restrictions:
            if sim_purchase.hours_remaining >= hours_needed:
                sim_purchase.consume_hours(hours_needed)
                return sim_purchase
        
        # If no unrestricted packages available, try restricted packages
        # Only if booking_start_time is provided
        if booking_start_time:
            for sim_purchase in packages_with_restrictions:
                if sim_purchase.hours_remaining < hours_needed:
                    continue
                
                # Check if booking time matches any restrictions
                matching_restrictions = sim_purchase.package.get_matching_restrictions(booking_start_time)
                
                if not matching_restrictions.exists():
                    # No matching restrictions, can't use this package
                    continue
                
                # Check each matching restriction for limit
                can_use = True
                restriction_to_use = None
                
                for restriction in matching_restrictions:
                    # Sum existing hours used for this restriction on this day
                    from decimal import Decimal
                    usage_date = booking_start_time.date()
                    if restriction.is_recurring:
                        # For recurring, sum all hours used on this day of week
                        existing_usage_hours = SimulatorPackageUsage.objects.filter(
                            package_purchase=sim_purchase,
                            restriction=restriction,
                            usage_date=usage_date
                        ).aggregate(total=Sum('hours_used'))['total']
                        existing_usage_hours = Decimal(str(existing_usage_hours)) if existing_usage_hours else Decimal('0')
                    else:
                        # For non-recurring, sum hours used on the specific date
                        existing_usage_hours = SimulatorPackageUsage.objects.filter(
                            package_purchase=sim_purchase,
                            restriction=restriction,
                            usage_date=restriction.date
                        ).aggregate(total=Sum('hours_used'))['total']
                        existing_usage_hours = Decimal(str(existing_usage_hours)) if existing_usage_hours else Decimal('0')
                    
                    # Check if adding these hours would exceed the limit
                    if existing_usage_hours + hours_needed > restriction.limit_hours:
                        can_use = False
                        restriction_to_use = restriction
                        break
                    else:
                        restriction_to_use = restriction
                
                if can_use and restriction_to_use:
                    # Consume hours and track usage
                    sim_purchase.consume_hours(hours_needed)
                    # Usage will be tracked when booking is created (in perform_create)
                    # Store restriction info for later tracking
                    sim_purchase._used_restriction = restriction_to_use
                    return sim_purchase
                elif restriction_to_use:
                    # Limit exceeded
                    restriction_name = restriction_to_use.get_day_of_week_display() if restriction_to_use.is_recurring else str(restriction_to_use.date)
                    # Calculate how many hours are already used
                    from decimal import Decimal
                    usage_date = booking_start_time.date()
                    if restriction_to_use.is_recurring:
                        existing_usage_hours = SimulatorPackageUsage.objects.filter(
                            package_purchase=sim_purchase,
                            restriction=restriction_to_use,
                            usage_date=usage_date
                        ).aggregate(total=Sum('hours_used'))['total']
                        existing_usage_hours = Decimal(str(existing_usage_hours)) if existing_usage_hours else Decimal('0')
                    else:
                        existing_usage_hours = SimulatorPackageUsage.objects.filter(
                            package_purchase=sim_purchase,
                            restriction=restriction_to_use,
                            usage_date=restriction_to_use.date
                        ).aggregate(total=Sum('hours_used'))['total']
                        existing_usage_hours = Decimal(str(existing_usage_hours)) if existing_usage_hours else Decimal('0')
                    
                    remaining_hours = restriction_to_use.limit_hours - existing_usage_hours
                    raise serializers.ValidationError(
                        f"Package '{sim_purchase.package.title}' has reached its daily hour limit ({restriction_to_use.limit_hours} hrs) "
                        f"for {restriction_name} ({restriction_to_use.start_time} - {restriction_to_use.end_time}). "
                        f"Only {remaining_hours:.2f} hour(s) remaining. Please use a different package or book at a different time."
                    )
        else:
            # No booking_start_time provided, can't use restricted packages
            # Try unrestricted packages only (already tried above)
            pass
        
        return None
    
    def perform_create(self, serializer):
        booking_data = serializer.validated_data
        booking_type = booking_data.get('booking_type')
        use_simulator_credit = booking_data.pop('use_simulator_credit', False)
        use_organization_package = booking_data.pop('use_organization_package', False)
        simulator_count = booking_data.pop('simulator_count', 1)
        redeemed_credit = None
        
        # Determine target user
        target_user = self._determine_target_user()
        
        with transaction.atomic():
            if booking_type == 'simulator':
                start_time = booking_data.get('start_time')
                end_time = booking_data.get('end_time')
                
                if start_time and end_time:
                    # Handle multiple simulator bookings
                    if simulator_count > 1:
                        available_simulators = self._find_multiple_available_simulators(start_time, end_time, simulator_count)
                        if len(available_simulators) < simulator_count:
                            raise serializers.ValidationError(
                                f"Only {len(available_simulators)} simulator(s) available for this time slot. Requested: {simulator_count}"
                            )
                    else:
                        # Single simulator booking
                        assigned_simulator = booking_data.get('simulator') or self._find_optimal_simulator(start_time, end_time)
                        if not assigned_simulator:
                            raise serializers.ValidationError("No simulators available for this time slot")
                        available_simulators = [assigned_simulator]
                    
                    # User can choose: use pre-paid hours OR pay for one-off session
                    # Pre-paid hours include: credits + combo package hours + simulator-only package hours
                    from decimal import Decimal
                    duration_minutes = booking_data.get('duration_minutes', 0)
                    hours_needed = Decimal(str(duration_minutes)) / Decimal('60')
                    total_hours_needed = hours_needed * simulator_count  # Total hours for all simulators
                    
                    package_purchase = None
                    redeemed_credit = None
                    use_prepaid_hours = booking_data.get('use_prepaid_hours', None)
                    
                    if use_prepaid_hours is True:
                        # User explicitly wants to use pre-paid hours
                        # For multiple simulators, we need total_hours_needed
                        # Try credits first, then packages
                        try:
                            # Try to reserve credit for total hours needed
                            redeemed_credit = self._reserve_simulator_credit(total_hours_needed, user=target_user)
                        except serializers.ValidationError:
                            # No credits available, try package hours (combo or simulator-only)
                            # For multiple simulators, we need to consume hours for each simulator
                            # Calculate total duration in minutes for all simulators
                            total_duration_minutes = duration_minutes * simulator_count
                            package_purchase = self._consume_package_simulator_hours(total_duration_minutes, use_organization=True, booking_start_time=start_time, user=target_user)
                            if not package_purchase:
                                raise serializers.ValidationError("Insufficient pre-paid hours available")
                    elif use_prepaid_hours is False:
                        # User explicitly wants to pay - create temp booking and return redirect URL
                        # For multiple simulators, use the first simulator for redirect URL
                        first_simulator = available_simulators[0]
                        
                        # Calculate total price for all simulators
                        single_simulator_price = self._calculate_simulator_price(
                            first_simulator,
                            duration_minutes
                        )
                        calculated_price = single_simulator_price * simulator_count
                        
                        # Get location_id for temp booking
                        location_id = get_location_id_from_request(self.request)
                        
                        # Create temp booking - ensure it's saved and committed
                        # Store simulator_count in a way that can be retrieved later
                        # We'll use the first simulator for the temp booking
                        temp_booking = TempBooking(
                            simulator=first_simulator,
                            location_id=location_id,  # Store location_id so webhook can use it
                            buyer_phone=target_user.phone,
                            start_time=start_time,
                            end_time=end_time,
                            duration_minutes=duration_minutes,  # Store original duration per simulator
                            simulator_count=simulator_count,  # Store number of simulators
                            total_price=calculated_price
                        )
                        temp_booking.save()
                        
                        # Force database commit by refreshing from DB
                        temp_booking.refresh_from_db()
                        temp_id_str = str(temp_booking.temp_id)
                        
                        # Verify it was saved (within same transaction)
                        logger.info(f"Temp booking created in perform_create: temp_id={temp_id_str}, buyer={temp_booking.buyer_phone}, simulator_count={simulator_count}")
                        
                        # Get redirect URL - must be set for paid bookings
                        redirect_url = first_simulator.redirect_url
                        if not redirect_url:
                            raise serializers.ValidationError(
                                "This simulator does not have a redirect URL configured. Please contact support."
                            )
                        
                        # Store temp_id in instance for later retrieval
                        # Use a marker to indicate we should return redirect response
                        # Store simulator_count for webhook processing
                        self._temp_booking_response = {
                            'temp_id': temp_id_str,
                            'redirect_url': redirect_url,
                            'simulator_count': simulator_count,  # Store count for webhook
                            'message': 'Temporary booking created successfully. Redirect to payment.'
                        }
                        
                        # Don't raise exception - just return early
                        # The transaction will commit when we exit perform_create
                        # The create method will check for _temp_booking_response
                        return
                    else:
                        # Auto-detect: Try pre-paid hours first, fallback to payment
                        try:
                            # Try to reserve credit for total hours needed
                            redeemed_credit = self._reserve_simulator_credit(total_hours_needed, user=target_user)
                        except serializers.ValidationError:
                            # Calculate total duration in minutes for all simulators
                            total_duration_minutes = duration_minutes * simulator_count
                            try:
                                package_purchase = self._consume_package_simulator_hours(total_duration_minutes, use_organization=True, booking_start_time=start_time, user=target_user)
                            except serializers.ValidationError:
                                # Fallback to payment if no package or credit available
                                pass
                    
                    # Determine if package_purchase is a combo package or simulator-only package
                    simulator_package_purchase = None
                    combo_package_purchase = None
                    if package_purchase:
                        # Check if it's a SimulatorPackagePurchase
                        if isinstance(package_purchase, SimulatorPackagePurchase):
                            simulator_package_purchase = package_purchase
                        else:
                            # It's a CoachingPackagePurchase (combo package)
                            combo_package_purchase = package_purchase
                    
                    # Get location_id for bookings
                    location_id = get_location_id_from_request(self.request)
                    
                    # Create bookings for each simulator
                    created_bookings = []
                    for idx, simulator in enumerate(available_simulators):
                        # For multiple bookings, we need to handle credits/packages differently
                        # If using credits, we already consumed total_hours_needed, so don't consume again
                        # If using packages, we already consumed total_duration_minutes, so don't consume again
                        booking_instance = Booking(
                            client=target_user,
                            location_id=location_id,
                            booking_type='simulator',
                            simulator=simulator,
                            start_time=start_time,
                            end_time=end_time,
                            duration_minutes=duration_minutes,
                            total_price=Decimal('0.00'),
                            package_purchase=combo_package_purchase if idx == 0 else None,  # Only link to first booking
                            simulator_package_purchase=simulator_package_purchase if idx == 0 else None  # Only link to first booking
                        )
                        booking_instance.save()
                        created_bookings.append(booking_instance)
                    
                    # Handle credits and pricing for all bookings
                    if redeemed_credit:
                        # Credit hours were used (already consumed in _reserve_simulator_credit)
                        # Link credit to first booking only
                        created_bookings[0].simulator_credit_redemption = redeemed_credit
                        created_bookings[0].total_price = 0
                        created_bookings[0].save(update_fields=['simulator_credit_redemption', 'total_price', 'updated_at'])
                        # Set other bookings to 0 price as well
                        for booking in created_bookings[1:]:
                            booking.total_price = 0
                            booking.save(update_fields=['total_price', 'updated_at'])
                    elif package_purchase:
                        # Hours were consumed from package (combo or simulator-only), no charge
                        # Link package to first booking only
                        created_bookings[0].total_price = 0
                        update_fields = ['total_price', 'updated_at']
                        if combo_package_purchase:
                            update_fields.append('package_purchase')
                        if simulator_package_purchase:
                            update_fields.append('simulator_package_purchase')
                        created_bookings[0].save(update_fields=update_fields)
                        
                        # Track usage for time-restricted packages
                        if simulator_package_purchase and hasattr(simulator_package_purchase, '_used_restriction'):
                            from coaching.models import SimulatorPackageUsage
                            from decimal import Decimal
                            restriction = simulator_package_purchase._used_restriction
                            usage_date = start_time.date()
                            usage_time = start_time.time()
                            
                            # Calculate hours used (for multiple simulators, this is total hours)
                            hours_used = Decimal(str(duration_minutes)) / Decimal('60') * simulator_count
                            
                            # Create usage record for the first booking
                            SimulatorPackageUsage.objects.create(
                                package_purchase=simulator_package_purchase,
                                booking=created_bookings[0],
                                restriction=restriction,
                                usage_date=usage_date,
                                usage_time=usage_time,
                                hours_used=hours_used
                            )
                        
                        # Set other bookings to 0 price as well
                        for booking in created_bookings[1:]:
                            booking.total_price = 0
                            booking.save(update_fields=['total_price', 'updated_at'])
                    else:
                        # Charge the normal price for each booking
                        single_price = self._calculate_simulator_price(
                            available_simulators[0],
                            duration_minutes
                        )
                        for booking in created_bookings:
                            booking.total_price = single_price
                            booking.save(update_fields=['total_price', 'updated_at'])
                    
                    # Store created bookings for response
                    self._created_bookings = created_bookings
                    
                    # Update GHL custom fields after simulator booking creation
                    try:
                        from ghl.services import update_user_ghl_custom_fields
                        location_id = getattr(settings, 'GHL_DEFAULT_LOCATION', None)
                        update_user_ghl_custom_fields(target_user, location_id=location_id)
                    except Exception as exc:
                        logger.warning("Failed to update GHL custom fields after simulator booking creation: %s", exc)
                    
                    return
            elif booking_type == 'coaching':
                package = booking_data.get('coaching_package')
                if not package:
                    raise serializers.ValidationError("A coaching package is required for coaching bookings.")
                
                location_id = get_location_id_from_request(self.request)
                purchase = self._consume_package_session(package, use_organization=use_organization_package, user=target_user)
                # Check if package is TPI assessment
                is_tpi_assessment = package.is_tpi_assessment if hasattr(package, 'is_tpi_assessment') else False
                booking_instance = serializer.save(
                    client=target_user,
                    location_id=location_id,
                    package_purchase=purchase,
                    total_price=booking_data.get('total_price', 0),
                    is_tpi_assessment=is_tpi_assessment
                )
                # Ensure location_id is saved (in case serializer didn't include it)
                if location_id and not booking_instance.location_id:
                    booking_instance.location_id = location_id
                    booking_instance.save(update_fields=['location_id'])
                logger.info(f"Coaching booking created: id={booking_instance.id}, location_id={booking_instance.location_id}, client={target_user.phone}")
                
                # Update GHL custom fields after booking creation
                try:
                    from ghl.services import update_user_ghl_custom_fields
                    location_id = getattr(settings, 'GHL_DEFAULT_LOCATION', None)
                    update_user_ghl_custom_fields(target_user, location_id=location_id)
                except Exception as exc:
                    logger.warning("Failed to update GHL custom fields after coaching booking creation: %s", exc)
                
                return
            
            location_id = get_location_id_from_request(self.request)
            serializer.save(client=target_user, location_id=location_id)
    
    @action(detail=False, methods=['get'], url_path='available-simulator-hours')
    def available_simulator_hours(self, request):
        """
        Get total available simulator hours from all sources:
        - Simulator credits
        - Combo packages (coaching packages with simulator hours)
        - Simulator-only packages
        """
        use_organization = request.query_params.get('use_organization', 'false').lower() == 'true'
        target_user = request.user
        
        # Admin/Staff/Superadmin can query for other users
        if request.user.role in ['admin', 'staff', 'superadmin']:
            user_id = request.query_params.get('user_id')
            if user_id:
                try:
                    target_user = User.objects.get(id=user_id)
                except User.DoesNotExist:
                    return Response({'error': 'User not found'}, status=status.HTTP_404_NOT_FOUND)
        
        total_hours = self._get_total_available_simulator_hours(use_organization=use_organization, user=target_user)
        
        return Response({
            'total_available_hours': float(total_hours),
            'use_organization': use_organization,
            'user_id': target_user.id
        })
    
    @action(detail=False, methods=['get'])
    def upcoming(self, request):
        """Get all upcoming bookings for the current user, filtered by location_id.
        For clients: returns bookings where they are the client (both simulator and coaching).
        For admin/staff: returns bookings where they are EITHER the coach OR the client
        (staff can book sessions for themselves as clients).
        Requires location_id to be provided.
        """
        booking_type = request.query_params.get('booking_type')
        location_id = get_location_id_from_request(request)
        
        logger.info(f"Upcoming bookings request: user={request.user.phone}, role={request.user.role}, location_id={location_id}, booking_type={booking_type}")
        
        # Require location_id - return empty if not provided
        if not location_id:
            logger.warning(f"No location_id provided for upcoming bookings request from user {request.user.phone}")
            paginator = FivePerPagePagination()
            return paginator.get_paginated_response([])
        
        # Check if user is admin or staff (including superuser)
        is_admin_or_staff = (
            request.user.role in ['admin', 'staff'] or 
            getattr(request.user, 'is_superuser', False)
        )
        
        # For admin/staff, show bookings where they are EITHER the coach OR the client
        # (staff can book sessions for themselves as clients)
        # For clients, show bookings where they are the client (both simulator and coaching)
        if is_admin_or_staff:
            upcoming_bookings = Booking.objects.filter(
                Q(coach=request.user) | Q(client=request.user),
                start_time__gte=timezone.now()
            ).exclude(status='cancelled')
        else:
            upcoming_bookings = Booking.objects.filter(
                client=request.user,
                start_time__gte=timezone.now()
            ).exclude(status='cancelled')
        
        # Filter strictly by location_id - only show bookings with matching location_id
        upcoming_bookings = upcoming_bookings.filter(location_id=location_id)
        logger.info(f"Filtered by location_id={location_id}, count before type filter: {upcoming_bookings.count()}")
        
        # Filter by booking_type if provided
        if booking_type in ['simulator', 'coaching']:
            upcoming_bookings = upcoming_bookings.filter(booking_type=booking_type)
            logger.info(f"Filtered by booking_type={booking_type}, final count: {upcoming_bookings.count()}")
        
        upcoming_bookings = upcoming_bookings.order_by('start_time')
        
        # Use 5 per page pagination for upcoming bookings
        paginator = FivePerPagePagination()
        page = paginator.paginate_queryset(upcoming_bookings, request)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return paginator.get_paginated_response(serializer.data)
        
        serializer = self.get_serializer(upcoming_bookings, many=True)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def today(self, request):
        """Get today's bookings (for admin/staff/superadmin)"""
        if request.user.role not in ['admin', 'staff', 'superadmin']:
            return Response(
                {'error': 'Permission denied'}, 
                status=status.HTTP_403_FORBIDDEN
            )
        
        location_id = get_location_id_from_request(request)
        today = timezone.now().date()
        today_bookings = Booking.objects.filter(
            start_time__date=today
        )
        if location_id:
            today_bookings = today_bookings.filter(location_id=location_id)
        today_bookings = today_bookings.order_by('start_time')
        
        serializer = self.get_serializer(today_bookings, many=True)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def coaching_sessions_by_coach(self, request):
        """Get coaching sessions where a specific coach is assigned"""
        coach_id = request.query_params.get('coach_id')
        filter_type = request.query_params.get('filter', 'upcoming')  # 'upcoming' or 'completed'
        
        # Check if user is admin or staff (including superuser)
        is_admin_or_staff = (
            request.user.role in ['admin', 'staff'] or 
            getattr(request.user, 'is_superuser', False)
        )
        
        # If coach_id is provided, use it; otherwise use the current user (for staff/admin viewing their own sessions)
        if coach_id:
            # Admin/staff can view any coach's sessions
            if not is_admin_or_staff:
                return Response(
                    {'error': 'Permission denied'}, 
                    status=status.HTTP_403_FORBIDDEN
                )
            target_coach_id = coach_id
        else:
            # Staff/admin viewing their own sessions
            if not is_admin_or_staff:
                return Response(
                    {'error': 'Permission denied'}, 
                    status=status.HTTP_403_FORBIDDEN
                )
            target_coach_id = request.user.id
        
        # Base queryset for coaching sessions
        sessions = Booking.objects.filter(
            booking_type='coaching',
            coach_id=target_coach_id
        ).exclude(status='cancelled')
        
        # Filter by upcoming or completed
        if filter_type == 'completed':
            # Completed sessions: status is 'completed' or start_time is in the past
            sessions = sessions.filter(
                Q(status='completed') | Q(start_time__lt=timezone.now())
            ).order_by('-start_time')  # Most recent first
        else:
            # Upcoming sessions: start_time is in the future
            sessions = sessions.filter(
                start_time__gte=timezone.now()
            ).order_by('start_time')  # Earliest first
        
        # Use 5 per page pagination
        paginator = FivePerPagePagination()
        page = paginator.paginate_queryset(sessions, request)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return paginator.get_paginated_response(serializer.data)
        
        serializer = self.get_serializer(sessions, many=True)
        return Response(serializer.data)
    
    def _is_admin(self, user):
        return getattr(user, 'role', None) in ['admin', 'superadmin'] or getattr(user, 'is_superuser', False)
    
    def _lock_applies(self, booking):
        return booking.start_time - timezone.now() < self.lock_window
    
    def _user_can_manage_booking(self, user, booking):
        if user.role in ['admin', 'staff', 'superadmin']:
            return True
        return booking.client_id == user.id
    
    def _reserve_simulator_credit(self, hours_needed, user=None):
        """
        Reserve and consume hours from available simulator credits.
        Supports aggregating multiple small credits and splitting large credits
        to satisfy the requested duration.
        """
        from decimal import Decimal
        hours_needed = Decimal(str(hours_needed))
        user = user or self.request.user
        
        # Find credits with available hours, ordered by oldest first
        credits = SimulatorCredit.objects.select_for_update().filter(
            status=SimulatorCredit.Status.AVAILABLE,
            client=user,
            hours_remaining__gt=0
        ).order_by('issued_at')
        
        # Calculate total available
        total_available = sum(c.hours_remaining for c in credits)
        if total_available < hours_needed:
             raise serializers.ValidationError(
                f"Insufficient credit hours. Available: {total_available}, Needed: {hours_needed}"
            )

        # Collect credits to consume
        consumed_credits = []
        collected_amount = Decimal('0')
        
        for credit in credits:
            consumed_credits.append(credit)
            collected_amount += credit.hours_remaining
            if collected_amount >= hours_needed:
                break
        
        # Mark collected credits as used (consolidated)
        for credit in consumed_credits:
            credit.status = SimulatorCredit.Status.REDEEMED
            credit.notes = f"{credit.notes} (Consolidated)"[:255]
            credit.hours_remaining = Decimal('0') 
            credit.save(update_fields=['status', 'notes', 'hours_remaining'])
            
            # Note: We don't need to unlink from 'redeemed_booking' here because 
            # we are not reusing these specific credit records for the new booking.
            # We are creating a fresh one below.

        # Handle the "Change" (Leftover)
        change_amount = collected_amount - hours_needed
        if change_amount > 0:
            SimulatorCredit.objects.create(
                client=user,
                hours=change_amount,
                hours_remaining=change_amount,
                status=SimulatorCredit.Status.AVAILABLE,
                reason=SimulatorCredit.Reason.MANUAL,
                notes="Remaining balance from credit consolidation",
                # Preserve issuer if possible, or leave blank (system)
            )
            
        # Create the Payment Credit (Clean, unlinked)
        payment_credit = SimulatorCredit.objects.create(
            client=user,
            hours=hours_needed,
            hours_remaining=hours_needed, 
            status=SimulatorCredit.Status.AVAILABLE,
            reason=SimulatorCredit.Reason.MANUAL,
            notes="Consolidated credit for booking"
        )
        
        # Consume it immediately
        payment_credit.consume_hours(hours_needed)
        return payment_credit

    def _calculate_simulator_price(self, simulator, duration_minutes):
        if not simulator or not duration_minutes:
            return Decimal('0.00')
        if simulator.is_coaching_bay:
            return Decimal('0.00')
        if simulator.hourly_price:
            hours = Decimal(duration_minutes) / Decimal(60)
            price = (Decimal(simulator.hourly_price) * hours).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            return price
        from simulators.models import DurationPrice
        try:
            duration_price = DurationPrice.objects.get(duration_minutes=duration_minutes)
            return Decimal(duration_price.price)
        except DurationPrice.DoesNotExist:
            return Decimal('0.00')
    
    def _restore_coaching_session(self, booking):
        purchase = booking.package_purchase
        if not purchase:
            return None
        purchase.sessions_remaining = F('sessions_remaining') + 1
        purchase.save(update_fields=['sessions_remaining', 'updated_at'])
        purchase.refresh_from_db(fields=['sessions_remaining'])
        return purchase.sessions_remaining
    
    def _issue_simulator_credit(self, booking, issued_by=None, reason=SimulatorCredit.Reason.CANCELLATION):
        """
        Issue a simulator credit with hours equal to the cancelled booking duration.
        
        Args:
            booking: The cancelled booking
            issued_by: User who issued the credit (for admin overrides)
            reason: Reason for issuing the credit
            
        Returns:
            SimulatorCredit: The created credit
        """
        from decimal import Decimal
        hours = Decimal(str(booking.duration_minutes)) / Decimal('60')
        
        credit = SimulatorCredit.objects.create(
            client=booking.client,
            reason=reason,
            hours=hours,
            hours_remaining=hours,
            issued_by=issued_by if issued_by and self._is_admin(issued_by) else None,
            source_booking=booking,
            notes=f"Credit issued for booking #{booking.id} ({hours} hours)"
        )
        return credit
    
    def update(self, request, *args, **kwargs):
        booking = self.get_object()
        if not self._user_can_manage_booking(request.user, booking):
            return Response(
                {'error': 'Permission denied'},
                status=status.HTTP_403_FORBIDDEN
            )
        if self._lock_applies(booking) and not self._is_admin(request.user):
            return Response(
                {'error': 'Bookings within 24 hours cannot be modified. Contact an admin for assistance.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        return super().update(request, *args, **kwargs)
    
    def partial_update(self, request, *args, **kwargs):
        booking = self.get_object()
        if not self._user_can_manage_booking(request.user, booking):
            return Response(
                {'error': 'Permission denied'},
                status=status.HTTP_403_FORBIDDEN
            )
        if self._lock_applies(booking) and not self._is_admin(request.user):
            return Response(
                {'error': 'Bookings within 24 hours cannot be modified. Contact an admin for assistance.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        return super().partial_update(request, *args, **kwargs)
    
    @action(detail=True, methods=['post'])
    def update_status(self, request, pk=None):
        """Update booking status"""
        booking = self.get_object()
        new_status = request.data.get('status')
        
        if not new_status:
            return Response(
                {'error': 'Status is required'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        valid_statuses = [choice[0] for choice in Booking.STATUS_CHOICES]
        if new_status not in valid_statuses:
            return Response(
                {'error': f'Invalid status. Must be one of: {", ".join(valid_statuses)}'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        booking.status = new_status
        booking.save()
        
        # Log status change
        print(f"Booking {booking.id} status changed to {new_status} by {request.user}")
        
        return Response({
            'message': f'Booking status updated to {new_status}',
            'status': new_status
        })
    
    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """Cancel a booking"""
        booking = self.get_object()
        force_override_value = request.data.get('force_override', False)
        force_override = str(force_override_value).lower() in ['1', 'true', 'yes']
        
        if not self._user_can_manage_booking(request.user, booking):
            return Response(
                {'error': 'Permission denied'}, 
                status=status.HTTP_403_FORBIDDEN
            )
        
        if booking.status == 'cancelled':
            serializer = self.get_serializer(booking)
            return Response({
                'message': 'Booking already cancelled',
                'booking': serializer.data
            }, status=status.HTTP_200_OK)
        
        lock_applies = self._lock_applies(booking)
        if lock_applies and not (force_override and self._is_admin(request.user)):
            return Response(
                {
                    'error': 'This booking starts within 24 hours and cannot be cancelled online. Contact an admin for assistance.',
                    'lock_applies': True
                },
                status=status.HTTP_400_BAD_REQUEST
            )
        
        with transaction.atomic():
            booking.status = 'cancelled'
            booking.save(update_fields=['status', 'updated_at'])
            restitution = {}
            
            if booking.booking_type == 'coaching':
                remaining = self._restore_coaching_session(booking)
                restitution['sessions_remaining'] = remaining
            elif booking.booking_type == 'simulator':
                from decimal import Decimal
                hours_to_restore = Decimal(str(booking.duration_minutes)) / Decimal('60')
                
                # Case 1: Booking used combo package hours -> restore to same package
                if booking.package_purchase and not booking.simulator_credit_redemption and not booking.simulator_package_purchase:
                    purchase = booking.package_purchase
                    purchase.simulator_hours_remaining = F('simulator_hours_remaining') + hours_to_restore
                    purchase.save(update_fields=['simulator_hours_remaining', 'updated_at'])
                    purchase.refresh_from_db(fields=['simulator_hours_remaining'])
                    restitution['simulator_hours_restored'] = float(purchase.simulator_hours_remaining)
                # Case 2: Booking used simulator-only package hours -> add to credits (per requirement)
                elif booking.simulator_package_purchase:
                    # When simulator-only package booking is cancelled, add hours to credits
                    credit = self._issue_simulator_credit(
                        booking,
                        issued_by=request.user if force_override and self._is_admin(request.user) else None,
                        reason=SimulatorCredit.Reason.CANCELLATION
                    )
                    restitution['simulator_credit_id'] = credit.id
                    restitution['simulator_credit_hours'] = float(credit.hours)
                # Case 3: Booking used credit hours -> restore to credit
                elif booking.simulator_credit_redemption:
                    credit = booking.simulator_credit_redemption
                    credit.hours_remaining = F('hours_remaining') + hours_to_restore
                    credit.status = SimulatorCredit.Status.AVAILABLE
                    credit.redeemed_at = None
                    credit.save(update_fields=['hours_remaining', 'status', 'redeemed_at'])
                    
                    # Remove the link from the booking to the credit so the credit can be reused
                    # (Booking.simulator_credit_redemption is OneToOne, so valid for only one booking)
                    booking.simulator_credit_redemption = None
                    booking.save(update_fields=['simulator_credit_redemption'])
                    
                    credit.refresh_from_db(fields=['hours_remaining', 'status'])
                    restitution['simulator_credit_hours_restored'] = float(credit.hours_remaining)
                # Case 4: Booking was paid (no package, no credit) -> issue credit with exact hours
                else:
                    credit = self._issue_simulator_credit(
                        booking,
                        issued_by=request.user if force_override and self._is_admin(request.user) else None
                    )
                    restitution['simulator_credit_id'] = credit.id
                    restitution['simulator_credit_hours'] = float(credit.hours)
        
        # Update GHL custom fields after booking cancellation
        try:
            from ghl.services import update_user_ghl_custom_fields
            location_id = getattr(settings, 'GHL_DEFAULT_LOCATION', None)
            update_user_ghl_custom_fields(booking.client, location_id=location_id)
        except Exception as exc:
            logger.warning("Failed to update GHL custom fields after booking cancellation: %s", exc)
        
        serializer = self.get_serializer(booking)
        return Response({
            'message': 'Booking cancelled successfully',
            'booking': serializer.data,
            'lock_applies': lock_applies,
            'lock_overridden': lock_applies and force_override and self._is_admin(request.user),
            'restitution': restitution
        })

    @action(detail=True, methods=['post'])
    def reschedule(self, request, pk=None):
        """Reschedule a booking to a new time"""
        booking = self.get_object()
        force_override_value = request.data.get('force_override', False)
        force_override = str(force_override_value).lower() in ['1', 'true', 'yes']
        
        if not self._user_can_manage_booking(request.user, booking):
            return Response(
                {'error': 'Permission denied'},
                status=status.HTTP_403_FORBIDDEN
            )
        
        lock_applies = self._lock_applies(booking)
        if lock_applies and not (force_override and self._is_admin(request.user)):
            return Response(
                {
                    'error': 'Bookings within 24 hours cannot be rescheduled online. Contact an admin for assistance.',
                    'lock_applies': True
                },
                status=status.HTTP_400_BAD_REQUEST
            )
        
        incoming_data = request.data.copy()
        incoming_data['booking_type'] = booking.booking_type
        incoming_data.setdefault('duration_minutes', booking.duration_minutes)
        if booking.booking_type == 'coaching':
            if not booking.coaching_package:
                return Response({'error': 'This coaching booking is missing its package reference.'}, status=status.HTTP_400_BAD_REQUEST)
            incoming_data.setdefault('coaching_package', booking.coaching_package_id)
            if booking.coach_id:
                incoming_data.setdefault('coach', booking.coach_id)
        elif booking.booking_type == 'simulator' and booking.simulator_id:
            incoming_data.setdefault('simulator', booking.simulator_id)
        
        serializer = BookingCreateSerializer(
            data=incoming_data,
            context={'exclude_booking_id': booking.id}
        )
        serializer.is_valid(raise_exception=True)
        validated = serializer.validated_data
        
        with transaction.atomic():
            booking.start_time = validated['start_time']
            booking.end_time = validated['end_time']
            booking.duration_minutes = validated.get('duration_minutes', booking.duration_minutes)
            
            update_fields = ['start_time', 'end_time', 'duration_minutes', 'updated_at']
            if booking.booking_type == 'simulator':
                assigned_simulator = validated.get('simulator')
                if not assigned_simulator:
                    assigned_simulator = self._find_optimal_simulator(booking.start_time, booking.end_time)
                if not assigned_simulator:
                    raise serializers.ValidationError("No simulators available for this time slot")
                booking.simulator = assigned_simulator
                update_fields.append('simulator')
            elif booking.booking_type == 'coaching':
                if validated.get('coach'):
                    booking.coach = validated['coach']
                    update_fields.append('coach')

            if booking.booking_type == 'simulator' and not booking.simulator_credit_redemption_id:
                booking.total_price = self._calculate_simulator_price(
                    booking.simulator,
                    booking.duration_minutes
                )
                update_fields.append('total_price')
            
            booking.save(update_fields=update_fields)
        
        serializer = self.get_serializer(booking)
        return Response({
            'message': 'Booking rescheduled successfully',
            'booking': serializer.data,
            'lock_applies': lock_applies,
            'lock_overridden': lock_applies and force_override and self._is_admin(request.user)
        })
    
    @action(detail=False, methods=['get'])
    def calendar_events(self, request):
        """Get bookings for calendar view"""
        start_date = request.query_params.get('start_date')
        end_date = request.query_params.get('end_date')
        booking_type = request.query_params.get('booking_type')  # 'simulator' or 'coaching'
        
        if not start_date or not end_date:
            return Response(
                {'error': 'start_date and end_date are required'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            # Handle ISO format with 'Z' or timezone offset
            if start_date.endswith('Z'):
                start_date = start_date.replace('Z', '+00:00')
            if end_date.endswith('Z'):
                end_date = end_date.replace('Z', '+00:00')
            
            # Parse ISO format datetime
            if 'T' in start_date:
                start_datetime = datetime.fromisoformat(start_date)
            else:
                # If just date, assume start of day
                start_datetime = datetime.fromisoformat(f"{start_date}T00:00:00+00:00")
            
            if 'T' in end_date:
                end_datetime = datetime.fromisoformat(end_date)
            else:
                # If just date, assume end of day
                end_datetime = datetime.fromisoformat(f"{end_date}T23:59:59+00:00")
            
            # Make timezone aware if not already
            if timezone.is_naive(start_datetime):
                start_datetime = timezone.make_aware(start_datetime)
            if timezone.is_naive(end_datetime):
                end_datetime = timezone.make_aware(end_datetime)
                
        except (ValueError, AttributeError) as e:
            return Response(
                {'error': f'Invalid date format: {str(e)}'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Get coach_id filter if provided
        coach_id = request.query_params.get('coach_id')
        
        # Get location_id for filtering
        location_id = get_location_id_from_request(request)
        
        # For clients, only show their bookings
        if request.user.role == 'client':
            bookings = Booking.objects.filter(
                client=request.user,
                start_time__gte=start_datetime,
                end_time__lte=end_datetime
            )
            if location_id:
                # Filter strictly by location_id - only show bookings with matching location_id
                bookings = bookings.filter(location_id=location_id)
        else:
            # Admins and staff see all bookings (filtered by location)
            bookings = Booking.objects.filter(
                start_time__gte=start_datetime,
                end_time__lte=end_datetime
            )
            if location_id:
                # Filter strictly by location_id - only show bookings with matching location_id
                bookings = bookings.filter(location_id=location_id)
        
        # Filter by booking_type if provided
        if booking_type:
            if booking_type not in ['simulator', 'coaching']:
                return Response(
                    {'error': 'booking_type must be either "simulator" or "coaching"'}, 
                    status=status.HTTP_400_BAD_REQUEST
                )
            bookings = bookings.filter(booking_type=booking_type)
        
        # Filter by coach_id if provided (for viewing specific coach's sessions)
        if coach_id:
            if request.user.role == 'client':
                return Response(
                    {'error': 'Permission denied'}, 
                    status=status.HTTP_403_FORBIDDEN
                )
            bookings = bookings.filter(coach_id=coach_id)
        
        serializer = self.get_serializer(bookings, many=True)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def stats(self, request):
        """Get booking statistics (admin only)"""
        if request.user.role not in ['admin', 'staff', 'superadmin']:
            return Response(
                {'error': 'Permission denied'}, 
                status=status.HTTP_403_FORBIDDEN
            )
        
        location_id = get_location_id_from_request(request)
        today = timezone.now().date()
        week_ago = today - timedelta(days=7)
        
        bookings_qs = Booking.objects.all()
        if location_id:
            bookings_qs = bookings_qs.filter(location_id=location_id)
        
        stats = {
            'total_bookings': bookings_qs.count(),
            'today_bookings': bookings_qs.filter(start_time__date=today).count(),
            'week_bookings': bookings_qs.filter(start_time__date__gte=week_ago).count(),
            'simulator_bookings': bookings_qs.filter(booking_type='simulator').count(),
            'coaching_bookings': bookings_qs.filter(booking_type='coaching').count(),
            'revenue_today': bookings_qs.filter(
                start_time__date=today
            ).aggregate(total=Sum('total_price'))['total'] or 0,
            'revenue_week': bookings_qs.filter(
                start_time__date__gte=week_ago
            ).aggregate(total=Sum('total_price'))['total'] or 0,
        }
        
        return Response(stats)
    
    @action(detail=False, methods=['get'])
    def check_simulator_availability(self, request):
        """Check available time slots for simulator booking"""
        from simulators.models import SimulatorAvailability
        
        date_str = request.query_params.get('date')
        duration_minutes = request.query_params.get('duration', 60)
        simulator_count = request.query_params.get('simulator_count', 1)  # Default to 1 for backward compatibility
        show_bay_details = request.user.role in ['admin', 'staff']
        
        if not date_str:
            return Response(
                {'error': 'Date is required'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            duration_minutes = int(duration_minutes)
            simulator_count = int(simulator_count)
            if simulator_count < 1:
                return Response(
                    {'error': 'simulator_count must be at least 1'}, 
                    status=status.HTTP_400_BAD_REQUEST
                )
            booking_date = datetime.strptime(date_str, '%Y-%m-%d').date()
            # Get day of week (0=Monday, 6=Sunday)
            day_of_week = booking_date.weekday()
        except (ValueError, TypeError):
            return Response(
                {'error': 'Invalid date, duration, or simulator_count format'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Get location_id for filtering
        location_id = get_location_id_from_request(request)
        
        # Get all active simulators (bays 1-5, excluding coaching bay)
        available_simulators = Simulator.objects.filter(
            is_active=True,
            is_coaching_bay=False
        )
        if location_id:
            available_simulators = available_simulators.filter(location_id=location_id)
        available_simulators = available_simulators.order_by('bay_number')
        
        max_available_simulators = available_simulators.count()
        
        if not available_simulators.exists():
            # Still try to get hourly_price even if no simulators are available
            hourly_price = None
            return Response({
                'available_slots': [],
                'message': 'No simulators available',
                'hourly_price': hourly_price,
                'max_available_simulators': 0
            })
        
        # Validate simulator_count doesn't exceed available simulators
        if simulator_count > max_available_simulators:
            return Response(
                {'error': f'simulator_count ({simulator_count}) cannot exceed available simulators ({max_available_simulators})'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Get simulator availability for this day of week
        simulator_availabilities = SimulatorAvailability.objects.filter(
            simulator__in=available_simulators,
            day_of_week=day_of_week
        ).select_related('simulator').order_by('simulator', 'start_time')
        
        if not simulator_availabilities.exists():
            return Response({
                'available_slots': [],
                'message': 'No simulators available for this day'
            })
        
        slot_interval = 30  # minutes
        available_slots = []
        
        # Group availability by simulator
        availability_by_simulator = {}
        for avail in simulator_availabilities:
            if avail.simulator not in availability_by_simulator:
                availability_by_simulator[avail.simulator] = []
            availability_by_simulator[avail.simulator].append(avail)
        
        # Generate slots from availability windows
        for simulator in available_simulators:
            if simulator not in availability_by_simulator:
                continue
            
            for sim_avail in availability_by_simulator[simulator]:
                # Generate slots within this availability window
                avail_start = datetime.combine(booking_date, sim_avail.start_time)
                avail_end = datetime.combine(booking_date, sim_avail.end_time)
                
                # Handle case where end_time is before start_time (crosses midnight)
                if avail_end <= avail_start:
                    if sim_avail.end_time < sim_avail.start_time:
                        avail_end = avail_end + timedelta(days=1)
                    else:
                        continue
                
                # Convert to timezone-aware datetime for availability_end_time
                availability_end_datetime = timezone.make_aware(avail_end)
                
                # Generate slots at 30-minute intervals (regardless of requested duration)
                # This allows frontend to validate if selected duration fits
                current_time = avail_start
                now = timezone.now()
                
                # If booking is for today, adjust start time to skip past slots
                if booking_date == now.date():
                    # Round current time up to next 30-minute interval
                    current_minute = now.minute
                    current_second = now.second
                    current_microsecond = now.microsecond
                    
                    # Calculate minutes to add to round up to next 30-minute slot
                    minutes_to_add = 30 - (current_minute % 30)
                    if minutes_to_add == 30 and current_second == 0 and current_microsecond == 0:
                        minutes_to_add = 0  # Already on a 30-minute boundary
                    
                    # Calculate the next valid slot start time (timezone-aware)
                    next_slot_time = now + timedelta(minutes=minutes_to_add)
                    next_slot_time = next_slot_time.replace(second=0, microsecond=0)
                    
                    # Convert to naive datetime for comparison with current_time
                    next_slot_naive = next_slot_time.replace(tzinfo=None)
                    if next_slot_naive > current_time:
                        current_time = next_slot_naive
                        # If we've passed the availability window, skip to next availability
                        if current_time >= avail_end:
                            break
                
                while current_time < avail_end:
                    slot_start = timezone.make_aware(current_time)
                    # Skip slots that have already passed (for today's bookings)
                    if booking_date == now.date() and slot_start <= now:
                        current_time += timedelta(minutes=slot_interval)
                        continue
                    
                    slot_end = slot_start + timedelta(minutes=duration_minutes)
                    slot_fits_duration = slot_end <= availability_end_datetime
                    
                    # Check for conflicting bookings (use requested duration for conflict check)
                    conflicting_bookings = Booking.objects.filter(
                        simulator=simulator,
                        start_time__lt=slot_end,
                        end_time__gt=slot_start,
                        status__in=['confirmed', 'completed']
                    )
                    
                    # Check for special event conflicts
                    has_special_event, event_title = self._check_special_event_conflict(slot_start)
                    
                    # Check if facility is closed
                    from admin_panel.models import ClosedDay
                    location_id = get_location_id_from_request(request)
                    is_closed, closed_message = ClosedDay.check_if_closed(slot_start, location_id=location_id)
                    
                    if not conflicting_bookings.exists() and not has_special_event and not is_closed:
                        slot_start_str = slot_start.isoformat()
                        existing_slot = next((s for s in available_slots if s['start_time'] == slot_start_str), None)
                        
                        if not existing_slot:
                            slot_payload = {
                                'slot_id': f"{slot_start_str}:{duration_minutes}",
                                'start_time': slot_start_str,
                                'end_time': slot_end.isoformat(),
                                'duration_minutes': duration_minutes,
                                'availability_end_time': availability_end_datetime.isoformat(),
                                'fits_duration': slot_fits_duration,
                                'bay_count': 1,
                            }
                            if show_bay_details:
                                slot_payload['available_simulators'] = [{
                                    'id': simulator.id,
                                    'name': simulator.name,
                                    'bay_number': simulator.bay_number
                                }]
                                slot_payload['assigned_simulator'] = {
                                    'id': simulator.id,
                                    'name': simulator.name,
                                    'bay_number': simulator.bay_number
                                }
                            available_slots.append(slot_payload)
                        else:
                            # Keep the furthest availability end time and mark as fitting if any simulator fits
                            if availability_end_datetime.isoformat() > existing_slot.get('availability_end_time', ''):
                                existing_slot['availability_end_time'] = availability_end_datetime.isoformat()
                            if slot_fits_duration:
                                existing_slot['fits_duration'] = True
                            existing_slot['end_time'] = slot_end.isoformat()
                            existing_slot['bay_count'] = existing_slot.get('bay_count', 1) + 1
                            if show_bay_details:
                                if simulator.id not in [s['id'] for s in existing_slot.get('available_simulators', [])]:
                                    existing_slot.setdefault('available_simulators', []).append({
                                        'id': simulator.id,
                                        'name': simulator.name,
                                        'bay_number': simulator.bay_number
                                    })
                    
                    current_time += timedelta(minutes=slot_interval)
        
        # Filter slots to only include those with at least simulator_count available simulators
        filtered_slots = []
        for slot in available_slots:
            bay_count = slot.get('bay_count', 0)
            if bay_count >= simulator_count:
                slot['available_simulator_count'] = bay_count
                filtered_slots.append(slot)
        
        # Sort slots by start_time
        filtered_slots.sort(key=lambda x: x['start_time'])
        
        # Get hourly_price from first available simulator (for price calculation on frontend)
        # All simulators should have the same hourly_price, but we'll use the first one
        hourly_price = None
        if available_simulators.exists():
            first_simulator = available_simulators.first()
            hourly_price = float(first_simulator.hourly_price) if first_simulator.hourly_price else None
        
        response_data = {
            'date': date_str,
            'duration_minutes': duration_minutes,
            'simulator_count': simulator_count,
            'available_slots': filtered_slots,
            'hourly_price': hourly_price,  # Always include hourly_price (can be None)
            'max_available_simulators': max_available_simulators
        }
        
        return Response(response_data)
    
    @action(detail=False, methods=['get'])
    def check_coaching_availability(self, request):
        from users.models import User
        """Check available time slots for coaching booking"""
        date_str = request.query_params.get('date')
        package_id = request.query_params.get('package_id')
        coach_id = request.query_params.get('coach_id')
        
        if not date_str:
            return Response(
                {'error': 'Date is required'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        if not package_id:
            return Response(
                {'error': 'package_id is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            booking_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            return Response(
                {'error': 'Invalid date format'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        from coaching.models import CoachingPackage
        from users.models import StaffAvailability, StaffDayAvailability
        
        # Get location_id for filtering
        # Support guest users by phone parameter
        phone = request.query_params.get('phone')
        if phone and not request.user.is_authenticated:
            # Guest user - get location from user by phone
            try:
                from users.models import User
                guest_user = User.objects.get(phone=phone)
                location_id = guest_user.ghl_location_id
            except User.DoesNotExist:
                # Try to get location from request param
                location_id = request.query_params.get('location_id') or get_location_id_from_request(request)
        else:
            location_id = get_location_id_from_request(request)
        
        # Get coaching bay (bay 6) - filter by location if provided
        coaching_bay_qs = Simulator.objects.filter(is_coaching_bay=True, is_active=True)
        if location_id:
            coaching_bay_qs = coaching_bay_qs.filter(location_id=location_id)
        coaching_bay = coaching_bay_qs.first()
        if not coaching_bay:
            return Response({
                'available_slots': [],
                'message': 'Coaching bay not available'
            })
        
        # Build the coach queryset based on package/coach selections
        coaches_qs = User.objects.filter(role__in=['staff', 'admin'], is_active=True)
        if location_id:
            coaches_qs = coaches_qs.filter(ghl_location_id=location_id)
        try:
            selected_package = CoachingPackage.objects.get(id=package_id, is_active=True)
            coaches_qs = selected_package.staff_members.filter(role__in=['staff', 'admin'], is_active=True)
        except CoachingPackage.DoesNotExist:
            return Response(
                {'error': 'Package not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        if coach_id:
            coaches_qs = coaches_qs.filter(id=coach_id)
            # If a package is selected, ensure the coach belongs to it
            if selected_package and not selected_package.staff_members.filter(id=coach_id).exists():
                return Response(
                    {'error': 'Selected coach is not assigned to this package'},
                    status=status.HTTP_400_BAD_REQUEST
                )
        
        coaches = list(coaches_qs.distinct())
        
        if not coaches:
            return Response({
                'available_slots': [],
                'message': 'No coaches available'
            })
        
        # Session duration is dictated by package; allow optional override only if it matches
        requested_duration = request.query_params.get('duration')
        if requested_duration:
            duration_minutes = int(requested_duration)
            if duration_minutes != selected_package.session_duration_minutes:
                return Response(
                    {'error': f'Coaching sessions for this package must be {selected_package.session_duration_minutes} minutes.'},
                    status=status.HTTP_400_BAD_REQUEST
                )
        else:
            duration_minutes = selected_package.session_duration_minutes
        
        # Get day of week (0=Monday, 6=Sunday)
        day_of_week = booking_date.weekday()
        
        # Generate time slots based on staff availability for this day of week
        slot_interval = 30  # 30-minute intervals for slot generation
        available_slots_map = {}
        
        # Get weekly recurring staff availability entries for the requested day, scoped to selected coaches
        staff_availabilities = StaffAvailability.objects.filter(
            day_of_week=day_of_week,
            staff__in=coaches
        ).select_related('staff')
        
        # Get day-specific availability entries for the requested date, scoped to selected coaches
        day_specific_availabilities = StaffDayAvailability.objects.filter(
            date=booking_date,
            staff__in=coaches
        ).select_related('staff')
        
        availability_by_staff = {}
        
        # First, get staff IDs that have day-specific availability for this date
        # Day-specific availability takes precedence over weekly recurring
        staff_with_day_specific = set(day_specific_availabilities.values_list('staff_id', flat=True))
        
        # Process day-specific availability first (takes precedence)
        for day_avail in day_specific_availabilities:
            staff_id = day_avail.staff_id
            if staff_id not in availability_by_staff:
                availability_by_staff[staff_id] = []
            availability_by_staff[staff_id].append({
                'type': 'day_specific',
                'start_time': day_avail.start_time,
                'end_time': day_avail.end_time,
                'staff': day_avail.staff
            })
        
        # Process weekly recurring availability only for staff without day-specific availability
        for availability in staff_availabilities:
            staff_id = availability.staff_id
            # Only add weekly availability if this staff doesn't have day-specific availability
            if staff_id not in staff_with_day_specific:
                availability_by_staff.setdefault(staff_id, []).append({
                    'type': 'weekly',
                    'start_time': availability.start_time,
                    'end_time': availability.end_time,
                    'staff': availability.staff
                })
        
        for coach in coaches:
            coach_availabilities = availability_by_staff.get(coach.id, [])
            if not coach_availabilities:
                continue
            
            coach_name = f"{coach.first_name} {coach.last_name}".strip() or coach.username
            
            # Process all availability entries (both weekly and day-specific)
            for avail_entry in coach_availabilities:
                avail_start = datetime.combine(booking_date, avail_entry['start_time'])
                avail_end = datetime.combine(booking_date, avail_entry['end_time'])
                
                if avail_end <= avail_start:
                    if avail_entry['end_time'] < avail_entry['start_time']:
                        avail_end = avail_end + timedelta(days=1)
                    else:
                        continue
                
                current_time = avail_start
                now = timezone.now()
                
                # If booking is for today, adjust start time to skip past slots
                if booking_date == now.date():
                    # Round current time up to next 30-minute interval
                    current_minute = now.minute
                    current_second = now.second
                    current_microsecond = now.microsecond
                    
                    # Calculate minutes to add to round up to next 30-minute slot
                    minutes_to_add = 30 - (current_minute % 30)
                    if minutes_to_add == 30 and current_second == 0 and current_microsecond == 0:
                        minutes_to_add = 0  # Already on a 30-minute boundary
                    
                    # Calculate the next valid slot start time
                    next_slot_time = now + timedelta(minutes=minutes_to_add)
                    next_slot_time = next_slot_time.replace(second=0, microsecond=0)
                    
                    # If the next slot time is after availability start, use it
                    next_slot_naive = next_slot_time.replace(tzinfo=None)
                    if next_slot_naive > current_time:
                        current_time = next_slot_naive
                        # If we've passed the availability window, skip to next availability
                        if current_time >= avail_end:
                            continue
                
                while current_time + timedelta(minutes=slot_interval) <= avail_end:
                    slot_start = timezone.make_aware(current_time)
                    # Skip slots that have already passed (for today's bookings)
                    if booking_date == now.date() and slot_start < now:
                        current_time += timedelta(minutes=slot_interval)
                        continue
                    
                    slot_end = slot_start + timedelta(minutes=duration_minutes)
                    availability_end_datetime = timezone.make_aware(avail_end)
                    slot_fits_duration = slot_end <= availability_end_datetime
                    
                    # Check conflicts for coach
                    conflicting_bookings = Booking.objects.filter(
                        coach=coach,
                        start_time__lt=slot_end,
                        end_time__gt=slot_start,
                        status__in=['confirmed', 'completed']
                    )
                    
                    # Check for special event conflicts
                    has_special_event, event_title = self._check_special_event_conflict(slot_start)
                    
                    # Check if facility is closed
                    from admin_panel.models import ClosedDay
                    location_id = get_location_id_from_request(request)
                    is_closed, closed_message = ClosedDay.check_if_closed(slot_start, location_id=location_id)
                    
                    if conflicting_bookings.exists() or has_special_event or is_closed:
                        current_time += timedelta(minutes=slot_interval)
                        continue
                    
                    # Check conflicts for coaching bay
                    bay_conflicts = Booking.objects.filter(
                        simulator=coaching_bay,
                        start_time__lt=slot_end,
                        end_time__gt=slot_start,
                        status__in=['confirmed', 'completed']
                    )
                    
                    assigned_bay_number = coaching_bay.bay_number
                    if bay_conflicts.exists():
                        # Try any other available non-coaching bay
                        other_bay = Simulator.objects.filter(
                            is_active=True,
                            is_coaching_bay=False
                        ).exclude(
                            id__in=Booking.objects.filter(
                                start_time__lt=slot_end,
                                end_time__gt=slot_start,
                                status__in=['confirmed', 'completed']
                            ).values_list('simulator_id', flat=True)
                        ).first()
                        
                        if not other_bay:
                            current_time += timedelta(minutes=slot_interval)
                            continue
                        
                        assigned_bay_number = other_bay.bay_number
                    
                    slot_key = slot_start.isoformat()
                    slot_entry = available_slots_map.get(slot_key)
                    
                    if not slot_entry:
                        slot_entry = {
                            'start_time': slot_key,
                            'end_time': slot_end.isoformat(),
                            'duration_minutes': duration_minutes,
                            'availability_end_time': availability_end_datetime.isoformat(),
                            'fits_duration': slot_fits_duration,
                            'available_coaches': []
                        }
                        available_slots_map[slot_key] = slot_entry
                    else:
                        # Keep the furthest availability end time and mark as fitting if any coach fits
                        if availability_end_datetime.isoformat() > slot_entry['availability_end_time']:
                            slot_entry['availability_end_time'] = availability_end_datetime.isoformat()
                        if slot_fits_duration:
                            slot_entry['fits_duration'] = True
                        slot_entry['end_time'] = slot_end.isoformat()
                    
                    if coach.id not in [c['id'] for c in slot_entry['available_coaches']]:
                        slot_entry['available_coaches'].append({
                            'id': coach.id,
                            'name': coach_name,
                            'email': coach.email,
                            'assigned_bay': assigned_bay_number
                        })
                    
                    current_time += timedelta(minutes=slot_interval)
        
        available_slots = sorted(available_slots_map.values(), key=lambda x: x['start_time'])
        
        response_data = {
            'date': date_str,
            'package_id': package_id,
            'coach_id': coach_id,
            'available_slots': available_slots
        }
        
        return Response(response_data)


class CreateTempBookingView(APIView):
    """
    Create a temporary booking record before redirecting to payment for simulator bookings.
    Returns temp_id which is used in the redirect URL and webhook.
    """
    permission_classes = [AllowAny]  # Allow unauthenticated for flexibility
    
    @transaction.atomic
    def post(self, request):
        simulator_id = request.data.get('simulator_id')
        buyer_phone = request.data.get('buyer_phone')
        start_time = request.data.get('start_time')
        end_time = request.data.get('end_time')
        duration_minutes = request.data.get('duration_minutes')
        total_price = request.data.get('total_price')
        
        # Validate required fields
        if not simulator_id:
            return Response(
                {'error': 'simulator_id is required.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        if not buyer_phone:
            return Response(
                {'error': 'buyer_phone is required.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        if not start_time or not end_time:
            return Response(
                {'error': 'start_time and end_time are required.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        if not duration_minutes:
            return Response(
                {'error': 'duration_minutes is required.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        if not total_price:
            return Response(
                {'error': 'total_price is required.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Validate simulator exists and is active
        try:
            simulator = Simulator.objects.get(id=simulator_id, is_active=True)
        except Simulator.DoesNotExist:
            return Response(
                {'error': f'Simulator with ID {simulator_id} not found or is inactive.'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Get redirect URL - must be set
        redirect_url = simulator.redirect_url
        if not redirect_url:
            return Response(
                {'error': 'Simulator does not have a redirect URL configured.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Parse datetime strings
        try:
            start_time_dt = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
            end_time_dt = datetime.fromisoformat(end_time.replace('Z', '+00:00'))
            if timezone.is_naive(start_time_dt):
                start_time_dt = timezone.make_aware(start_time_dt)
            if timezone.is_naive(end_time_dt):
                end_time_dt = timezone.make_aware(end_time_dt)
        except (ValueError, AttributeError) as e:
            return Response(
                {'error': f'Invalid datetime format: {str(e)}'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Get location_id for temp booking
        location_id = get_location_id_from_request(request)
        
        # Create temp booking
        try:
            temp_booking = TempBooking.objects.create(
                simulator=simulator,
                location_id=location_id,
                buyer_phone=buyer_phone,
                start_time=start_time_dt,
                end_time=end_time_dt,
                duration_minutes=int(duration_minutes),
                total_price=Decimal(str(total_price))
            )
            
            logger.info(f"Temp booking created: temp_id={temp_booking.temp_id}, buyer={buyer_phone}, simulator={simulator_id}")
            
            return Response({
                'temp_id': str(temp_booking.temp_id),
                'redirect_url': redirect_url,
                'message': 'Temporary booking created successfully.'
            }, status=status.HTTP_201_CREATED)
            
        except Exception as e:
            logger.error(f"Error creating temp booking: {e}")
            return Response(
                {'error': f'Failed to create temporary booking: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class BookingWebhookView(APIView):
    """
    Webhook endpoint to create a simulator booking after external payment verification.
    Receives recipient_phone (which contains temp_id), phone, and booking details.
    Retrieves TempBooking, and creates actual booking.
    """
    permission_classes = [AllowAny]  # Webhook should be accessible without authentication
    
    @transaction.atomic
    def post(self, request):
        import uuid
        
        # New parameter name: recipient_phone contains the temp_id
        temp_id_str = request.data.get('recipient_phone')
        
        # Fallback to temp_id for backward compatibility
        if not temp_id_str:
            temp_id_str = request.data.get('temp_id')
        
        if not temp_id_str:
            return Response(
                {'error': 'recipient_phone (temp_id) is required.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Parse and validate temp_id
        try:
            temp_id = uuid.UUID(temp_id_str)
        except (ValueError, TypeError):
            return Response(
                {'error': 'Invalid recipient_phone (temp_id) format. Must be a valid UUID.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Get count parameter (duration in hours) - optional for backward compatibility
        count = request.data.get('count')
        if count:
            try:
                count = int(count)
                logger.info(f"Booking webhook called with count: {count} hours")
            except (ValueError, TypeError):
                logger.warning(f"Invalid count parameter: {count}, ignoring")
                count = None
        
        # Log webhook attempt
        logger.info(f"Booking webhook called for temp_id: {temp_id_str}, phone: {request.data.get('phone')}, count: {count}")
        
        # Get temp booking
        try:
            temp_booking = TempBooking.objects.get(temp_id=temp_id)
            logger.info(f"Temp booking found: temp_id={temp_id}, buyer={temp_booking.buyer_phone}, created_at={temp_booking.created_at}, expired={temp_booking.is_expired}")
        except TempBooking.DoesNotExist:
            # Log additional debugging info
            recent_temp_bookings = TempBooking.objects.order_by('-created_at')[:5]
            logger.error(
                f"Temp booking not found: temp_id={temp_id_str}. "
                f"Recent temp bookings (last 5): {[(str(tb.temp_id), tb.buyer_phone, tb.created_at) for tb in recent_temp_bookings]}"
            )
            
            # Check if there are any temp bookings at all
            total_count = TempBooking.objects.count()
            logger.error(f"Total temp bookings in database: {total_count}")
            
            # Check if phone matches any recent temp bookings
            phone_from_request = request.data.get('phone')
            if phone_from_request:
                temp_by_phone = TempBooking.objects.filter(buyer_phone=phone_from_request).order_by('-created_at').first()
                if temp_by_phone:
                    logger.warning(f"Found temp booking for phone {phone_from_request}: temp_id={temp_by_phone.temp_id}, created_at={temp_by_phone.created_at}")
            
            return Response(
                {
                    'error': f'Temporary booking with recipient_phone (temp_id) {temp_id_str} not found.',
                    'debug_info': {
                        'temp_id_received': temp_id_str,
                        'phone_received': phone_from_request,
                        'total_temp_bookings': total_count,
                        'recent_temp_bookings': [
                            {
                                'temp_id': str(tb.temp_id),
                                'buyer_phone': tb.buyer_phone,
                                'created_at': tb.created_at.isoformat() if tb.created_at else None,
                                'expired': tb.is_expired
                            }
                            for tb in recent_temp_bookings
                        ]
                    }
                },
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Check if temp booking is expired
        if temp_booking.is_expired:
            return Response(
                {'error': 'Temporary booking has expired.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Get buyer user
        try:
            buyer = User.objects.get(phone=temp_booking.buyer_phone)
        except User.DoesNotExist:
            return Response(
                {'error': f'Buyer with phone number {temp_booking.buyer_phone} not found.'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Get simulator_count from temp_booking (default to 1 for backward compatibility)
        simulator_count = getattr(temp_booking, 'simulator_count', 1)
        
        # Validate count if provided (should match duration_minutes * simulator_count / 60)
        if count is not None:
            expected_count = (temp_booking.duration_minutes * simulator_count) / 60
            if abs(count - expected_count) > 0.01:  # Allow small floating point differences
                logger.warning(
                    f"Count mismatch: received count={count}, expected count={expected_count} "
                    f"(duration_minutes={temp_booking.duration_minutes}, simulator_count={simulator_count}). Using values from temp_booking."
                )
        
        # Create bookings for each simulator
        try:
            created_bookings = []
            
            # Get location_id from temp booking, with fallback to user's location_id or simulator's location_id
            location_id = temp_booking.location_id
            if not location_id:
                # Fallback: try to get from buyer's ghl_location_id
                location_id = getattr(buyer, 'ghl_location_id', None)
                if not location_id:
                    # Final fallback: get from simulator
                    location_id = getattr(temp_booking.simulator, 'location_id', None)
            
            # Find available simulators for this time slot
            # Use the same logic as in BookingViewSet
            active_simulators = Simulator.objects.filter(
                is_active=True,
                is_coaching_bay=False
            )
            if location_id:
                active_simulators = active_simulators.filter(location_id=location_id)
            active_simulators = active_simulators.order_by('bay_number')
            
            available_simulators = []
            for simulator in active_simulators:
                if len(available_simulators) >= simulator_count:
                    break
                    
                conflict_exists = Booking.objects.filter(
                    simulator=simulator,
                    start_time__lt=temp_booking.end_time,
                    end_time__gt=temp_booking.start_time,
                    status__in=['confirmed', 'completed'],
                    booking_type='simulator'
                ).exists()
                
                if not conflict_exists:
                    available_simulators.append(simulator)
            
            if len(available_simulators) < simulator_count:
                logger.error(
                    f"Only {len(available_simulators)} simulator(s) available for webhook booking. "
                    f"Requested: {simulator_count}. Temp booking: {temp_booking.temp_id}"
                )
                return Response(
                    {'error': f'Only {len(available_simulators)} simulator(s) available for this time slot.'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Create a booking for each simulator
            single_simulator_price = temp_booking.total_price / simulator_count
            for simulator in available_simulators:
                booking = Booking.objects.create(
                    client=buyer,
                    location_id=location_id,
                    booking_type='simulator',
                    simulator=simulator,
                    start_time=temp_booking.start_time,
                    end_time=temp_booking.end_time,
                    duration_minutes=temp_booking.duration_minutes,
                    total_price=single_simulator_price,
                    status='confirmed'
                )
                created_bookings.append(booking)
            
            logger.info(
                f"Simulator booking(s) created via webhook: User {buyer.phone}, "
                f"Booking IDs: {[b.id for b in created_bookings]}, "
                f"Simulator count: {simulator_count}, "
                f"Duration per simulator: {temp_booking.duration_minutes} minutes "
                f"({temp_booking.duration_minutes / 60} hours), Count received: {count}"
            )
            
            # Return all created bookings
            booking_serializer = BookingSerializer(created_bookings, many=True)
            return Response({
                'message': f'Simulator booking(s) created successfully ({simulator_count} booking(s)).',
                'booking_ids': [b.id for b in created_bookings],
                'bookings': booking_serializer.data
            }, status=status.HTTP_201_CREATED)
            
        except Exception as e:
            logger.error(f"Error creating booking via webhook: {e}")
            return Response(
                {'error': f'Failed to create booking: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class GuestBookingCreateView(APIView):
    """
    Create a coaching booking for a guest user (by phone number).
    Guest users can book using their TPI assessment packages without authentication.
    """
    permission_classes = [AllowAny]
    authentication_classes = []  # Explicitly disable authentication for guest bookings
    
    def get_authenticators(self):
        """Override to disable authentication for guest bookings"""
        return []
    
    @transaction.atomic
    def post(self, request):
        phone = request.data.get('phone')
        if not phone:
            return Response(
                {'error': 'Phone number is required.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        phone = phone.strip()
        
        # Get user by phone
        try:
            from users.models import User
            user = User.objects.get(phone=phone)
        except User.DoesNotExist:
            return Response(
                {'error': 'User not found. Please ensure you have completed registration.'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Get booking data
        booking_type = request.data.get('booking_type')
        if booking_type != 'coaching':
            return Response(
                {'error': 'Only coaching bookings are supported for guest users.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        package_id = request.data.get('coaching_package')
        if not package_id:
            return Response(
                {'error': 'A coaching package is required.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Get package and verify it's a TPI assessment package
        from coaching.models import CoachingPackage, CoachingPackagePurchase
        try:
            package = CoachingPackage.objects.get(id=package_id, is_active=True)
        except CoachingPackage.DoesNotExist:
            return Response(
                {'error': 'Package not found.'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        if not package.is_tpi_assessment:
            return Response(
                {'error': 'Only TPI assessment packages can be used for guest bookings.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Verify user has a purchase for this package with sessions remaining
        purchase = CoachingPackagePurchase.objects.filter(
            client=user,
            package=package,
            sessions_remaining__gt=0,
            package_status='active'
        ).first()
        
        if not purchase:
            return Response(
                {'error': 'No active purchase found for this package with remaining sessions.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Get other booking details
        start_time_str = request.data.get('start_time')
        end_time_str = request.data.get('end_time')
        coach_id = request.data.get('coach')
        location_id = request.data.get('location_id') or user.ghl_location_id
        
        if not start_time_str or not end_time_str:
            return Response(
                {'error': 'start_time and end_time are required.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            from datetime import datetime
            start_time = datetime.fromisoformat(start_time_str.replace('Z', '+00:00'))
            end_time = datetime.fromisoformat(end_time_str.replace('Z', '+00:00'))
        except (ValueError, AttributeError):
            return Response(
                {'error': 'Invalid date format for start_time or end_time.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Validate coach if provided
        coach = None
        if coach_id:
            try:
                coach = User.objects.get(id=coach_id, role__in=['staff', 'admin'], is_active=True)
                if not package.staff_members.filter(id=coach_id).exists():
                    return Response(
                        {'error': 'Selected coach is not assigned to this package.'},
                        status=status.HTTP_400_BAD_REQUEST
                    )
            except User.DoesNotExist:
                return Response(
                    {'error': 'Coach not found.'},
                    status=status.HTTP_404_NOT_FOUND
                )
        
        # Check for booking conflicts
        from .models import Booking
        conflicting_bookings = Booking.objects.filter(
            Q(start_time__lt=end_time, end_time__gt=start_time),
            status__in=['confirmed', 'pending']
        )
        
        if coach:
            conflicting_bookings = conflicting_bookings.filter(coach=coach)
        else:
            # Check if any coach assigned to package has conflict
            package_coaches = package.staff_members.filter(role__in=['staff', 'admin'], is_active=True)
            if location_id:
                package_coaches = package_coaches.filter(ghl_location_id=location_id)
            conflicting_bookings = conflicting_bookings.filter(coach__in=package_coaches)
        
        # Check coaching bay availability
        from simulators.models import Simulator
        coaching_bay = Simulator.objects.filter(is_coaching_bay=True, is_active=True).first()
        if location_id:
            coaching_bay = Simulator.objects.filter(
                is_coaching_bay=True, is_active=True, location_id=location_id
            ).first()
        
        if not coaching_bay:
            return Response(
                {'error': 'Coaching bay not available.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Check if coaching bay is booked
        bay_conflicts = Booking.objects.filter(
            simulator=coaching_bay,
            start_time__lt=end_time,
            end_time__gt=start_time,
            status__in=['confirmed', 'pending']
        )
        if bay_conflicts.exists():
            return Response(
                {'error': 'This time slot is already booked.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        if conflicting_bookings.exists():
            return Response(
                {'error': 'This time slot is already booked for the selected coach.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Calculate price
        from decimal import Decimal, ROUND_HALF_UP
        if package.session_count:
            per_session = (Decimal(str(package.price)) / Decimal(str(package.session_count))).quantize(
                Decimal('0.01'),
                rounding=ROUND_HALF_UP
            )
        else:
            per_session = Decimal(str(package.price)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        
        # Consume session from purchase
        if purchase.sessions_remaining <= 0:
            return Response(
                {'error': 'No sessions remaining in this package.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        purchase.sessions_remaining -= 1
        purchase.save(update_fields=['sessions_remaining', 'updated_at'])
        
        # Create booking
        booking = Booking.objects.create(
            client=user,
            location_id=location_id,
            booking_type='coaching',
            coaching_package=package,
            coach=coach,
            simulator=coaching_bay,
            start_time=start_time,
            end_time=end_time,
            duration_minutes=package.session_duration_minutes,
            total_price=per_session,
            package_purchase=purchase,
            is_tpi_assessment=True,
            status='confirmed'
        )
        
        logger.info(f"Guest coaching booking created: id={booking.id}, location_id={location_id}, client={user.phone}")
        
        # Update GHL custom fields
        try:
            from ghl.services import update_user_ghl_custom_fields
            update_user_ghl_custom_fields(user, location_id=location_id)
        except Exception as exc:
            logger.warning("Failed to update GHL custom fields after guest booking creation: %s", exc)
        
        from .serializers import BookingSerializer
        serializer = BookingSerializer(booking)
        
        return Response({
            'message': 'Booking created successfully.',
            'booking': serializer.data
        }, status=status.HTTP_201_CREATED)