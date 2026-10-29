import uuid
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

# Strict E.164; the client formats the number, the server does not normalize.
PHONE_PATTERN = r"^\+[1-9]\d{7,14}$"
Phone = Annotated[str, StringConstraints(pattern=PHONE_PATTERN)]
Email = Annotated[
    str,
    StringConstraints(strip_whitespace=True, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$"),
]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
# 128 stops argon2 being used as a CPU/memory DoS with huge inputs.
MAX_PASSWORD_LENGTH = 128


class OtpRequest(BaseModel):
    phone: Phone


class OtpVerify(BaseModel):
    phone: Phone
    code: Annotated[str, StringConstraints(pattern=r"^\d{6}$")]
    name: Name | None = None


class RegisterRequest(BaseModel):
    name: Name
    email: Email
    password: Annotated[str, StringConstraints(min_length=10, max_length=MAX_PASSWORD_LENGTH)]


class LoginRequest(BaseModel):
    email: Email
    password: Annotated[str, StringConstraints(min_length=1, max_length=MAX_PASSWORD_LENGTH)]


class UserOut(BaseModel):
    """Never includes password_hash."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    email: str | None
    phone: str | None
    role: str


class AuthResponse(BaseModel):
    access_token: str
    user: UserOut


class RegisterResponse(BaseModel):
    user: UserOut
