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
        {"name": "Login Otp", "type": "TEXT", "key": "login_otp"},  # Changed to match your GHL
        {"name": "Last Login At", "type": "TEXT", "key": "last_login_at"},
        {"name": "Purchase Amount", "type": "TEXT", "key": "purchase_amount"},
        {"name": "Total Coaching Session", "type": "TEXT", "key": "total_coaching_session"},
        {"name": "Total Simulator Hour", "type": "TEXT", "key": "total_simulator_hour"},
        {"name": "Last Active Package", "type": "TEXT", "key": "last_active_package"},
        {"name": "upcoming simulator booking date", "type": "TEXT", "key": "upcoming_simulator_booking_date"},
        {"name": "upcoming coaching session booking date", "type": "TEXT", "key": "upcoming_coaching_session_booking_date"}
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
                    'login_otp': 'Login Otp',
                    'last_login_at': 'Last Login At', 
                    'purchase_amount': 'Purchase Amount',
                    'total_coaching_session': 'Total Coaching Session',
                    'total_simulator_hour': 'Total Simulator Hour',
                    'last_active_package': 'Last Active Package',
                    'upcoming_simulator_booking_date': 'upcoming simulator booking date',
                    'upcoming_coaching_session_booking_date': 'upcoming coaching session booking date'
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
                   last_name: Optional[str] = None, date_of_birth: Optional[str] = None,
                   tags: Optional[List[str]] = None, custom_fields: Optional[dict] = None):
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
        if date_of_birth:
            # Format: YYYY-MM-DD (GHL expects ISO date format)
            payload["dateOfBirth"] = date_of_birth
        
        # REMOVED: if tags: payload["tags"] = tags

        endpoint = f"{self.base_url}/contacts/"
        
        try:
            headers = self._headers()
            if 'Location' in headers:
                del headers['Location']
                
            # First, try to create/update the contact
            response = requests.post(endpoint, json=payload, headers=headers, timeout=30)
            
            # GHL returns 200 for updates and 201 for successful creation
            if response.status_code in (200, 201):
                contact_data = response.json()
                contact_id = contact_data.get('contact', {}).get('id') or contact_data.get('id')
                logger.info(f"Successfully synced contact {phone} with GHL (contact_id: {contact_id}, status: {response.status_code})")
                
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
                        # If update fails, try to get the contact by phone
                        return self._get_contact_by_phone_and_update(phone, location_id, payload, custom_fields)
                else:
                    logger.error(f"Could not extract contact ID from duplicate error")
                    # Try to get the contact by phone instead
                    return self._get_contact_by_phone_and_update(phone, location_id, payload, custom_fields)
            else:
                logger.error(f"Failed to create contact {phone}: {response.status_code} - {response.text}")
                # Try direct creation without locationId in payload for existing contacts
                return self._create_or_update_contact_direct(phone, location_id, payload, custom_fields, headers)
                
        except requests.exceptions.HTTPError as exc:
            logger.error(f"Failed to sync contact {phone}: {exc}")
            return None
        except Exception as exc:
            logger.error(f"Failed to sync contact {phone}: {exc}")
            return None

    # Add these helper methods to the GHLClient class
    def _get_existing_contact_id(self, phone, location_id):
        """Get existing contact ID by phone number"""
        try:
            # Search for contact by phone
            search_url = f"{self.base_url}/contacts/search"
            search_payload = {
                "query": {
                    "locationId": location_id,
                    "phone": phone
                }
            }
            
            headers = self._headers()
            if 'Location' in headers:
                del headers['Location']
            
            search_response = requests.post(search_url, json=search_payload, headers=headers, timeout=30)
            if search_response.status_code == 200:
                search_data = search_response.json()
                contacts = search_data.get('contacts', [])
                if contacts:
                    contact = contacts[0]
                    contact_id = contact.get('id')
                    logger.info(f"Found existing contact {phone} with ID: {contact_id}")
                    return contact_id
            
            return None
        except Exception as exc:
            logger.error(f"Failed to search for contact {phone}: {exc}")
            return None
    
    def _get_contact_by_phone_and_update(self, phone, location_id, payload, custom_fields):
        """Get contact by phone and update it"""
        try:
            # Search for contact by phone
            search_url = f"{self.base_url}/contacts/search"
            search_payload = {
                "query": {
                    "locationId": location_id,
                    "phone": phone
                }
            }
            
            headers = self._headers()
            if 'Location' in headers:
                del headers['Location']
            
            search_response = requests.post(search_url, json=search_payload, headers=headers, timeout=30)
            if search_response.status_code == 200:
                search_data = search_response.json()
                contacts = search_data.get('contacts', [])
                if contacts:
                    contact = contacts[0]
                    contact_id = contact.get('id')
                    
                    # Update the contact
                    update_payload = {k: v for k, v in payload.items() if k != 'locationId'}
                    update_endpoint = f"{self.base_url}/contacts/{contact_id}"
                    
                    update_response = requests.put(update_endpoint, json=update_payload, headers=headers, timeout=30)
                    if update_response.status_code == 200:
                        logger.info(f"✅ Successfully updated contact {phone} (contact_id: {contact_id})")
                        
                        # Set custom values
                        if custom_fields and contact_id:
                            get_contact_custom_field_mapping(location_id)
                            set_contact_custom_values(contact_id, location_id, custom_fields)
                        
                        return update_response.json()
            
            # If we get here, try direct creation
            return self._create_or_update_contact_direct(phone, location_id, payload, custom_fields, headers)
            
        except Exception as exc:
            logger.error(f"Failed to search/update contact {phone}: {exc}")
            return None

    def _create_or_update_contact_direct(self, phone, location_id, payload, custom_fields, headers):
        """Direct creation/update with fallback logic"""
        try:
            # Remove locationId from payload for update/creation
            create_payload = payload.copy()
            if 'locationId' in create_payload:
                del create_payload['locationId']
            
            endpoint = f"{self.base_url}/contacts/"
            
            # Try to create contact
            response = requests.post(endpoint, json=create_payload, headers=headers, timeout=30)
            
            # GHL returns 200 for updates and 201 for successful creation
            if response.status_code in (200, 201):
                contact_data = response.json()
                contact_id = contact_data.get('contact', {}).get('id') or contact_data.get('id')
                logger.info(f"Successfully created contact {phone} with GHL (contact_id: {contact_id}, status: {response.status_code})")
                
                # Set custom values
                if custom_fields and contact_id:
                    get_contact_custom_field_mapping(location_id)
                    set_contact_custom_values(contact_id, location_id, custom_fields)
                
                return contact_data
            else:
                logger.error(f"Direct creation also failed for {phone}: {response.status_code} - {response.text}")
                return None
                
        except Exception as exc:
            logger.error(f"Failed in direct creation for {phone}: {exc}")
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

    # Resolve location: use provided location_id, or user's ghl_location_id, or default from settings
    resolved_location = location_id or getattr(user, 'ghl_location_id', None) or getattr(settings, 'GHL_DEFAULT_LOCATION', None)
    if not resolved_location:
        logger.warning("No GHL location available for user %s", user.id)
        return None, None

    try:
        client = GHLClient(location_id=resolved_location)
        
        # Ensure custom fields are properly formatted
        mapped_custom_fields = {}
        if custom_fields:
            # Field name mapping: key -> field name
            field_name_mapping = {
                'login_otp': 'Login Otp',
                'last_login_at': 'Last Login At',
                'purchase_amount': 'Purchase Amount',
                'total_coaching_session': 'Total Coaching Session',
                'total_simulator_hour': 'Total Simulator Hour',
                'last_active_package': 'Last Active Package',
                'upcoming_simulator_booking_date': 'upcoming simulator booking date',
                'upcoming_coaching_session_booking_date': 'upcoming coaching session booking date',
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
        
        # Format date_of_birth for GHL (YYYY-MM-DD format)
        date_of_birth = None
        if hasattr(user, 'date_of_birth') and user.date_of_birth:
            date_of_birth = user.date_of_birth.strftime('%Y-%m-%d')
        
        # Try to sync contact
        response = client.upsert_contact(
            phone=user.phone,
            email=getattr(user, 'email', None),
            first_name=getattr(user, 'first_name', None),
            last_name=getattr(user, 'last_name', None),
            date_of_birth=date_of_birth,
            location_id=resolved_location,
            tags=None,
            custom_fields=mapped_custom_fields,
        )
        
        contact_id = None
        if response and isinstance(response, dict):
            contact_id = response.get('contact', {}).get('id') or response.get('id')

        if contact_id and user.ghl_contact_id != contact_id:
            user.ghl_contact_id = contact_id
            user.save(update_fields=['ghl_contact_id'])
            logger.info("Saved GHL contact_id %s for user %s", contact_id, user.id)

        # If contact_id is None, try one more approach - search and update
        if not contact_id:
            logger.warning(f"Contact ID not found for user {user.id}, trying alternative approach...")
            # Try to get existing contact by phone
            contact_id = client._get_existing_contact_id(user.phone, resolved_location)
            if contact_id:
                user.ghl_contact_id = contact_id
                user.save(update_fields=['ghl_contact_id'])
                logger.info("Found existing contact ID %s for user %s", contact_id, user.id)
                
                # If we found the contact but didn't set custom fields yet, set them now
                if mapped_custom_fields and contact_id:
                    get_contact_custom_field_mapping(resolved_location)
                    set_contact_custom_values(contact_id, resolved_location, mapped_custom_fields)
                    logger.info("Set custom fields for existing contact %s", contact_id)

        logger.info("Successfully synced user %s to GHL location %s", user.id, resolved_location)
        return response, contact_id
        
    except Exception as exc:
        logger.error("GHL sync failed for user %s (location: %s): %s", 
                   user.id, resolved_location, exc, exc_info=True)
        return None, None

def calculate_total_coaching_sessions(user):
    """
    Calculate total coaching sessions available for a user from all sources:
    - Personal purchases
    - Gifted packages (accepted)
    - Transferred sessions (accepted)
    - Organization packages where user is a member
    """
    from coaching.models import CoachingPackagePurchase, OrganizationPackageMember
    from django.db.models import Sum, Q
    
    # Get all coaching package purchases for the user
    # Include: personal purchases, accepted gifts, accepted transfers
    personal_purchases = CoachingPackagePurchase.objects.filter(
        Q(client=user) | 
        Q(recipient_phone=user.phone, gift_status='accepted')
    ).exclude(
        gift_status='pending'
    ).exclude(
        purchase_type='organization'
    ).filter(
        package_status='active'
    )
    
    # Sum sessions remaining from personal purchases
    total_sessions = personal_purchases.aggregate(
        total=Sum('sessions_remaining')
    )['total'] or 0
    
    # Add organization packages where user is a member
    org_purchase_ids = OrganizationPackageMember.objects.filter(
        Q(phone=user.phone) | Q(user=user)
    ).values_list('package_purchase_id', flat=True)
    
    org_purchases = CoachingPackagePurchase.objects.filter(
        id__in=org_purchase_ids,
        purchase_type='organization',
        package_status='active',
        sessions_remaining__gt=0
    )
    
    org_sessions = org_purchases.aggregate(
        total=Sum('sessions_remaining')
    )['total'] or 0
    
    total_sessions += org_sessions
    
    return int(total_sessions)


def calculate_total_simulator_hours(user):
    """
    Calculate total simulator hours available for a user from all sources:
    - Simulator credits
    - Combo packages (coaching packages with simulator hours)
    - Simulator-only packages
    - Includes organization packages where user is a member
    """
    from decimal import Decimal
    from simulators.models import SimulatorCredit
    from coaching.models import CoachingPackagePurchase, SimulatorPackagePurchase, OrganizationPackageMember
    from django.db.models import Sum, Q
    
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
    
    # Personal combo packages
    personal_combo = base_qs.filter(
        Q(client=user) | 
        Q(recipient_phone=user.phone, gift_status='accepted')
    ).exclude(purchase_type='organization')
    
    # Organization combo packages
    org_purchase_ids = OrganizationPackageMember.objects.filter(
        Q(phone=user.phone) | Q(user=user)
    ).values_list('package_purchase_id', flat=True)
    
    org_combo = base_qs.filter(
        id__in=org_purchase_ids,
        purchase_type='organization'
    )
    
    combo_hours = (personal_combo | org_combo).aggregate(
        total=Sum('simulator_hours_remaining')
    )['total'] or Decimal('0')
    total += combo_hours
    
    # 3. Simulator-only packages
    sim_base_qs = SimulatorPackagePurchase.objects.filter(
        hours_remaining__gt=0,
        package_status='active'
    ).exclude(gift_status='pending')
    
    personal_sim = sim_base_qs.filter(
        Q(client=user) | 
        Q(recipient_phone=user.phone, gift_status='accepted')
    )
    
    sim_hours = personal_sim.aggregate(
        total=Sum('hours_remaining')
    )['total'] or Decimal('0')
    total += sim_hours
    
    return float(total)


def get_last_active_package(user):
    """
    Get the latest package purchased by the user.
    Can be coaching package, combo package, or simulator-only package.
    Returns the package name/title.
    """
    from coaching.models import CoachingPackagePurchase, SimulatorPackagePurchase
    from django.db.models import Q
    
    # Get latest coaching/combo package purchase
    latest_coaching = CoachingPackagePurchase.objects.filter(
        Q(client=user) | 
        Q(recipient_phone=user.phone, gift_status='accepted')
    ).exclude(
        gift_status='pending'
    ).exclude(
        purchase_type='organization'
    ).select_related('package').order_by('-purchased_at').first()
    
    # Get latest simulator-only package purchase
    latest_simulator = SimulatorPackagePurchase.objects.filter(
        Q(client=user) | 
        Q(recipient_phone=user.phone, gift_status='accepted')
    ).exclude(
        gift_status='pending'
    ).select_related('package').order_by('-purchased_at').first()
    
    # Compare and return the most recent
    if latest_coaching and latest_simulator:
        if latest_coaching.purchased_at > latest_simulator.purchased_at:
            return latest_coaching.package.title
        else:
            return latest_simulator.package.title
    elif latest_coaching:
        return latest_coaching.package.title
    elif latest_simulator:
        return latest_simulator.package.title
    else:
        return ''


def update_user_ghl_custom_fields(user, location_id=None):
    """
    Update GHL custom fields for a user:
    - Total Coaching Session
    - Total Simulator Hour
    - Last Active Package
    """
    if not user or not getattr(user, 'phone', None):
        logger.warning("Cannot update GHL custom fields: user or phone missing")
        return False
    
    try:
        total_sessions = calculate_total_coaching_sessions(user)
        total_hours = calculate_total_simulator_hours(user)
        last_package = get_last_active_package(user)
        
        custom_fields = {
            'total_coaching_session': str(total_sessions),
            'total_simulator_hour': str(total_hours),
            'last_active_package': last_package
        }
        
        result, contact_id = sync_user_contact(
            user,
            location_id=location_id,
            custom_fields=custom_fields
        )
        
        if contact_id:
            logger.info(f"Updated GHL custom fields for user {user.id}: sessions={total_sessions}, hours={total_hours}, package={last_package}")
            return True
        else:
            logger.warning(f"Failed to update GHL custom fields for user {user.id}")
            return False
    except Exception as exc:
        logger.error(f"Error updating GHL custom fields for user {user.id}: {exc}", exc_info=True)
        return False


def get_contact_custom_field_value(contact_id, location_id, field_key):
    """
    Get the current value of a specific custom field for a contact.
    
    Args:
        contact_id: GHL contact ID
        location_id: GHL location ID
        field_key: Internal key for the field (e.g., 'upcoming_simulator_booking_date')
    
    Returns:
        The current field value as string, or None if not found
    """
    from .models import GHLLocation
    import requests
    
    try:
        location = GHLLocation.objects.get(location_id=location_id)
        access_token = location.access_token
    except GHLLocation.DoesNotExist:
        logger.error(f"Location {location_id} not found")
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
            
            # Get field mapping to find the field ID
            field_mapping = get_contact_custom_field_mapping(location_id)
            field_id = field_mapping.get(field_key)
            
            if field_id:
                # Find the field in the custom fields list
                for field in custom_fields:
                    if field.get('id') == field_id:
                        return field.get('value')
            
            return None
        else:
            logger.error(f"Failed to get contact {contact_id}: {response.status_code} - {response.text}")
            return None
    except Exception as e:
        logger.error(f"Error getting custom field value for contact {contact_id}: {e}")
        return None


def get_first_upcoming_simulator_booking(user, location_id=None):
    """
    Get the first upcoming simulator booking for a user.
    
    Args:
        user: User instance
        location_id: Optional location ID to filter by
    
    Returns:
        Booking instance or None
    """
    from bookings.models import Booking
    from django.utils import timezone
    from django.db.models import Q
    
    query = Booking.objects.filter(
        client=user,
        booking_type='simulator',
        status='confirmed',
        start_time__gt=timezone.now()
    )
    
    if location_id:
        # Include bookings that match the location_id or have no location_id set
        query = query.filter(Q(location_id=location_id) | Q(location_id__isnull=True) | Q(location_id=''))
    
    return query.order_by('start_time').first()


def get_first_upcoming_coaching_booking(user, location_id=None):
    """
    Get the first upcoming coaching session booking for a user.
    
    Args:
        user: User instance
        location_id: Optional location ID to filter by
    
    Returns:
        Booking instance or None
    """
    from bookings.models import Booking
    from django.utils import timezone
    from django.db.models import Q
    
    query = Booking.objects.filter(
        client=user,
        booking_type='coaching',
        status='confirmed',
        start_time__gt=timezone.now()
    )
    
    if location_id:
        # Include bookings that match the location_id or have no location_id set
        query = query.filter(Q(location_id=location_id) | Q(location_id__isnull=True) | Q(location_id=''))
    
    return query.order_by('start_time').first()


def format_booking_datetime(booking):
    """
    Format booking start_time to a readable date and time string.
    Converts UTC time to Canada timezone (America/Halifax - Atlantic Time) before formatting.
    
    Args:
        booking: Booking instance
    
    Returns:
        Formatted string like "21-OCT-2021 08:30 AM" or empty string if no booking
    """
    if not booking or not booking.start_time:
        return ''
    
    from django.utils import timezone
    from zoneinfo import ZoneInfo
    
    # Convert to local timezone if needed
    start_time = booking.start_time
    
    # Convert UTC to Canada timezone (America/Halifax handles AST/ADT automatically)
    canada_tz = ZoneInfo('America/Halifax')
    
    if timezone.is_aware(start_time):
        # Convert from UTC (or current timezone) to Canada Atlantic Time
        # Django stores datetimes in UTC when USE_TZ=True, so convert to UTC first if needed
        start_time = start_time.astimezone(ZoneInfo('UTC'))
        # Then convert from UTC to Canada timezone
        start_time = start_time.astimezone(canada_tz)
    else:
        # If naive, assume UTC and convert to Canada timezone
        start_time = timezone.make_aware(start_time, ZoneInfo('UTC'))
        start_time = start_time.astimezone(canada_tz)
    
    # Format: "DD-MMM-YYYY HH:MM AM/PM" (e.g., "21-OCT-2021 08:30 AM")
    day = start_time.strftime("%d")
    month = start_time.strftime("%b").upper()  # Uppercase 3-letter month abbreviation
    year = start_time.strftime("%Y")
    time_str = start_time.strftime("%I:%M %p")  # 12-hour format with AM/PM
    
    formatted_date = f"{day}-{month}-{year} {time_str}"
    return formatted_date


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
                'login_otp': 'Login OTP',
                'last_login_at': 'Last Login At',
                'purchase_amount': 'Purchase Amount',
                'total_coaching_session': 'Total Coaching Session',
                'total_simulator_hour': 'Total Simulator Hour',
                'last_active_package': 'Last Active Package'
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
                    if field_id == field_mapping.get('login_otp'):
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