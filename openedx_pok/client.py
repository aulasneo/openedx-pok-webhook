"""
POK API client for certificate issuance.
"""
import json
import logging
from datetime import datetime
from urllib.parse import urljoin

import requests
from django.conf import settings
from opaque_keys.edx.keys import CourseKey
from openedx_pok.models import CertificateTemplate

from openedx_pok.utils import split_name

logger = logging.getLogger(__name__)


class PokApiClient:
    """Client for POK API."""

    def __init__(self, course_id: str):
        """Inicializa el cliente de la API POK."""
        course_key = CourseKey.from_string(course_id)

        try:
            template = CertificateTemplate.objects.get(course=course_key)
        except CertificateTemplate.DoesNotExist:
            template = None

        self.api_key =  settings.POK_API_KEY
        self.template = template.template_id if template else settings.POK_TEMPLATE_ID
        self.emission_type = template.emission_type if template else "pok"
        self.page = template.page_id if template and template.page_id is not None else settings.POK_PAGE_ID

        self.base_url = settings.POK_API_URL
        self.timeout = settings.POK_TIMEOUT

    def _get_headers(self, is_preview=False):
        headers = {
            'Authorization': f'ApiKey {self.api_key}',
            'Accept': 'application/json',
        }

        if is_preview:
            headers['Content-Type'] = 'application/json'  # Required by /template-preview
        return headers

    def _get_active_custom_parameters(self):
        """
        Devuelve un diccionario con los parámetros personalizados activos de la plantilla POK.
        Ejemplo: {'grade': '23978c77-...', 'signatory': '9ea35e3e-...'}
        """
        try:
            endpoint = urljoin(self.base_url, f'template/{self.template}')
            response = requests.get(endpoint, headers=self._get_headers(), timeout=self.timeout)
            response.raise_for_status()
            data = response.json()

            custom_parameters = data.get("customParameters", [])
            active_params = {
                param["label"]: param["id"]
                for param in custom_parameters
                if param.get("label") and param.get("id")
            }

            logger.info(f"[POK] Active custom parameters for template {self.template}: {json.dumps(active_params, indent=2)}")
            return active_params

        except Exception as e:
            logger.warning(f"[POK] Could not fetch active template attributes: {str(e)}")
            return {}

    def get_organization_details(self):
        """
        Retrieve details of the organization which owns the provided API key.

        Returns:
            dict: Organization details including wallet, name, available credits, etc.
        """
        endpoint = urljoin(self.base_url, 'organization/me')

        try:
            response = requests.get(
                endpoint,
                headers=self._get_headers(),
                timeout=self.timeout
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching organization details: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }

    def get_templates(self):
        """
        Retrieve available certificate templates.

        Returns:
            dict: Available templates for the organization
        """
        endpoint = urljoin(self.base_url, 'templates')

        try:
            response = requests.get(
                endpoint,
                headers=self._get_headers(),
                timeout=self.timeout
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching templates: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }

    def request_certificate(self, user, course_key, mode, organization, course_title,
                            **kwargs):
        endpoint = urljoin(self.base_url, 'credential/')
        email = user.email
        first_name, last_name = split_name(user.profile.name)

        active_params = self._get_active_custom_parameters()
        custom_params = {}
        for param in active_params:
            custom_params[param] = kwargs.get(param, "")

        payload = {
            "credential": {
                "tags": [
                    f"StudentId:{user.id}",
                    f"CourseId:{course_key}",
                    f"Mode:{mode}"
                ],
                "skipAcceptance": True,
                "emissionType": self.emission_type,
                "dateFormat": settings.POK_DATE_FORMAT,
                "emissionDate": datetime.now().isoformat(),
                "title": course_title,
                "emitter": organization
            },
            "receiver": {
                "languageTag": "es-ES",
                "identification": str(user.id),
                "email": email,
                "lastName": last_name,
                "firstName": first_name
            },
            "customization": {
                "page": self.page,
                "template": {
                    "customParameters": custom_params,
                    "id": self.template
                }
            }
        }

        logger.info(f"[POK] Final payload sent to API: {json.dumps(payload, indent=2)}")

        try:
            logger.info(f"Sending certificate request to POK for user {user.id} in course {course_key}")
            response = requests.post(endpoint, json=payload, headers=self._get_headers())

            if response.status_code != 200:
                logger.error(f"POK returned non-200 status: {response.status_code}, body: {response.text}")
                return {
                    'success': False,
                    'error': f"Non-200 status: {response.status_code}",
                    'content': response.json()
                }

            return_data = response.json()
            credential_id = return_data.get("id")
            return self.get_credential_details(credential_id)

        except requests.exceptions.RequestException as e:
            logger.error(f"Error requesting certificate from POK: {str(e)}")
            return {'success': False, 'error': str(e)}
        except Exception as e:
            logger.exception(f"Unexpected error in POK API client: {str(e)}")
            return {'success': False, 'error': str(e)}



    def get_credential_details(self, certificate_id, decrypted=None):
        """
        Get details for a specific credential.

        Args:
            certificate_id: ID of the credential to retrieve
            decrypted: Optional decrypted data

        Returns:
            dict: Credential details
        """
        endpoint = urljoin(self.base_url, f'credential/{certificate_id}/')

        if decrypted:
            endpoint = urljoin(endpoint, "decrypted-image/")

        try:
            response = requests.get(
                endpoint,
                headers=self._get_headers(),
                timeout=self.timeout
            )
            response.raise_for_status()
            return {"success": True, "content": response.json()}

        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching credential details: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }

    def get_template_preview(self, user, organization, course_title, **kwargs):
        """
        Get a preview image of a certificate template using the correct minimal payload.
        """
        endpoint = urljoin(self.base_url, "template/preview")

        user_name = getattr(user.profile, "name")
        if not user_name:
            user_name = user.username

        first_name, last_name = split_name(user_name)

        active_params = self._get_active_custom_parameters()
        custom_params = {}
        for param in active_params:
            custom_params[param] = kwargs.get(param, "")

        payload = {
            "credential": {
                "emissionType": self.emission_type,
                "dateFormat": settings.POK_DATE_FORMAT,
                "emissionDate": datetime.now().isoformat(),
                "title": course_title,
                "emitter": organization
            },
            "receiver": {
                "languageTag": "es-ES",
                "firstName": first_name,
                "lastName": last_name
            },
            "customization": {
                "template": {
                    "customParameters": custom_params,
                    "id": self.template
                }
            }
        }

        try:
            response = requests.post(
                endpoint,
                json=payload,
                headers=self._get_headers(is_preview=True),
                timeout=self.timeout
            )
            response.raise_for_status()

            data = response.json()
            logger.info(f"POK preview raw response: {json.dumps(data, indent=2)}")

            preview_url = (
                data.get("previewUrl") or
                data.get("preview_url") or
                data.get("url") or
                data.get("location")
            )

            return {
                "success": True,
                "preview": data,
                "preview_url": preview_url
            }

        except requests.exceptions.HTTPError as e:
            response = e.response
            logger.error(f"POK preview HTTP error: {response.status_code} - {response.reason} | Response body: {response.text}")
            return {"success": False, "error": f"{response.status_code}: {response.text}"}

        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching template preview: {str(e)}")
            return {"success": False, "error": str(e)}

