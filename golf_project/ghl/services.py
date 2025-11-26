import json
import logging
from typing import List, Optional
from urllib import error as urllib_error, request as urllib_request, parse as urllib_parse
from datetime import timedelta

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

from django.conf import settings
from django.utils import timezone

from .models import GHLLocation

logger = logging.getLogger(__name__)


class GHLClient:
    """
    OAuth-based wrapper around GoHighLevel (LeadConnector) APIs.
    """

    def __init__(self, location_id: Optional[str] = None, base_url: Optional[str] = None):
        self.location_id = location_id
        self.base_url = (base_url or getattr(settings, 'GHL_BASE_URL', 'https://services.leadconnectorhq.com')).rstrip('/')
        self.api_version = getattr(settings, 'GHL_API_VERSION', '2021-07-28')
        self._location = None

    def _get_location(self):
        """Get or fetch the GHL location with valid token"""
        if self._location and self._location.is_token_valid():
            return self._location
        
        if not self.location_id:
            raise ValueError('location_id is required for OAuth authentication')
        
        try:
            location = GHLLocation.objects.get(location_id=self.location_id)
            self._location = location
            
            # Check token validity
            if not location.access_token:
                raise ValueError(f'GHL location {self.location_id} has no access token. Please re-onboard.')
            
            if not location.is_token_valid():
                logger.warning("Token for location %s is expired. Attempting refresh...", self.location_id)
                if location.needs_token_refresh() or not location.is_token_valid():
                    self._refresh_access_token(location)
            elif location.needs_token_refresh():
                # Token is valid but will expire soon, refresh proactively
                logger.info("Token for location %s expires soon. Refreshing proactively...", self.location_id)
                self._refresh_access_token(location)
            
            return location
        except GHLLocation.DoesNotExist:
            raise ValueError(f'GHL location {self.location_id} not found. Please complete OAuth flow first.')

    def _refresh_access_token(self, location: GHLLocation):
        """Refresh the OAuth access token using refresh token"""
        if not location.refresh_token:
            raise ValueError(f'No refresh token available for location {location.location_id}. Please re-authenticate.')
        
        client_id = getattr(settings, 'GHL_CLIENT_ID', '')
        client_secret = getattr(settings, 'GHL_CLIENT_SECRET', '')
        
        if not client_id or not client_secret:
            raise ValueError('GHL_CLIENT_ID and GHL_CLIENT_SECRET must be configured')
        
        token_url = f"{self.base_url}/oauth/token"
        payload = {
            'client_id': client_id,
            'client_secret': client_secret,
            'grant_type': 'refresh_token',
            'refresh_token': location.refresh_token,
        }
        
        try:
            if requests:
                response = requests.post(token_url, data=payload, timeout=30)
                response.raise_for_status()
                data = response.json()
            else:
                data_str = urllib_parse.urlencode(payload).encode('utf-8')
                req = urllib_request.Request(token_url, data=data_str, method='POST')
                req.add_header('Content-Type', 'application/x-www-form-urlencoded')
                with urllib_request.urlopen(req, timeout=30) as resp:  # nosec B310
                    body = resp.read().decode('utf-8')
                    data = json.loads(body or "{}")
            
            location.access_token = data.get('access_token', '')
            location.refresh_token = data.get('refresh_token', location.refresh_token)
            expires_in = data.get('expires_in', 3600)
            location.token_expires_at = timezone.now() + timedelta(seconds=expires_in)
            location.save(update_fields=['access_token', 'refresh_token', 'token_expires_at'])
            
            logger.info("Refreshed access token for location %s", location.location_id)
            self._location = location
        except (requests.RequestException, urllib_error.URLError, urllib_error.HTTPError) as exc:
            logger.error("Failed to refresh token for location %s: %s", location.location_id, exc, exc_info=True)
            raise

    def _headers(self, location_id: Optional[str] = None):
        """Get headers with OAuth token"""
        loc_id = location_id or self.location_id
        if loc_id:
            location = self._get_location()
            access_token = location.access_token
        else:
            # Fallback: try to get from default location
            default_location_id = getattr(settings, 'GHL_DEFAULT_LOCATION', None)
            if default_location_id:
                try:
                    location = GHLLocation.objects.get(location_id=default_location_id)
                    if location.needs_token_refresh():
                        self._refresh_access_token(location)
                    access_token = location.access_token
                except GHLLocation.DoesNotExist:
                    raise ValueError('No valid GHL location found')
            else:
                raise ValueError('location_id is required')
        
        headers = {
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'Authorization': f"Bearer {access_token}",
            'Version': self.api_version,
        }
        if loc_id:
            headers['Location'] = loc_id
        return headers

    def _get(self, endpoint: str, headers: dict):
        """Make GET request"""
        if requests:
            response = requests.get(endpoint, headers=headers, timeout=30)
            response.raise_for_status()
            return response.json()
        req = urllib_request.Request(endpoint, headers=headers, method='GET')
        with urllib_request.urlopen(req, timeout=30) as resp:  # nosec B310
            body = resp.read().decode('utf-8')
            return json.loads(body or "{}")

    def _post(self, endpoint: str, payload: Optional[dict], headers: dict):
        """Make POST request"""
        if requests:
            response = requests.post(endpoint, json=payload or {}, headers=headers, timeout=30)
            if not response.ok:
                error_detail = response.text
                logger.error("GHL API error: %d - %s. Response: %s", 
                           response.status_code, response.reason, error_detail)
                logger.error("Request endpoint: %s", endpoint)
                logger.error("Request headers: %s", {k: v for k, v in headers.items() if k != 'Authorization'})
                logger.error("Request payload: %s", payload)
            response.raise_for_status()
            return response.json()
        data = json.dumps(payload or {}).encode('utf-8')
        req = urllib_request.Request(endpoint, data=data, headers=headers, method='POST')
        with urllib_request.urlopen(req, timeout=30) as resp:  # nosec B310
            body = resp.read().decode('utf-8')
            return json.loads(body or "{}")

    def onboard_subaccount(self, location_id: str, payload: Optional[dict] = None):
        """
        Call GHL to onboard / refresh a subaccount.
        Note: This may require OAuth authentication depending on GHL API requirements.
        """
        if not location_id:
            raise ValueError('location_id is required')

        data = payload or {}
        endpoint = f"{self.base_url}/locations/{location_id}/actions/onboard"
        try:
            response_data = self._post(endpoint, data, self._headers(location_id))
            logger.info("GHL onboard success for %s", location_id)
            return response_data
        except (requests.RequestException, urllib_error.URLError, urllib_error.HTTPError) as exc:
            logger.error("Failed to onboard GHL location %s: %s", location_id, exc, exc_info=True)
            raise

    def upsert_contact(self, *, phone: str, location_id: Optional[str] = None,
                       email: Optional[str] = None, first_name: Optional[str] = None,
                       last_name: Optional[str] = None, tags: Optional[List[str]] = None,
                       custom_fields: Optional[dict] = None):
        """
        Create or update a contact in GHL using phone/email as identifiers.
        """
        if not phone:
            raise ValueError('phone number is required to upsert contact')

        payload = {
            "phone": phone,
            "source": "golf-portal",
        }
        if email:
            payload["email"] = email
        names = []
        if first_name:
            names.append(first_name)
        if last_name:
            names.append(last_name)
        if names:
            payload["name"] = " ".join(names).strip()
        if tags:
            payload["tags"] = tags
        if custom_fields:
            payload["customFields"] = custom_fields

        endpoint = f"{self.base_url}/contacts/"
        try:
            headers = self._headers(location_id)
            logger.debug("GHL API request - Endpoint: %s, Location header: %s", 
                        endpoint, headers.get('Location'))
            data = self._post(endpoint, payload, headers)
            logger.info("Synced contact %s with GHL", phone)
            return data
        except requests.exceptions.HTTPError as exc:
            # Log detailed error information
            if hasattr(exc, 'response') and exc.response is not None:
                error_detail = exc.response.text
                logger.error("GHL API HTTP Error %d for contact %s: %s", 
                           exc.response.status_code, phone, error_detail)
                logger.error("Response headers: %s", dict(exc.response.headers))
            logger.error("Failed to sync contact %s: %s", phone, exc, exc_info=True)
            raise
        except (requests.RequestException, urllib_error.URLError, urllib_error.HTTPError) as exc:
            logger.error("Failed to sync contact %s: %s", phone, exc, exc_info=True)
            raise

    def add_tags(self, contact_id: str, tags: List[str], location_id: Optional[str] = None):
        """Add tags to a contact"""
        if not contact_id or not tags:
            return

        endpoint = f"{self.base_url}/contacts/{contact_id}/tags"
        payload = {"tags": tags}
        try:
            data = self._post(endpoint, payload, self._headers(location_id))
            logger.info("Applied tags %s to contact %s", tags, contact_id)
            return data
        except (requests.RequestException, urllib_error.URLError, urllib_error.HTTPError) as exc:
            logger.error("Failed to add tags to contact %s: %s", contact_id, exc, exc_info=True)
            raise


