import pytest
from pydantic import ValidationError

from app.core.security import derive_otp_key, otp_mac, password_needs_rehash
from app.schemas.auth import LoginRequest, OtpRequest, OtpVerify, RegisterRequest
from app.services.auth import DUMMY_PASSWORD_HASH
from tests.helpers import make_settings


@pytest.mark.parametrize("phone", ["+919876543210", "+14155552671", "+12345678"])
def test_valid_e164_phones(phone: str) -> None:
    assert OtpRequest(phone=phone).phone == phone


@pytest.mark.parametrize(
    "phone",
    [
        "9876543210",
        "+0123456789",
        "+1234567",
        "+1234567890123456",
        "+91 98765 43210",
        "+919876543210\n",
        " +919876543210",
        "+91-9876543210",
        "",
    ],
)
def test_invalid_phones_are_rejected_without_normalization(phone: str) -> None:
    with pytest.raises(ValidationError):
        OtpRequest(phone=phone)


@pytest.mark.parametrize("code", ["12345", "1234567", "12345a", "123456\n", ""])
def test_invalid_codes_are_rejected(code: str) -> None:
    with pytest.raises(ValidationError):
        OtpVerify(phone="+919876543210", code=code)


@pytest.mark.parametrize("password", ["x" * 9, "x" * 129])
def test_password_length_bounds(password: str) -> None:
    with pytest.raises(ValidationError):
        RegisterRequest(name="n", email="a@b.co", password=password)
    with pytest.raises(ValidationError):
        LoginRequest(email="a@b.co", password="x" * 129)


def test_password_bounds_are_inclusive() -> None:
    RegisterRequest(name="n", email="a@b.co", password="x" * 10)
    RegisterRequest(name="n", email="a@b.co", password="x" * 128)


@pytest.mark.parametrize(
    "email", ["plain", "a@b", "a b@c.co", "@b.co", "a@@b.co", "a@b.co" + "x" * 250]
)
def test_invalid_emails_are_rejected(email: str) -> None:
    with pytest.raises(ValidationError):
        RegisterRequest(name="n", email=email, password="x" * 10)


def test_otp_key_is_domain_separated_and_macs_are_labelled() -> None:
    settings = make_settings()
    key = derive_otp_key(settings)
    assert key != settings.jwt_secret.encode() and len(key) == 32
    assert otp_mac(key, "phone", "x") != otp_mac(key, "code", "x")
    assert otp_mac(key, "phone", "x") == otp_mac(key, "phone", "x")


def test_dummy_hash_matches_the_current_argon2_parameters() -> None:
    # Otherwise unknown emails would be verified with a cheaper hash than real users.
    assert not password_needs_rehash(DUMMY_PASSWORD_HASH)
