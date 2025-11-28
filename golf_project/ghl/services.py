import json
from django.core.cache import cache
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


def get_or_create_contact_custom_field(location_id, field_name, field_type="TEXT"):
    """
    Get existing contact custom field ID or create it if it doesn't exist.
    """
    from .models import GHLLocation
    import requests
    
    try:
        location = GHLLocation.objects.get(location_id=location_id)
        access_token = location.access_token
    except GHLLocation.DoesNotExist:
        logger.error(f"Location {location_id} not found")
        return None

    base_url = f"https://services.leadconnectorhq.com/locations/{location_id}/customFields"
    headers = {
        'Authorization': f'Bearer {access_token}',
        'Version': '2021-07-28',
        'Accept': 'application/json',
        'Content-Type': 'application/json'
    }
    
    try:
        # First, get all existing contact custom fields
        get_url = f"{base_url}?model=contact"
        response = requests.get(get_url, headers=headers, timeout=30)
        
        if response.status_code == 200:
            existing_custom_fields = response.json().get('customFields', [])
            
            # Check if field already exists
            for field in existing_custom_fields:
                if field.get('name') == field_name:
                    field_id = field.get('id')
                    logger.info(f"Found existing contact field '{field_name}' with ID: {field_id}")
                    return field_id
        
        # Field doesn't exist, create it for contacts
        create_payload = {
            "name": field_name,
            "dataType": field_type,
            "placeholder": field_name.lower().replace(' ', '_'),
            "model": "contact"
        }
        
        create_response = requests.post(base_url, json=create_payload, headers=headers, timeout=30)
        if create_response.status_code == 200:
            field_data = create_response.json()
            field_id = field_data.get('customField', {}).get('id')
            logger.info(f"Created new contact field '{field_name}' with ID: {field_id}")
            return field_id
        else:
            # Field might already exist but with different casing/spacing
            error_text = create_response.text
            if "already exists" in error_text.lower():
                logger.warning(f"Field '{field_name}' might already exist, searching again...")
                # Search more broadly in existing fields
                for field in existing_custom_fields:
                    if field_name.lower() in field.get('name', '').lower():
                        field_id = field.get('id')
                        logger.info(f"Found similar field '{field.get('name')}' with ID: {field_id}")
                        return field_id
            logger.error(f"Failed to create contact field '{field_name}': {create_response.status_code} - {create_response.text}")
            return None
            
    except Exception as e:
        logger.error(f"Error managing contact custom field '{field_name}' for location {location_id}: {e}")
        return None


def get_contact_custom_field_mapping(location_id):
    """
    Get mapping of field names to field IDs for required contact custom fields.
    """
    cache_key = f"ghl_contact_field_mapping_{location_id}"
    cached_mapping = cache.get(cache_key)
    
    if cached_mapping:
        return cached_mapping
    
    # Update these field names to match EXACTLY what's in your GHL dashboard
    required_fields = [
        {"name": "Login Otp", "type": "TEXT", "key": "otp_code"},  # Changed to match your GHL
        {"name": "Last Login At", "type": "TEXT", "key": "last_login_at"},
        {"name": "Purchase Amount", "type": "TEXT", "key": "purchase_amount"}
    ]
    
    field_mapping = {}
    
    for field_info in required_fields:
        field_id = get_or_create_contact_custom_field(
            location_id, 
            field_info["name"], 
            field_info["type"]
        )
        if field_id:
            field_mapping[field_info["key"]] = field_id
        else:
            logger.warning(f"Failed to get/create field '{field_info['name']}' for location {location_id}")
    
    # Cache for 1 hour
    if field_mapping:
        cache.set(cache_key, field_mapping, 3600)
        logger.info(f"Cached field mapping for location {location_id}: {field_mapping}")
    
    return field_mapping


