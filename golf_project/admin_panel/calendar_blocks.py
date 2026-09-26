"""
Admin calendar resource blackouts: staff (existing), simulator bays, category assets.
Unified create/list/delete endpoints for the admin calendar Block Time UI.
"""
import logging
from datetime import datetime as dt

from django.db import IntegrityError, transaction
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from users.utils import get_location_id_from_request

logger = logging.getLogger(__name__)


def _staff_only(user):
    return (
        user
        and user.is_authenticated
        and (user.is_superuser or getattr(user, 'role', None) in ['admin', 'staff', 'superadmin'])
    )


def _normalize_location_id(value):
    if not value:
        return None
    normalized = str(value).strip().rstrip('+').strip()
    return normalized or None


def _location_mismatch(resource_location_id, request_location_id):
    """True only when both sides are set and differ after normalize."""
    req = _normalize_location_id(request_location_id)
    res = _normalize_location_id(resource_location_id)
    return bool(req and res and req != res)


def _get_blockable_coach(resource_id):
    """Staff, admin, or superadmin who can be blocked as a coach."""
    from users.models import User
    return User.objects.get(
        id=resource_id,
        role__in=['staff', 'admin', 'superadmin'],
    )


def _parse_block_times(data):
    date_str = data.get('date')
    start_time_str = data.get('start_time')
    end_time_str = data.get('end_time')
    reason = data.get('reason') or ''

    if not date_str:
        return None, Response({'error': 'date is required (YYYY-MM-DD).'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        block_date = dt.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        return None, Response({'error': 'Invalid date format. Use YYYY-MM-DD.'}, status=status.HTTP_400_BAD_REQUEST)

    start_time = end_time = None
    if start_time_str and end_time_str:
        # Browsers may send HH:MM or HH:MM:SS from <input type="time">
        def _parse_time(value):
            value = str(value).strip()
            for fmt in ('%H:%M', '%H:%M:%S'):
                try:
                    return dt.strptime(value, fmt).time()
                except ValueError:
                    continue
            raise ValueError('bad time')

        try:
            start_time = _parse_time(start_time_str)
            end_time = _parse_time(end_time_str)
        except ValueError:
            return None, Response({'error': 'Invalid time format. Use HH:MM.'}, status=status.HTTP_400_BAD_REQUEST)
        if start_time >= end_time:
            return None, Response({'error': 'end_time must be after start_time.'}, status=status.HTTP_400_BAD_REQUEST)
    elif start_time_str or end_time_str:
        return None, Response(
            {'error': 'Provide both start_time and end_time, or neither for a full-day block.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    return {
        'date': block_date,
        'start_time': start_time,
        'end_time': end_time,
        'reason': reason,
    }, None


def _block_payload(block, resource_type, resource_id):
    return {
        'id': block.id,
        'resource_type': resource_type,
        'resource_id': resource_id,
        'date': block.date.isoformat(),
        'start_time': block.start_time.strftime('%H:%M') if block.start_time else None,
        'end_time': block.end_time.strftime('%H:%M') if block.end_time else None,
        'is_full_day': block.is_full_day_block(),
        'reason': block.reason or '',
    }


def _public_cancel_stats(cancel_stats):
    """Strip internal email context before returning to the client."""
    if not cancel_stats:
        return {}
    return {
        'cancelled_bookings': cancel_stats.get('cancelled_bookings', 0),
        'cancelled_booking_ids': cancel_stats.get('cancelled_booking_ids', []),
        'refunded_sessions': cancel_stats.get('refunded_sessions', 0),
        'refunded_simulator_hours': cancel_stats.get('refunded_simulator_hours', 0),
        'refunded_category_hours': cancel_stats.get('refunded_category_hours', 0),
        'emails_sent': cancel_stats.get('emails_sent', 0),
    }


def _parse_resource_ids(data):
    """
    Accept resource_ids (list) and/or resource_id (single).
    Returns (ids: list[int], error_response|None).
    """
    raw_ids = data.get('resource_ids')
    ids = []
    if raw_ids is not None:
        if not isinstance(raw_ids, (list, tuple)):
            return None, Response(
                {'error': 'resource_ids must be a list of IDs.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        for item in raw_ids:
            try:
                ids.append(int(item))
            except (TypeError, ValueError):
                return None, Response(
                    {'error': f'Invalid resource id: {item}'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
    single = data.get('resource_id')
    if single not in (None, '', 0, '0'):
        try:
            sid = int(single)
        except (TypeError, ValueError):
            return None, Response(
                {'error': 'Invalid resource_id.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if sid not in ids:
            ids.append(sid)
    # Preserve order, unique
    seen = set()
    unique = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            unique.append(i)
    if not unique:
        return None, Response(
            {'error': 'resource_id or resource_ids is required.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    return unique, None


def _merge_cancel_stats(acc, nxt):
    if not nxt:
        return acc
    if not acc:
        return nxt
    acc_bookings = (acc.get('_email_ctx') or {}).get('bookings') or []
    nxt_bookings = (nxt.get('_email_ctx') or {}).get('bookings') or []
    seen = set()
    merged_bookings = []
    for b in list(acc_bookings) + list(nxt_bookings):
        bid = getattr(b, 'id', None)
        if bid is None or bid in seen:
            continue
        seen.add(bid)
        merged_bookings.append(b)

    ids = list(dict.fromkeys(
        (acc.get('cancelled_booking_ids') or []) + (nxt.get('cancelled_booking_ids') or [])
    ))
    ctx = dict(acc.get('_email_ctx') or {})
    ctx.update(nxt.get('_email_ctx') or {})
    labels = []
    for part in (acc.get('_email_ctx') or {}).get('resource_label', ''), (nxt.get('_email_ctx') or {}).get('resource_label', ''):
        if part and part not in labels:
            labels.append(part)
    # Prefer concatenated labels from resource_label fields on stats if present
    prev_label = (acc.get('_email_ctx') or {}).get('resource_label') or ''
    next_label = (nxt.get('_email_ctx') or {}).get('resource_label') or ''
    if prev_label and next_label and prev_label != next_label:
        ctx['resource_label'] = f'{prev_label}, {next_label}'
    elif next_label:
        ctx['resource_label'] = next_label
    elif prev_label:
        ctx['resource_label'] = prev_label
    ctx['bookings'] = merged_bookings

    return {
        'cancelled_bookings': len(ids),
        'cancelled_booking_ids': ids,
        'refunded_sessions': (acc.get('refunded_sessions') or 0) + (nxt.get('refunded_sessions') or 0),
        'refunded_simulator_hours': float(acc.get('refunded_simulator_hours') or 0) + float(
            nxt.get('refunded_simulator_hours') or 0
        ),
        'refunded_category_hours': float(acc.get('refunded_category_hours') or 0) + float(
            nxt.get('refunded_category_hours') or 0
        ),
        'emails_sent': (acc.get('emails_sent') or 0) + (nxt.get('emails_sent') or 0),
        '_email_ctx': ctx,
    }


def _merge_preview(acc, nxt):
    if not nxt:
        return acc
    if not acc:
        return nxt
    by_id = {b['id']: b for b in (acc.get('bookings') or [])}
    for b in nxt.get('bookings') or []:
        by_id[b['id']] = b
    labels = [x for x in [acc.get('resource_label'), nxt.get('resource_label')] if x]
    # de-dupe label parts
    label_parts = []
    for lab in labels:
        for part in str(lab).split(', '):
            if part and part not in label_parts:
                label_parts.append(part)
    bookings = list(by_id.values())
    return {
        **nxt,
        'resource_label': ', '.join(label_parts) if label_parts else nxt.get('resource_label'),
        'count': len(bookings),
        'bookings': bookings,
    }


class CalendarBlockView(APIView):
    """
    POST /api/admin/calendar-blocks/
      resource_type: staff | simulator | asset
      resource_id: int
      date, start_time?, end_time?, reason?, category_id? (staff only)

    GET /api/admin/calendar-blocks/?resource_type=&resource_id=&date=YYYY-MM-DD
    DELETE /api/admin/calendar-blocks/
      body: resource_type, resource_id, block_id  OR  date (+ optional times)
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not _staff_only(request.user):
            return Response({'error': 'Staff or admin only.'}, status=status.HTTP_403_FORBIDDEN)

        resource_type = request.query_params.get('resource_type')
        resource_id = request.query_params.get('resource_id')
        date_str = request.query_params.get('date')
        location_id = get_location_id_from_request(request)

        if resource_type == 'staff' and resource_id:
            from users.models import StaffBlockedDate
            qs = StaffBlockedDate.objects.filter(staff_id=resource_id)
            if date_str:
                qs = qs.filter(date=date_str)
            return Response([
                {
                    'id': b.id,
                    'resource_type': 'staff',
                    'resource_id': b.staff_id,
                    'date': b.date.isoformat(),
                    'start_time': b.start_time.strftime('%H:%M') if b.start_time else None,
                    'end_time': b.end_time.strftime('%H:%M') if b.end_time else None,
                    'is_full_day': b.is_full_day_block(),
                    'reason': b.reason or '',
                }
                for b in qs.order_by('date', 'start_time')
            ])

        if resource_type == 'simulator' and resource_id:
            from simulators.models import SimulatorBlockedDate
            qs = SimulatorBlockedDate.objects.filter(simulator_id=resource_id)
            if date_str:
                qs = qs.filter(date=date_str)
            return Response([
                {
                    'id': b.id,
                    'resource_type': 'simulator',
                    'resource_id': b.simulator_id,
                    'date': b.date.isoformat(),
                    'start_time': b.start_time.strftime('%H:%M') if b.start_time else None,
                    'end_time': b.end_time.strftime('%H:%M') if b.end_time else None,
                    'is_full_day': b.is_full_day_block(),
                    'reason': b.reason or '',
                }
                for b in qs.order_by('date', 'start_time')
            ])

        if resource_type == 'asset' and resource_id:
            from categories.models import CategoryAssetBlockedDate
            qs = CategoryAssetBlockedDate.objects.filter(asset_id=resource_id)
            if date_str:
                qs = qs.filter(date=date_str)
            return Response([
                {
                    'id': b.id,
                    'resource_type': 'asset',
                    'resource_id': b.asset_id,
                    'date': b.date.isoformat(),
                    'start_time': b.start_time.strftime('%H:%M') if b.start_time else None,
                    'end_time': b.end_time.strftime('%H:%M') if b.end_time else None,
                    'is_full_day': b.is_full_day_block(),
                    'reason': b.reason or '',
                }
                for b in qs.order_by('date', 'start_time')
            ])

        # List all blocks for a date (calendar day view)
        if date_str:
            from users.models import StaffBlockedDate, User
            from simulators.models import SimulatorBlockedDate
            from categories.models import CategoryAssetBlockedDate

            staff_qs = StaffBlockedDate.objects.filter(date=date_str).select_related('staff')
            if location_id:
                staff_ids = User.objects.filter(
                    role__in=['staff', 'admin', 'superadmin'],
                    ghl_location_id=location_id,
                ).values_list('id', flat=True)
                staff_qs = staff_qs.filter(staff_id__in=staff_ids)

            sim_qs = SimulatorBlockedDate.objects.filter(date=date_str).select_related('simulator')
            if location_id:
                sim_qs = sim_qs.filter(simulator__location_id=location_id)

            asset_qs = CategoryAssetBlockedDate.objects.filter(date=date_str).select_related('asset')
            if location_id:
                asset_qs = asset_qs.filter(asset__location_id=location_id)

            results = []
            for b in staff_qs:
                results.append({
                    'id': b.id,
                    'resource_type': 'staff',
                    'resource_id': b.staff_id,
                    'resource_name': b.staff.get_full_name() or b.staff.username,
                    'date': b.date.isoformat(),
                    'start_time': b.start_time.strftime('%H:%M') if b.start_time else None,
                    'end_time': b.end_time.strftime('%H:%M') if b.end_time else None,
                    'is_full_day': b.is_full_day_block(),
                    'reason': b.reason or '',
                })
            for b in sim_qs:
                results.append({
                    'id': b.id,
                    'resource_type': 'simulator',
                    'resource_id': b.simulator_id,
                    'resource_name': f'Bay {b.simulator.bay_number} — {b.simulator.name}',
                    'date': b.date.isoformat(),
                    'start_time': b.start_time.strftime('%H:%M') if b.start_time else None,
                    'end_time': b.end_time.strftime('%H:%M') if b.end_time else None,
                    'is_full_day': b.is_full_day_block(),
                    'reason': b.reason or '',
                })
            for b in asset_qs:
                results.append({
                    'id': b.id,
                    'resource_type': 'asset',
                    'resource_id': b.asset_id,
                    'resource_name': b.asset.name,
                    'date': b.date.isoformat(),
                    'start_time': b.start_time.strftime('%H:%M') if b.start_time else None,
                    'end_time': b.end_time.strftime('%H:%M') if b.end_time else None,
                    'is_full_day': b.is_full_day_block(),
                    'reason': b.reason or '',
                })
            return Response(results)

        return Response(
            {'error': 'Provide resource_type+resource_id and/or date.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    def post(self, request):
        if not _staff_only(request.user):
            return Response({'error': 'Staff or admin only.'}, status=status.HTTP_403_FORBIDDEN)

        resource_type = (request.data.get('resource_type') or '').strip().lower()
        if resource_type not in ('staff', 'simulator', 'asset'):
            return Response(
                {'error': 'resource_type (staff|simulator|asset) is required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        resource_ids, ids_err = _parse_resource_ids(request.data)
        if ids_err:
            return ids_err

        parsed, err = _parse_block_times(request.data)
        if err:
            return err

        location_id = get_location_id_from_request(request)
        preview_only = str(request.data.get('preview', '')).lower() in ('1', 'true', 'yes')

        from admin_panel.block_cancellations import (
            cancel_for_staff_block,
            cancel_for_simulator_block,
            cancel_for_asset_block,
            finalize_block_cancel_emails,
            preview_for_staff_block,
            preview_for_simulator_block,
            preview_for_asset_block,
        )

        # ── Preview only: list overlapping bookings, do not create/cancel ──
        if preview_only:
            try:
                merged = None
                for resource_id in resource_ids:
                    if resource_type == 'staff':
                        from users.models import User
                        try:
                            staff = _get_blockable_coach(resource_id)
                        except User.DoesNotExist:
                            return Response({'error': f'Coach not found (id={resource_id}).'}, status=status.HTTP_404_NOT_FOUND)
                        if _location_mismatch(staff.ghl_location_id, location_id):
                            return Response({'error': 'Coach not in your location.'}, status=status.HTTP_403_FORBIDDEN)
                        data = preview_for_staff_block(
                            staff, parsed['date'], parsed['start_time'], parsed['end_time'],
                            location_id or staff.ghl_location_id,
                        )
                    elif resource_type == 'simulator':
                        from simulators.models import Simulator
                        try:
                            sim = Simulator.objects.get(id=resource_id)
                        except Simulator.DoesNotExist:
                            return Response({'error': f'Simulator not found (id={resource_id}).'}, status=status.HTTP_404_NOT_FOUND)
                        if _location_mismatch(sim.location_id, location_id):
                            return Response({'error': 'Simulator not in your location.'}, status=status.HTTP_403_FORBIDDEN)
                        data = preview_for_simulator_block(
                            sim, parsed['date'], parsed['start_time'], parsed['end_time'],
                            location_id or sim.location_id,
                        )
                    else:
                        from categories.models import CategoryAsset
                        try:
                            asset = CategoryAsset.objects.get(id=resource_id)
                        except CategoryAsset.DoesNotExist:
                            return Response({'error': f'Asset not found (id={resource_id}).'}, status=status.HTTP_404_NOT_FOUND)
                        if _location_mismatch(asset.location_id, location_id):
                            return Response({'error': 'Asset not in your location.'}, status=status.HTTP_403_FORBIDDEN)
                        data = preview_for_asset_block(
                            asset, parsed['date'], parsed['start_time'], parsed['end_time'],
                            location_id or asset.location_id,
                        )
                    merged = _merge_preview(merged, data)
            except Exception:
                logger.exception('Block preview failed')
                return Response(
                    {'error': 'Failed to preview affected bookings.'},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )

            return Response({
                'preview': True,
                'date': parsed['date'].isoformat(),
                'start_time': parsed['start_time'].strftime('%H:%M') if parsed['start_time'] else None,
                'end_time': parsed['end_time'].strftime('%H:%M') if parsed['end_time'] else None,
                'is_full_day': not (parsed['start_time'] and parsed['end_time']),
                'resource_ids': resource_ids,
                **(merged or {'count': 0, 'bookings': [], 'resource_label': ''}),
            })

        payloads = []
        cancel_stats = None

        # Validate all resources before creating any blocks
        resolved = []
        for resource_id in resource_ids:
            if resource_type == 'staff':
                from users.models import User
                try:
                    staff = _get_blockable_coach(resource_id)
                except User.DoesNotExist:
                    return Response({'error': f'Coach not found (id={resource_id}).'}, status=status.HTTP_404_NOT_FOUND)
                if _location_mismatch(staff.ghl_location_id, location_id):
                    return Response({'error': 'Coach not in your location.'}, status=status.HTTP_403_FORBIDDEN)
                resolved.append(('staff', staff))
            elif resource_type == 'simulator':
                from simulators.models import Simulator
                try:
                    sim = Simulator.objects.get(id=resource_id)
                except Simulator.DoesNotExist:
                    return Response({'error': f'Simulator not found (id={resource_id}).'}, status=status.HTTP_404_NOT_FOUND)
                if _location_mismatch(sim.location_id, location_id):
                    return Response({'error': 'Simulator not in your location.'}, status=status.HTTP_403_FORBIDDEN)
                resolved.append(('simulator', sim))
            else:
                from categories.models import CategoryAsset
                try:
                    asset = CategoryAsset.objects.get(id=resource_id)
                except CategoryAsset.DoesNotExist:
                    return Response({'error': f'Asset not found (id={resource_id}).'}, status=status.HTTP_404_NOT_FOUND)
                if _location_mismatch(asset.location_id, location_id):
                    return Response({'error': 'Asset not in your location.'}, status=status.HTTP_403_FORBIDDEN)
                resolved.append(('asset', asset))

        raw_cat = request.data.get('category_id')
        category_id = None
        if raw_cat not in (None, '', 0, '0'):
            try:
                category_id = int(raw_cat)
            except (TypeError, ValueError):
                return Response({'error': 'Invalid category_id.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            with transaction.atomic():
                for kind, obj in resolved:
                    if kind == 'staff':
                        from users.models import StaffBlockedDate
                        block = StaffBlockedDate.objects.create(
                            staff=obj,
                            date=parsed['date'],
                            start_time=parsed['start_time'],
                            end_time=parsed['end_time'],
                            reason=parsed['reason'],
                            created_by=request.user,
                            service_category_id=category_id,
                        )
                        stats = cancel_for_staff_block(
                            staff=obj,
                            block_date=parsed['date'],
                            start_time=parsed['start_time'],
                            end_time=parsed['end_time'],
                            location_id=location_id or obj.ghl_location_id,
                            issued_by=request.user,
                            reason=parsed['reason'],
                            send_email=False,
                        )
                        cancel_stats = _merge_cancel_stats(cancel_stats, stats)
                        payloads.append(_block_payload(block, 'staff', obj.id))

                    elif kind == 'simulator':
                        from simulators.models import SimulatorBlockedDate
                        block = SimulatorBlockedDate.objects.create(
                            simulator=obj,
                            date=parsed['date'],
                            start_time=parsed['start_time'],
                            end_time=parsed['end_time'],
                            reason=parsed['reason'],
                            created_by=request.user,
                        )
                        stats = cancel_for_simulator_block(
                            simulator=obj,
                            block_date=parsed['date'],
                            start_time=parsed['start_time'],
                            end_time=parsed['end_time'],
                            location_id=location_id or obj.location_id,
                            issued_by=request.user,
                            reason=parsed['reason'],
                            send_email=False,
                        )
                        cancel_stats = _merge_cancel_stats(cancel_stats, stats)
                        payloads.append(_block_payload(block, 'simulator', obj.id))

                    else:
                        from categories.models import CategoryAssetBlockedDate
                        block = CategoryAssetBlockedDate.objects.create(
                            asset=obj,
                            date=parsed['date'],
                            start_time=parsed['start_time'],
                            end_time=parsed['end_time'],
                            reason=parsed['reason'],
                            created_by=request.user,
                        )
                        stats = cancel_for_asset_block(
                            asset=obj,
                            block_date=parsed['date'],
                            start_time=parsed['start_time'],
                            end_time=parsed['end_time'],
                            location_id=location_id or obj.location_id,
                            issued_by=request.user,
                            reason=parsed['reason'],
                            send_email=False,
                        )
                        cancel_stats = _merge_cancel_stats(cancel_stats, stats)
                        payloads.append(_block_payload(block, 'asset', obj.id))
        except IntegrityError:
            return Response(
                {'error': 'This time is already blocked for one of the selected resources.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        payload = {
            'blocks': payloads,
            'resource_ids': resource_ids,
            'count': len(payloads),
        }
        # Back-compat single-block shape when only one resource
        if len(payloads) == 1:
            payload.update(payloads[0])

        if cancel_stats is not None:
            finalize_block_cancel_emails(cancel_stats)
            cancel_stats.pop('_email_ctx', None)
            stats = _public_cancel_stats(cancel_stats)
            payload.update(stats)
            payload['message'] = (
                f"Time blocked for {len(payloads)} resource(s). "
                f"Cancelled {stats['cancelled_bookings']} booking(s); "
                f"emailed {stats['emails_sent']} client(s)."
            )
        return Response(payload, status=status.HTTP_201_CREATED)

    def delete(self, request):
        if not _staff_only(request.user):
            return Response({'error': 'Staff or admin only.'}, status=status.HTTP_403_FORBIDDEN)

        resource_type = (request.data.get('resource_type') or '').strip().lower()
        block_id = request.data.get('block_id') or request.query_params.get('block_id')
        if not resource_type or not block_id:
            return Response(
                {'error': 'resource_type and block_id are required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if resource_type == 'staff':
            from users.models import StaffBlockedDate
            deleted, _ = StaffBlockedDate.objects.filter(id=block_id).delete()
        elif resource_type == 'simulator':
            from simulators.models import SimulatorBlockedDate
            deleted, _ = SimulatorBlockedDate.objects.filter(id=block_id).delete()
        elif resource_type == 'asset':
            from categories.models import CategoryAssetBlockedDate
            deleted, _ = CategoryAssetBlockedDate.objects.filter(id=block_id).delete()
        else:
            return Response({'error': 'Invalid resource_type.'}, status=status.HTTP_400_BAD_REQUEST)

        if not deleted:
            return Response({'error': 'Block not found.'}, status=status.HTTP_404_NOT_FOUND)
        return Response({'message': 'Block removed.'})
