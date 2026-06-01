from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select

from src.database import User, get_session


EMAIL_RE = re.compile(r"\s+")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "1440"))
SECRET_KEY = os.getenv("SECRET_KEY", "change-this-secret-key-in-production")
ALGORITHM = "HS256"

password_context = CryptContext(schemes=["argon2"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token")


class AuthCredentials(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class AuthUser(BaseModel):
    id: int
    email: str


class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: AuthUser

    @property
    def token(self) -> str:
        return self.access_token


def normalize_email(email: str) -> str:
    return EMAIL_RE.sub("", email).lower()


def hash_password(password: str) -> str:
    return password_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return password_context.verify(password, password_hash)


def user_to_response(user: User) -> AuthUser:
    return AuthUser(id=user.id, email=user.email)


def create_access_token(user: User) -> str:
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload: dict[str, Any] = {
        "sub": str(user.id),
        "email": user.email,
        "exp": expires_at,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def authenticate_user(email: str, password: str) -> User | None:
    normalized_email = normalize_email(email)
    with get_session() as session:
        user = session.execute(select(User).where(User.email == normalized_email)).scalar_one_or_none()
        if user is None or not verify_password(password, user.password_hash):
            return None

        session.expunge(user)
        return user


def auth_response_for_user(user: User) -> AuthResponse:
    return AuthResponse(access_token=create_access_token(user), user=user_to_response(user))


def signup_user(credentials: AuthCredentials) -> AuthResponse:
    email = normalize_email(credentials.email)
    with get_session() as session:
        existing_user = session.execute(select(User).where(User.email == email)).scalar_one_or_none()
        if existing_user is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email is already registered")

        user = User(email=email, password_hash=hash_password(credentials.password))
        session.add(user)
        session.flush()
        return auth_response_for_user(user)


def login_user(credentials: AuthCredentials) -> AuthResponse:
    user = authenticate_user(credentials.email, credentials.password)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")
    return auth_response_for_user(user)


def login_user_with_oauth_form(form_data: Annotated[OAuth2PasswordRequestForm, Depends()]) -> AuthResponse:
    user = authenticate_user(form_data.username, form_data.password)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")
    return auth_response_for_user(user)


def get_current_user(token: Annotated[str, Depends(oauth2_scheme)]) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        subject = payload.get("sub")
        user_id = int(subject) if subject is not None else None
    except (JWTError, TypeError, ValueError):
        raise credentials_exception

    if user_id is None:
        raise credentials_exception

    with get_session() as session:
        user = session.get(User, user_id)
        if user is None:
            raise credentials_exception

        session.expunge(user)
        return user


def logout_user() -> dict[str, bool]:
    return {"ok": True}


CurrentUser = Annotated[User, Depends(get_current_user)]
