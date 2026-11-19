import hashlib
import time

import httpx
from fastapi import HTTPException


class Authorizer:
    def __init__(self, config):
        self.config = config
        self.client = httpx.AsyncClient(timeout=8)
        self.cache = {}

    async def close(self):
        await self.client.aclose()

    async def user(self, authorization):
        if not self.config.auth_configured:
            raise HTTPException(503, "Configure Supabase and CAMERA_ALLOWED_USER_IDS on the camera server")
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer":
            raise HTTPException(401, "Sign in to access this camera")
        if not token or len(token) > 8192 or any(character.isspace() for character in token):
            raise HTTPException(401, "Invalid access token")
        key = hashlib.sha256(token.encode()).hexdigest()
        cached = self.cache.get(key)
        if cached and cached[1] > time.monotonic():
            return cached[0]
        try:
            response = await self.client.get(
                self.config.supabase_url + "/auth/v1/user",
                headers={"apikey": self.config.supabase_key, "Authorization": authorization},
            )
        except httpx.HTTPError:
            raise HTTPException(503, "Could not verify sign-in. Check the server internet connection.")
        if response.status_code in (401, 403):
            raise HTTPException(401, "Sign-in expired. Reconnect after signing in.")
        if response.status_code != 200:
            raise HTTPException(503, "Sign-in service is unavailable")
        try:
            payload = response.json()
            user_id = payload.get("id") if isinstance(payload, dict) else None
        except ValueError:
            raise HTTPException(503, "Sign-in service returned an invalid response")
        if not isinstance(user_id, str):
            raise HTTPException(503, "Sign-in service returned an invalid response")
        if user_id not in self.config.allowed_users:
            raise HTTPException(403, "This account has not been granted access to this camera")
        self.cache = {key: value for key, value in self.cache.items() if value[1] > time.monotonic()}
        self.cache[key] = (user_id, time.monotonic() + 10)
        return user_id
