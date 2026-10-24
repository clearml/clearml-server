from uuid import uuid4

import jwt

from datetime import datetime, timedelta

from jwt.algorithms import get_default_algorithms

from apiserver.apierrors import errors
from apiserver.config import ConfigurationError
from apiserver.config_repo import config
from apiserver.database.model.auth import Role

from .auth_type import AuthType
from .payload import Payload

token_secret = config.get("secure.auth.token_secret", None)
if (
    not isinstance(token_secret, str)
    or len(token_secret.strip()) < 32
    or token_secret.strip() == "Rq8FW84sSqVgq7WvBB_4EzNl9y8z8IGiDXX3C345_a5AZfcwZcwCIA"
):
    raise ConfigurationError(
        "Set secure.auth.token_secret to a unique random signing key of at least "
        "32 characters (e.g. openssl rand -hex 32), using external secure.conf or "
        "CLEARML__SECURE__AUTH__TOKEN_SECRET. The published default is not accepted."
    )

log = config.logger(__file__)


class Token(Payload):
    default_expiration_sec = config.get("apiserver.auth.default_expiration_sec")

    def __init__(
        self,
        exp=None,
        iat=None,
        nbf=None,
        env=None,
        identity=None,
        session_id=None,
        entities=None,
        **_,
    ):
        super(Token, self).__init__(
            AuthType.bearer_token, identity=identity, entities=entities
        )
        self.exp = exp
        self.iat = iat
        self.nbf = nbf
        self._session_id = session_id
        self._env = env or config.get("env", "<unknown>")

    @property
    def session_id(self):
        return self._session_id

    @property
    def env(self):
        return self._env

    @property
    def exp(self):
        return self._exp

    @exp.setter
    def exp(self, value):
        self._exp = value

    @property
    def iat(self):
        return self._iat

    @iat.setter
    def iat(self, value):
        self._iat = value

    @property
    def nbf(self):
        return self._nbf

    @nbf.setter
    def nbf(self, value):
        self._nbf = value

    def get_log_entry(self):
        d = super(Token, self).get_log_entry()
        d.update(iat=self.iat, exp=self.exp, env=self.env)
        return d

    def encode(self, **extra_payload):
        payload = self.to_dict(**extra_payload)
        return jwt.encode(payload, token_secret)

    @classmethod
    def decode(cls, encoded_token, verify=True):
        options = (
            {"verify_signature": False, "verify_exp": True} if not verify else None
        )
        return jwt.decode(
            encoded_token,
            token_secret,
            algorithms=get_default_algorithms(),
            options=options,
        )

    @classmethod
    def from_encoded_token(cls, encoded_token, verify=True):
        decoded = cls.decode(encoded_token, verify=verify)
        try:
            token = Token.from_dict(decoded)
            assert isinstance(token, Token)
            if not token.identity:
                raise errors.unauthorized.InvalidToken("token missing identity")
            return token
        except Exception as e:
            raise errors.unauthorized.InvalidToken(
                "failed parsing token", reason=e.args[0]
            )

    @classmethod
    def create_encoded_token(
        cls, identity, expiration_sec=None, entities=None, **extra_payload
    ):
        if identity.role not in (Role.system,):
            # limit expiration time for all roles but an internal service
            expiration_sec = expiration_sec or cls.default_expiration_sec

        now = datetime.utcnow()
        session_id = uuid4().hex

        token = cls(
            identity=identity, entities=entities, iat=now, session_id=session_id
        )

        if expiration_sec:
            # add 'expiration' claim
            token.exp = now + timedelta(seconds=expiration_sec)

        return token.encode(**extra_payload)

    @classmethod
    def decode_identity(cls, encoded_token):
        # noinspection PyBroadException
        try:
            from ..auth import Identity

            decoded = cls.decode(encoded_token, verify=False)
            return Identity.from_dict(decoded.get("identity", {}))
        except Exception as ex:
            log.error(f"Failed parsing identity from encoded token: {ex}")
