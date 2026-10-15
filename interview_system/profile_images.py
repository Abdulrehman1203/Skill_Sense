"""Store personal profile images in Clerk, retaining only their URL locally."""

from urllib.parse import quote

import requests
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from rest_framework.exceptions import APIException


class ProfileImageUploadError(APIException):
    status_code = 502
    default_detail = "Clerk could not save your profile picture. Please retry."


def upload_profile_image(user, image):
    secret = getattr(settings, "CLERK_SECRET_KEY", "")
    if not secret or not user.clerk_id:
        raise ProfileImageUploadError("Profile picture upload is unavailable.")

    image.seek(0)
    try:
        response = requests.post(
            f"https://api.clerk.com/v1/users/{quote(user.clerk_id, safe='')}/profile_image",
            headers={"Authorization": f"Bearer {secret}"},
            files={"file": (image.name, image, image.content_type)},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or data.get("id") != user.clerk_id:
            raise ValueError("Unexpected Clerk user.")
        url = data.get("image_url")
        if not isinstance(url, str) or not url or len(url) > 1024:
            raise ValueError("Missing image URL.")
        URLValidator(schemes=["https"])(url)
        if data.get("has_image") is not True:
            raise ValueError("Clerk did not confirm the uploaded image.")
        return url
    except (requests.RequestException, ValueError, ValidationError) as exc:
        raise ProfileImageUploadError() from exc
