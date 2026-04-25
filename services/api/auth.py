"""Authentication: Google OAuth ID token verification + JWT session tokens."""

import datetime
import logging
import os
import uuid

import google.auth.transport.requests
import google.oauth2.id_token
import jwt
from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)

JWT_SECRET = os.getenv("JWT_SECRET", "sketchmind-dev-secret-change-in-prod")
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_HOURS = 72
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")


def verify_google_token(id_token: str) -> dict:
    """Verify a Google OAuth ID token and return user info.

    Returns dict with: google_id, email, name, picture.
    """
    try:
        req = google.auth.transport.requests.Request()
        id_info = google.oauth2.id_token.verify_oauth2_token(
            id_token, req, GOOGLE_CLIENT_ID,
        )
        if id_info["iss"] not in ("accounts.google.com", "https://accounts.google.com"):
            raise ValueError("Invalid issuer")
        return {
            "google_id": id_info["sub"],
            "email": id_info.get("email", ""),
            "name": id_info.get("name", ""),
            "picture": id_info.get("picture", ""),
        }
    except Exception as exc:
        logger.warning("Google token verification failed: %s", exc)
        raise HTTPException(status_code=401, detail="Invalid Google token")


def create_token(user_id: str, email: str, name: str) -> str:
    """Create a JWT session token for an authenticated user."""
    payload = {
        "sub": user_id,
        "email": email,
        "name": name,
        "exp": datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(hours=JWT_EXPIRY_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    """Decode and validate a JWT session token."""
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


def get_current_user(request: Request) -> dict:
    """Extract and verify JWT from Authorization header. Raises 401 if missing."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing auth token")
    return decode_token(auth[7:])


def get_optional_user(request: Request) -> dict | None:
    """Like get_current_user but returns None instead of raising."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    try:
        return decode_token(auth[7:])
    except HTTPException:
        return None


def generate_id() -> str:
    """Generate a short unique ID."""
    return uuid.uuid4().hex[:12]
