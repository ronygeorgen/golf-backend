from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.db.models import Sum, Count, Q
from django.utils import timezone
from datetime import datetime, timedelta

# Import from other apps
from users.utils import get_location_id_from_request
from bookings.models import Booking
from users.models import User
from coaching.models import CoachingPackagePurchase, SimulatorPackagePurchase


class DashboardViewSet(viewsets.ViewSet):
    permission_classes = [IsAuthenticated]
    
    def _get_date_range(self, request):
        """Extract start_date and end_date from query params, default to last 30 days"""
        start_date = request.query_params.get('start_date')
        end_date = request.query_params.get('end_date')
        
        if not start_date or not end_date:
            # Default to last 30 days
            end_date = timezone.now().date()
            start_date = end_date - timedelta(days=30)
        else:
            try:
                start_date = datetime.strptime(start_date, '%Y-%m-%d').date()
                end_date = datetime.strptime(end_date, '%Y-%m-%d').date()
            except ValueError:
                # Invalid date format, use default
                end_date = timezone.now().date()
                start_date = end_date - timedelta(days=30)
        
        return start_date, end_date
    
    def _filter_by_location(self, queryset, location_id, location_field='location_id'):
        """Filter queryset by location_id"""
        if location_id:
            return queryset.filter(**{location_field: location_id})
        return queryset
    
    @action(detail=False, methods=['get'], url_path='busy-quiet-times')
    def busy_quiet_times(self, request):
        """
        Returns heatmap data for busy & quiet times
        Format: {day_of_week: {hour: activity_count}}
        """
        location_id = get_location_id_from_request(request)
        start_date, end_date = self._get_date_range(request)
        
        # Get bookings in date range
        bookings = Booking.objects.filter(
            start_time__date__gte=start_date,
            start_time__date__lte=end_date,
            status__in=['confirmed', 'completed']
        )
        bookings = self._filter_by_location(bookings, location_id)
        
        # Initialize data structure: {day_of_week: {hour: count}}
        # day_of_week: 0=Monday, 6=Sunday
        # hour: 0-23
        heatmap_data = {}
        for day in range(7):
            heatmap_data[day] = {}
            for hour in range(24):
                heatmap_data[day][hour] = 0
        
        # Aggregate bookings by day of week and hour
        for booking in bookings:
            booking_time = booking.start_time
            day_of_week = booking_time.weekday()  # 0=Monday, 6=Sunday
            hour = booking_time.hour
            
            heatmap_data[day_of_week][hour] += 1
        
        # Convert to list format for frontend
        # Format: [{day: 0, hour: 0, value: 5}, ...]
        result = []
        day_names = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
        
        for day in range(7):
            for hour in range(24):
                result.append({
                    'day': day,
                    'day_name': day_names[day],
                    'hour': hour,
                    'value': heatmap_data[day][hour]
                })
        
        return Response({
            'data': result,
            'start_date': start_date.isoformat(),
            'end_date': end_date.isoformat()
        })
    
    @action(detail=False, methods=['get'], url_path='top-customers')
    def top_customers(self, request):
        """
        Returns top customers by total spend
        """
        location_id = get_location_id_from_request(request)
        start_date, end_date = self._get_date_range(request)
        
        # Get bookings in date range
        bookings = Booking.objects.filter(
            start_time__date__gte=start_date,
            start_time__date__lte=end_date,
            status__in=['confirmed', 'completed']
        )
        bookings = self._filter_by_location(bookings, location_id)
        
        # Aggregate total spend per customer
        customer_spend = bookings.values('client').annotate(
            total_spend=Sum('total_price'),
            booking_count=Count('id')
        ).order_by('-total_spend')
        
        # Get customer details
        result = []
        for item in customer_spend:
            try:
                customer = User.objects.get(id=item['client'])
                result.append({
                    'customer_id': customer.id,
                    'customer_name': f"{customer.first_name} {customer.last_name}".strip() or customer.username,
                    'total_spend': float(item['total_spend']),
                    'booking_count': item['booking_count']
                })
            except User.DoesNotExist:
                continue
        
        return Response({
            'data': result,
            'start_date': start_date.isoformat(),
            'end_date': end_date.isoformat()
        })
    
    @action(detail=False, methods=['get'], url_path='staff-sales')
    def staff_sales(self, request):
        """
        Returns staff sales performance (revenue per staff member)
        """
        location_id = get_location_id_from_request(request)
        start_date, end_date = self._get_date_range(request)
        
        # Get bookings in date range where coach is assigned
        bookings = Booking.objects.filter(
            start_time__date__gte=start_date,
            start_time__date__lte=end_date,
            status__in=['confirmed', 'completed'],
            booking_type='coaching',
            coach__isnull=False
        )
        bookings = self._filter_by_location(bookings, location_id)
        
        # Aggregate revenue per staff member
        staff_revenue = bookings.values('coach').annotate(
            total_revenue=Sum('total_price'),
            booking_count=Count('id')
        ).order_by('-total_revenue')
        
        # Get staff details
        result = []
        for item in staff_revenue:
            try:
                staff = User.objects.get(id=item['coach'])
                result.append({
                    'staff_id': staff.id,
                    'staff_name': f"{staff.first_name} {staff.last_name}".strip() or staff.username,
                    'total_revenue': float(item['total_revenue']),
                    'booking_count': item['booking_count']
                })
            except User.DoesNotExist:
                continue
        
        return Response({
            'data': result,
            'start_date': start_date.isoformat(),
            'end_date': end_date.isoformat()
        })
    
    @action(detail=False, methods=['get'], url_path='tpi-conversion')
    def tpi_conversion(self, request):
        """
        Returns TPI conversion rate data
        """
        location_id = get_location_id_from_request(request)
        start_date, end_date = self._get_date_range(request)
        
        # Get all TPI assessment bookings
        tpi_bookings = Booking.objects.filter(
            is_tpi_assessment=True,
            booking_type='coaching',
            start_time__date__gte=start_date,
            start_time__date__lte=end_date
        )
        tpi_bookings = self._filter_by_location(tpi_bookings, location_id)
        
        # Get unique customers who had TPI assessments
        tpi_customers = tpi_bookings.values_list('client', flat=True).distinct()
        total_tpis = len(tpi_customers)
        
        # Check which TPI customers made additional purchases (converted)
        # A converted TPI is a customer who:
        # 1. Had a TPI assessment booking
        # 2. Made at least one additional booking or purchase after the TPI
        
        converted_tpis = 0
        converted_customers = []
        not_converted_customers = []
        
        for customer_id in tpi_customers:
            # Get customer's first TPI booking date
            first_tpi = tpi_bookings.filter(client_id=customer_id).order_by('start_time').first()
            if not first_tpi:
                continue
            
            tpi_date = first_tpi.start_time.date()
            
            # Check if customer made any bookings or purchases after TPI
            # Check for additional bookings (non-TPI)
            additional_bookings = Booking.objects.filter(
                client_id=customer_id,
                start_time__date__gt=tpi_date,
                status__in=['confirmed', 'completed']
            ).exclude(is_tpi_assessment=True)
            
            # Check for package purchases
            coaching_purchases = CoachingPackagePurchase.objects.filter(
                client_id=customer_id,
                purchased_at__date__gt=tpi_date
            )
            simulator_purchases = SimulatorPackagePurchase.objects.filter(
                client_id=customer_id,
                purchased_at__date__gt=tpi_date
            )
            
            try:
                customer = User.objects.get(id=customer_id)
                customer_data = {
                    'customer_id': customer.id,
                    'customer_name': f"{customer.first_name} {customer.last_name}".strip() or customer.username,
                    'customer_email': customer.email or '',
                    'customer_phone': customer.phone or '',
                    'tpi_date': tpi_date.isoformat()
                }
                
                if additional_bookings.exists() or coaching_purchases.exists() or simulator_purchases.exists():
                    converted_tpis += 1
                    converted_customers.append(customer_data)
                else:
                    not_converted_customers.append(customer_data)
            except User.DoesNotExist:
                pass
        
        not_converted = total_tpis - converted_tpis
        conversion_rate = (converted_tpis / total_tpis * 100) if total_tpis > 0 else 0
        
        return Response({
            'total_tpis': total_tpis,
            'converted_tpis': converted_tpis,
            'not_converted_tpis': not_converted,
            'conversion_rate': round(conversion_rate, 2),
            'converted_customers': converted_customers,
            'not_converted_customers': not_converted_customers,
            'start_date': start_date.isoformat(),
            'end_date': end_date.isoformat()
        })
    
    @action(detail=False, methods=['get'], url_path='kpi-stats')
    def kpi_stats(self, request):
        """
        Returns KPI statistics:
        - Total revenue of completed sessions
        - Total completed simulator bookings
        - Total completed coaching session bookings
        - Total confirmed bookings
        """
        from decimal import Decimal, ROUND_HALF_UP
        
        location_id = get_location_id_from_request(request)
        start_date, end_date = self._get_date_range(request)
        
        # 1. Total revenue of completed sessions (both coaching and simulator)
        # Include both confirmed and completed bookings (both represent paid revenue)
        completed_bookings = Booking.objects.filter(
            start_time__date__gte=start_date,
            start_time__date__lte=end_date,
            status__in=['confirmed', 'completed']
        )
        completed_bookings = self._filter_by_location(completed_bookings, location_id)
        
        # Calculate revenue: sum total_price, but for bookings with total_price=0 and a package,
        # calculate per-session price from the package
        total_completed_revenue = Decimal('0.00')
        for booking in completed_bookings:
            if booking.total_price and booking.total_price > 0:
                total_completed_revenue += Decimal(str(booking.total_price))
            elif booking.coaching_package:
                # Calculate per-session price from coaching package
                package = booking.coaching_package
                if package.session_count and package.session_count > 0:
                    per_session = (Decimal(str(package.price)) / Decimal(str(package.session_count))).quantize(
                        Decimal('0.01'),
                        rounding=ROUND_HALF_UP
                    )
                    total_completed_revenue += per_session
                else:
                    total_completed_revenue += Decimal(str(package.price))
            elif booking.booking_type == 'simulator' and booking.simulator:
                # For simulator bookings without total_price, calculate from simulator hourly price or duration price
                if booking.duration_minutes and booking.simulator.hourly_price:
                    hours = Decimal(str(booking.duration_minutes)) / Decimal('60')
                    price = (Decimal(str(booking.simulator.hourly_price)) * hours).quantize(
                        Decimal('0.01'),
                        rounding=ROUND_HALF_UP
                    )
                    total_completed_revenue += price
                elif booking.duration_minutes:
                    # Try to get from DurationPrice model
                    try:
                        from simulators.models import DurationPrice
                        duration_price = DurationPrice.objects.get(duration_minutes=booking.duration_minutes)
                        total_completed_revenue += Decimal(str(duration_price.price))
                    except:
                        pass  # Skip if no price found
        
        # 2. Total completed simulator bookings
        completed_simulator_bookings = Booking.objects.filter(
            booking_type='simulator',
            start_time__date__gte=start_date,
            start_time__date__lte=end_date,
            status__in=['confirmed', 'completed']
        )
        completed_simulator_bookings = self._filter_by_location(completed_simulator_bookings, location_id)
        total_simulator_bookings = completed_simulator_bookings.count()
        
        # 3. Total completed coaching session bookings
        completed_coaching_bookings = Booking.objects.filter(
            booking_type='coaching',
            start_time__date__gte=start_date,
            start_time__date__lte=end_date,
            status__in=['confirmed', 'completed']
        )
        completed_coaching_bookings = self._filter_by_location(completed_coaching_bookings, location_id)
        total_coaching_bookings = completed_coaching_bookings.count()
        
        # 4. Total confirmed bookings (only bookings with status='confirmed' in the date range)
        # Note: This counts only 'confirmed' status bookings, not 'completed' bookings
        confirmed_bookings = Booking.objects.filter(
            start_time__date__gte=start_date,
            start_time__date__lte=end_date,
            status='confirmed'
        )
        confirmed_bookings = self._filter_by_location(confirmed_bookings, location_id)
        total_confirmed_count = confirmed_bookings.count()
        
        return Response({
            'total_completed_revenue': float(total_completed_revenue),
            'total_simulator_bookings': total_simulator_bookings,
            'total_coaching_bookings': total_coaching_bookings,
            'total_confirmed_bookings': total_confirmed_count,
            'start_date': start_date.isoformat(),
            'end_date': end_date.isoformat()
        })
