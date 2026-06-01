from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.security import OAuth2PasswordRequestForm

from src.auth import (
    AuthCredentials,
    AuthResponse,
    AuthUser,
    CurrentUser,
    login_user,
    login_user_with_oauth_form,
    logout_user,
    signup_user,
    user_to_response,
)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.post("/signup", response_model=AuthResponse)
def signup(credentials: AuthCredentials) -> AuthResponse:
    return signup_user(credentials)


@router.post("/login", response_model=AuthResponse)
def login(credentials: AuthCredentials) -> AuthResponse:
    return login_user(credentials)


@router.post("/token", response_model=AuthResponse)
def token_login(response: Annotated[AuthResponse, Depends(login_user_with_oauth_form)]) -> AuthResponse:
    return response


@router.get("/me", response_model=AuthUser)
def me(current_user: CurrentUser) -> AuthUser:
    return user_to_response(current_user)


@router.post("/logout")
def logout() -> dict[str, bool]:
    return logout_user()
