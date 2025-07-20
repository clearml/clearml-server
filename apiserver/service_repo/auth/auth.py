import base64
from datetime import datetime
from time import time

import bcrypt
import jwt
from mongoengine import Q

from apiserver.apierrors import errors
from apiserver.config_repo import config
from apiserver.database.errors import translate_errors_context
from apiserver.database.model.auth import User, Entities, Credentials
from apiserver.database.model.company import Company
from apiserver.database.utils import get_options
from apiserver.redis_manager import redman
from .fixed_user import FixedUser
from .identity import Identity
from .payload import Payload, Token, Basic, AuthType

log = config.logger(__file__)
entity_keys = set(get_options(Entities))
verify_user_tokens = config.get("apiserver.auth.verify_user_tokens", True)
_revoked_tokens_key = "revoked_tokens"
redis = redman.connection("apiserver")


def get_auth_func(auth_type):
    if auth_type == AuthType.bearer_token:
        return authorize_token
    elif auth_type == AuthType.basic:
        return authorize_credentials
    raise errors.unauthorized.BadAuthType()


def authorize_token(jwt_token, service, action, call):
    """Validate token against service/endpoint and requests data (dicts).
    Returns a parsed token object (auth payload)
    """
    call_info = {"ip": call.real_ip}

    def log_error(msg):
        info = ", ".join(f"{k}={v}" for k, v in call_info.items())
        log.error(f"{msg} Call info: {info}")

    try:
        token = Token.from_encoded_token(jwt_token)
        if is_token_revoked(token):
            raise errors.unauthorized.InvalidToken("revoked token")
        return token
    except jwt.exceptions.InvalidKeyError as ex:
        log_error("Failed parsing token.")
        raise errors.unauthorized.InvalidToken(
            "jwt invalid key error", reason=ex.args[0]
        )
    except jwt.InvalidTokenError as ex:
        log_error("Failed parsing token.")
        raise errors.unauthorized.InvalidToken("invalid jwt token", reason=ex.args[0])
    except ValueError as ex:
        log_error(f"Failed while processing token: {str(ex.args[0])}.")
        raise errors.unauthorized.InvalidToken(
            "failed processing token", reason=ex.args[0]
        )
    except Exception:
        log_error("Failed processing token.")
        raise


def authorize_credentials(auth_data, service, action, call):
    """Validate credentials against service/action and request data (dicts).
    Returns a new basic object (auth payload)
    """
    try:
        access_key, _, secret_key = (
            base64.b64decode(auth_data.encode()).decode("latin-1").partition(":")
        )
    except Exception as e:
        log.exception("malformed credentials")
        raise errors.unauthorized.BadCredentials(str(e))

    query = Q(credentials__match=Credentials(key=access_key, secret=secret_key))

    fixed_user = None

    if FixedUser.enabled():
        fixed_user = FixedUser.get_by_username(access_key)
        if fixed_user:
            if FixedUser.pass_hashed():
                if not compare_secret_key_hash(secret_key, fixed_user.password):
                    raise errors.unauthorized.InvalidCredentials(
                        "bad username or password"
                    )
            else:
                if secret_key != fixed_user.password:
                    raise errors.unauthorized.InvalidCredentials(
                        "bad username or password"
                    )

            if fixed_user.is_guest and not FixedUser.is_guest_endpoint(service, action):
                raise errors.unauthorized.InvalidCredentials(
                    "endpoint not allowed for guest"
                )

            query = Q(id=fixed_user.id)

    with translate_errors_context("authorizing request"):
        user = User.objects(query).first()
        if not user:
            raise errors.unauthorized.InvalidCredentials(
                "failed to locate provided credentials"
            )

        if not fixed_user:
            # In case these are proper credentials, update last used time
            User.objects(id=user.id, credentials__key=access_key).update(
                **{
                    "set__credentials__$__last_used": datetime.utcnow(),
                    "set__credentials__$__last_used_from": call.get_worker(
                        default=call.real_ip
                    ),
                }
            )

    company = Company.objects(id=user.company).only("id", "name").first()

    if not company:
        raise errors.unauthorized.InvalidCredentials("invalid user company")

    identity = Identity(
        user=user.id,
        company=user.company,
        role=user.role,
        user_name=user.name,
        company_name=company.name,
    )

    basic = Basic(user_key=access_key, identity=identity)

    return basic


def authorize_impersonation(user, identity, service, action, call):
    """ Returns a new basic object (auth payload)"""
    if not user:
        raise ValueError("missing user")

    company = Company.objects(id=user.company).only("id", "name").first()
    if not company:
        raise errors.unauthorized.InvalidCredentials("invalid user company")

    return Payload(auth_type=None, identity=identity)


def compare_secret_key_hash(secret_key: str, hashed_secret: str) -> bool:
    """
    Compare hash for the passed secret key with the passed hash
    :return: True if equal. Otherwise False
    """
    return bcrypt.checkpw(
        secret_key.encode(), base64.b64decode(hashed_secret.encode("ascii"))
    )


def is_token_revoked(token: Token) -> bool:
    if not isinstance(token, Token) or not token.session_id:
        return False

    return redis.zscore(_revoked_tokens_key, token.session_id) is not None


def revoke_auth_token(token: Token):
    if not isinstance(token, Token) or not token.session_id:
        return

    timestamp_now = int(time())
    expiration_timestamp = token.exp
    if not expiration_timestamp:
        expiration_timestamp = timestamp_now + Token.default_expiration_sec

    redis.zadd(_revoked_tokens_key, {token.session_id: expiration_timestamp})
    redis.zremrangebyscore(_revoked_tokens_key, min=0, max=timestamp_now)
