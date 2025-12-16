#!/usr/bin/env python3
"""
API Authentication and Authorization Middleware
"""

from fastapi import HTTPException, Security, Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from datetime import datetime, timedelta
import jwt
import os
from typing import Optional

security = HTTPBearer()

SECRET_KEY = os.getenv("JWT_SECRET_KEY", "change-this-secret-key")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30


class TokenManager:
    """Manage JWT tokens for API authentication"""
    
    @staticmethod
    def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
        """Create JWT access token"""
        to_encode = data.copy()
        if expires_delta:
            expire = datetime.utcnow() + expires_delta
        else:
            expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
        
        to_encode.update({"exp": expire})
        encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
        return encoded_jwt
    
    @staticmethod
    def verify_token(token: str) -> dict:
        """Verify and decode JWT token"""
        try:
            payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
            return payload
        except jwt.ExpiredSignatureError:
            raise HTTPException(status_code=401, detail="Token has expired")
        except jwt.JWTError:
            raise HTTPException(status_code=401, detail="Invalid token")


async def get_current_user(credentials: HTTPAuthorizationCredentials = Security(security)):
    """Dependency to get current authenticated user"""
    token = credentials.credentials
    payload = TokenManager.verify_token(token)
    
    username = payload.get("sub")
    if username is None:
        raise HTTPException(status_code=401, detail="Invalid authentication credentials")
    
    return {"username": username, "roles": payload.get("roles", [])}


def require_role(required_role: str):
    """Decorator to require specific role"""
    async def role_checker(current_user: dict = Depends(get_current_user)):
        if required_role not in current_user.get("roles", []):
            raise HTTPException(
                status_code=403,
                detail=f"Insufficient permissions. Required role: {required_role}"
            )
        return current_user
    return role_checker


# API Key authentication (simpler alternative)
API_KEYS = {
    os.getenv("API_KEY_1", "dev-key-1"): "admin",
    os.getenv("API_KEY_2", "dev-key-2"): "read-only"
}


async def verify_api_key(credentials: HTTPAuthorizationCredentials = Security(security)):
    """Verify API key"""
    api_key = credentials.credentials
    
    if api_key not in API_KEYS:
        raise HTTPException(status_code=401, detail="Invalid API key")
    
    return {"role": API_KEYS[api_key]}


# Usage in server.py:
# @self.app.post("/scale", dependencies=[Depends(require_role("admin"))])
# async def manual_scale(request: ScalingRequest):
#     ...