def list_contact_custom_fields(location_id):
    """
    List all contact custom fields for a location (for debugging).
    """
    from .models import GHLLocation
    import requests
    
    try:
        location = GHLLocation.objects.get(location_id=location_id)
        access_token = location.access_token
    except GHLLocation.DoesNotExist:
        return None

    url = f"https://services.leadconnectorhq.com/locations/{location_id}/customFields?model=contact"
    headers = {
        'Authorization': f'Bearer {access_token}',
        'Version': '2021-07-28',
        'Accept': 'application/json'
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=30)
        if response.status_code == 200:
            data = response.json()
            custom_fields = data.get('customFields', [])
            print(f"=== CONTACT CUSTOM FIELDS FOR LOCATION {location_id} ===")
            for field in custom_fields:
                print(f"Name: {field.get('name')}")
                print(f"ID: {field.get('id')}")
                print(f"DataType: {field.get('dataType')}")
                print(f"Placeholder: {field.get('placeholder')}")
                print("-" * 40)
            return custom_fields
        else:
            print(f"Error: {response.status_code} - {response.text}")
            return None
    except Exception as e:
        print(f"Error: {e}")
        return None

def set_contact_custom_values(contact_id, location_id, custom_fields_dict):
    """
    Set custom field values for a specific contact using Contact update endpoint.
    """
    from .models import GHLLocation
    import requests
    
    try:
        location = GHLLocation.objects.get(location_id=location_id)
        access_token = location.access_token
    except GHLLocation.DoesNotExist:
        logger.error(f"Location {location_id} not found")
        return False

    url = f"https://services.leadconnectorhq.com/contacts/{contact_id}"
    headers = {
        'Authorization': f'Bearer {access_token}',
        'Version': '2021-07-28',
        'Accept': 'application/json',
        'Content-Type': 'application/json'
    }
    
    try:
        # First, get the current contact to preserve existing data
        get_response = requests.get(url, headers=headers, timeout=30)
        if get_response.status_code != 200:
            logger.error(f"Failed to get contact {contact_id}: {get_response.text}")
            return False
        
        contact_data = get_response.json().get('contact', {})
        
        # Prepare customFields payload
        custom_fields_payload = []
        
        # Get existing custom fields to preserve them
        existing_custom_fields = contact_data.get('customFields', [])
        
        # Get field name to ID mapping
        field_mapping = get_contact_custom_field_mapping(location_id)
        
        # Build the custom fields array for update
        for field_name, field_value in custom_fields_dict.items():
            # Find the field ID for this field name
            field_id = None
            for key, f_id in field_mapping.items():
                # Map back from our internal key to field name
                field_name_mapping = {
                    'otp_code': 'Login Otp',
                    'last_login_at': 'Last Login At', 
                    'purchase_amount': 'Purchase Amount'
                }
                if field_name_mapping.get(key) == field_name:
                    field_id = f_id
                    break
            
            if field_id:
                custom_fields_payload.append({
                    "id": field_id,
                    "value": str(field_value)
                })
                logger.info(f"🔄 Setting custom field: {field_name} = {field_value} (Field ID: {field_id})")
            else:
                logger.error(f"❌ Field ID not found for field name: {field_name}")
                # Try to get field mapping again
                field_mapping = get_contact_custom_field_mapping(location_id)
                logger.info(f"🔍 Current field mapping: {field_mapping}")
        
        # Update the contact with custom fields
        update_payload = {
            "customFields": custom_fields_payload
        }
        
        logger.info(f"📤 Sending update payload: {update_payload}")
        
        update_response = requests.put(url, json=update_payload, headers=headers, timeout=30)
        if update_response.status_code == 200:
            logger.info(f"✅ Successfully updated custom fields for contact {contact_id}")
            
            # Debug: Verify the update worked
            debug_contact_custom_fields(contact_id, location_id)
            
            return True
        else:
            logger.error(f"❌ Failed to update contact custom fields: {update_response.status_code} - {update_response.text}")
            return False
        
    except Exception as e:
        logger.error(f"❌ Error setting custom values for contact {contact_id}: {e}")
        return False

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
        # REMOVED: Don't add Location header since it goes in the body
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
        Create or update a contact in GHL using the correct approach.
        """
        if not phone:
            raise ValueError('phone number is required to upsert contact')
        
        if not location_id:
            raise ValueError('location_id is required for GHL contact creation')

        # Build basic contact payload
        payload = {
            "phone": phone,
            "source": "golf-portal",
            "locationId": location_id,
        }
        
        # Add basic fields
        if email:
            payload["email"] = email
        if first_name:
            payload["firstName"] = first_name
        if last_name:
            payload["lastName"] = last_name
        if first_name and last_name:
            payload["name"] = f"{first_name} {last_name}".strip()
        
        # REMOVED: if tags: payload["tags"] = tags

        endpoint = f"{self.base_url}/contacts/"
        
        try:
            headers = self._headers()
            if 'Location' in headers:
                del headers['Location']
                
            # First, try to create/update the contact
            response = requests.post(endpoint, json=payload, headers=headers, timeout=30)
            
            if response.status_code == 200:
                contact_data = response.json()
                contact_id = contact_data.get('contact', {}).get('id') or contact_data.get('id')
                logger.info(f"Successfully synced contact {phone} with GHL (contact_id: {contact_id})")
                
                # Ensure custom fields exist before setting values
                if custom_fields and contact_id:
                    # Get/create custom field mappings (this ensures fields exist)
                    get_contact_custom_field_mapping(location_id)
                    # Now set custom values separately using Contact update endpoint
                    set_contact_custom_values(contact_id, location_id, custom_fields)
                
                return contact_data
                
            elif response.status_code == 400 and "duplicated contacts" in response.text:
                # Contact already exists - extract contact ID and update
                error_data = response.json()
                contact_id = error_data.get('meta', {}).get('contactId')
                
                if contact_id:
                    logger.warning(f"Contact {phone} already exists with ID: {contact_id}, updating...")
                    
                    # Update basic contact info
                    update_payload = {k: v for k, v in payload.items() if k != 'locationId'}
                    update_endpoint = f"{endpoint}{contact_id}"
                    
                    update_response = requests.put(update_endpoint, json=update_payload, headers=headers, timeout=30)
                    if update_response.status_code == 200:
                        logger.info(f"✅ Successfully updated contact {phone} (contact_id: {contact_id})")
                        
                        # Ensure custom fields exist before setting values
                        if custom_fields and contact_id:
                            # Get/create custom field mappings (this ensures fields exist)
                            get_contact_custom_field_mapping(location_id)
                            # Set custom values for existing contact
                            set_contact_custom_values(contact_id, location_id, custom_fields)
                        
                        return update_response.json()
                    else:
                        logger.error(f"Failed to update contact {phone}: {update_response.text}")
                        return None
                else:
                    logger.error(f"Could not extract contact ID from duplicate error")
                    return None
            else:
                response.raise_for_status()
                return None
                
        except requests.exceptions.HTTPError as exc:
            logger.error(f"Failed to sync contact {phone}: {exc}")
            return None
        except Exception as exc:
            logger.error(f"Failed to sync contact {phone}: {exc}")
            return None


def purchase_custom_fields(purchase_name, amount):
    """
    Build custom fields dict for purchase sync.
    Uses the same field names as defined in get_contact_custom_field_mapping.
    """
    return {
        "Purchase Amount": str(amount),  # This will be created/updated as custom field
    }


def sync_user_contact(user, *, location_id: Optional[str] = None,
                      tags: Optional[List[str]] = None, custom_fields: Optional[dict] = None):
    """
    Production-ready contact sync with custom fields.
    Follows the same pattern as login: create/update contact, then set custom field values.
    """
    if not user or not getattr(user, 'phone', None):
        logger.warning("Cannot sync user to GHL: user or phone missing")
        return None, None

    resolved_location = location_id or getattr(user, 'ghl_location_id', None) or getattr(settings, 'GHL_DEFAULT_LOCATION', None)
    if not resolved_location:
        logger.warning("No GHL location available for user %s", user.id)
        return None, None

    try:
        client = GHLClient(location_id=resolved_location)
        
        # Map custom_fields keys to field names if needed
        mapped_custom_fields = {}
        if custom_fields:
            # Field name mapping: key -> field name
            field_name_mapping = {
                'otp_code': 'Login Otp',
                'last_login_at': 'Last Login At',
                'purchase_amount': 'Purchase Amount',
            }
            
            for key, value in custom_fields.items():
                # If key is already a field name (contains space or title case), use it directly
                if ' ' in key or key[0].isupper():
                    mapped_custom_fields[key] = value
                # Otherwise, map the key to field name
                elif key in field_name_mapping:
                    mapped_custom_fields[field_name_mapping[key]] = value
                else:
                    # Use key as-is (might be a field name already)
                    mapped_custom_fields[key] = value
        
        # REMOVED: All tag creation logic
        
        response = client.upsert_contact(
            phone=user.phone,
            email=getattr(user, 'email', None),
            first_name=getattr(user, 'first_name', None),
            last_name=getattr(user, 'last_name', None),
            location_id=resolved_location,
            tags=None,  # REMOVED: tags parameter
            custom_fields=mapped_custom_fields,
        )
        
        contact_id = None
        if response and isinstance(response, dict):
            contact_id = response.get('contact', {}).get('id') or response.get('id')

        if contact_id and user.ghl_contact_id != contact_id:
            user.ghl_contact_id = contact_id
            user.save(update_fields=['ghl_contact_id'])
            logger.info("Saved GHL contact_id %s for user %s", contact_id, user.id)

        logger.info("Successfully synced user %s to GHL location %s", user.id, resolved_location)
        return response, contact_id
        
    except Exception as exc:
        logger.error("GHL sync failed for user %s (location: %s): %s", 
                   user.id, resolved_location, exc, exc_info=True)
        return None, None


def debug_contact_custom_fields(contact_id, location_id):
    """
    Debug function to check current custom field values for a contact.
    """
    from .models import GHLLocation
    import requests
    
    try:
        location = GHLLocation.objects.get(location_id=location_id)
        access_token = location.access_token
    except GHLLocation.DoesNotExist:
        print("❌ Location not found")
        return None

    url = f"https://services.leadconnectorhq.com/contacts/{contact_id}"
    headers = {
        'Authorization': f'Bearer {access_token}',
        'Version': '2021-07-28',
        'Accept': 'application/json'
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=30)
        if response.status_code == 200:
            contact_data = response.json().get('contact', {})
            custom_fields = contact_data.get('customFields', [])
            
            print(f"\n🔍 === CUSTOM FIELDS FOR CONTACT {contact_id} ===")
            print(f"📱 Phone: {contact_data.get('phone')}")
            print(f"👤 Name: {contact_data.get('name')}")
            print(f"🏢 Location: {location_id}")
            
            # Get field mapping to map IDs to names
            field_mapping = get_contact_custom_field_mapping(location_id)
            # Reverse the mapping to get ID -> name
            id_to_name = {v: k for k, v in field_mapping.items()}
            # Map our internal keys to display names
            display_names = {
                'otp_code': 'Login OTP',
                'last_login_at': 'Last Login At',
                'purchase_amount': 'Purchase Amount'
            }
            
            if custom_fields:
                for field in custom_fields:
                    field_id = field.get('id')
                    field_name = field.get('name')
                    field_value = field.get('value')
                    
                    # If field name is None, try to get it from our mapping
                    if not field_name and field_id:
                        internal_key = id_to_name.get(field_id)
                        if internal_key:
                            field_name = display_names.get(internal_key, internal_key)
                    
                    print(f"---")
                    print(f"Field ID: {field_id}")
                    print(f"Field Name: {field_name or '❌ Unknown'}")
                    print(f"Field Value: {field_value}")
                    
                    # Show which field this corresponds to
                    if field_id == field_mapping.get('otp_code'):
                        print(f"🔐 This is the Login OTP field")
                    elif field_id == field_mapping.get('purchase_amount'):
                        print(f"💰 This is the Purchase Amount field")
                    elif field_id == field_mapping.get('last_login_at'):
                        print(f"⏰ This is the Last Login At field")
            else:
                print("❌ No custom fields found")
                
            print("=" * 50)
            return custom_fields
        else:
            print(f"❌ Error getting contact: {response.status_code} - {response.text}")
            return None
    except Exception as e:
        print(f"❌ Error: {e}")
        return None