def build_purchase_tags(amount):
    """
    Helper to produce a deterministic tag format for purchase amounts.
    """
    try:
        value = float(amount)
        normalized = int(value)
    except (TypeError, ValueError):
        normalized = 0
    tag_value = f"amount_{normalized}"
    return ["purchase", tag_value]


def purchase_custom_fields(purchase_name, amount):
    return {
        "last_purchase_name": purchase_name,
        "last_purchase_amount": str(amount),
        "last_purchase_at": timezone.now().isoformat(),
    }


def sync_user_contact(user, *, location_id: Optional[str] = None,
                      tags: Optional[List[str]] = None, custom_fields: Optional[dict] = None):
    """
    Convenience helper to push the current user's info into GHL and persist the contact id.
    """
    if not user or not getattr(user, 'phone', None):
        logger.warning("Cannot sync user to GHL: user or phone missing")
        return None, None

    resolved_location = location_id or getattr(user, 'ghl_location_id', None) or getattr(settings, 'GHL_DEFAULT_LOCATION', None)
    if not resolved_location:
        logger.warning("No GHL location available for user %s (phone: %s)", user, getattr(user, 'phone', None))
        return None, None

    # Check if location exists in database and has tokens
    try:
        location = GHLLocation.objects.get(location_id=resolved_location)
        if not location.access_token:
            logger.error("GHL location %s exists but has no access token. Please re-onboard.", resolved_location)
            return None, None
        
        # Decode JWT token to verify it's for the correct location
        try:
            import base64
            import json as json_lib
            # JWT tokens have 3 parts separated by dots: header.payload.signature
            token_parts = location.access_token.split('.')
            if len(token_parts) >= 2:
                # Decode the payload (second part)
                payload = token_parts[1]
                # Add padding if needed
                payload += '=' * (4 - len(payload) % 4)
                decoded = base64.urlsafe_b64decode(payload)
                token_data = json_lib.loads(decoded)
                token_location_id = token_data.get('authClassId') or token_data.get('primaryAuthClassId')
                logger.info("Token is for location: %s, trying to use: %s", token_location_id, resolved_location)
                if token_location_id != resolved_location:
                    logger.error("TOKEN MISMATCH! Token is for location %s but we're trying to use %s. Please re-onboard with the correct location.", 
                               token_location_id, resolved_location)
                    return None, None
        except Exception as decode_exc:
            logger.warning("Could not decode token to verify location: %s", decode_exc)
        
        logger.info("Syncing user %s (phone: %s) to GHL location %s", user.id, user.phone, resolved_location)
    except GHLLocation.DoesNotExist:
        logger.error("GHL location %s not found in database. Please onboard first using /api/ghlpage/onboard/", resolved_location)
        return None, None

    try:
        client = GHLClient(location_id=resolved_location)
        response = client.upsert_contact(
            phone=user.phone,
            email=getattr(user, 'email', None),
            first_name=getattr(user, 'first_name', None),
            last_name=getattr(user, 'last_name', None),
            location_id=resolved_location,
            tags=tags,
            custom_fields=custom_fields,
        )
        contact_id = None
        if isinstance(response, dict):
            contact_id = response.get('contact', {}).get('id') or response.get('id')

        if contact_id and user.ghl_contact_id != contact_id:
            user.ghl_contact_id = contact_id
            user.save(update_fields=['ghl_contact_id'])
            logger.info("Saved GHL contact_id %s for user %s", contact_id, user.id)

        logger.info("Successfully synced user %s to GHL location %s", user.id, resolved_location)
        return response, contact_id
    except Exception as exc:
        logger.error("Failed to sync user %s to GHL location %s: %s", user.id, resolved_location, exc, exc_info=True)
        raise
