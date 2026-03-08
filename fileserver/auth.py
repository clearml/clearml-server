import json
from hashlib import sha256
from typing import Optional

import attr
from flask import abort, Response, Request
from redis import StrictRedis
from clearml_agent.backend_api import Session
from werkzeug.exceptions import HTTPException

from config import config
from redis_manager import redman

log = config.logger(__file__)


@attr.s(auto_attribs=True)
class TokenInfo:
    company: str
    user: str


class FileserverSession(Session):
    @property
    def client(self):
        return "fileserver"

    @client.setter
    def client(self, _):
        # do not allow the base class to override the client
        pass


class AuthHandler:
    enabled = config.get("fileserver.auth.enabled", False)
    _instance = None

    @classmethod
    def instance(cls):
        if not cls.enabled:
            return None
        if not cls._instance:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        self.session = FileserverSession(
            api_key=config.get("secure.credentials.fileserver.user_key"),
            secret_key=config.get("secure.credentials.fileserver.user_secret"),
            host=config.get("hosts.api_server"),
            initialize_logging=False,
        )

        self.redis: StrictRedis = redman.connection("fileserver")

    def _validate_and_get_token_info(self, token: str) -> TokenInfo:
        token_hash = sha256(token.encode()).hexdigest() if len(token) > 256 else token
        key = f"token_{token_hash}"
        token_data = self.redis.get(key)
        if token_data:
            return TokenInfo(**json.loads(token_data))

        try:
            res = self.session.send_request(
                service="auth", action="validate_token", json={"token": token}
            )

            if res.status_code == 500:
                log.error("Error validating token")
                abort(Response(f"Internal error (status={res.status_code})", 500))
            elif res.status_code != 200:
                log.error("Error validating token")
                abort(res.status_code)

            data = res.json()["data"]
            if not data["valid"]:
                log.error(f"Error validating token: {data['msg']}")
                abort(Response(data["msg"], 401))

            info = TokenInfo(
                company=data.get("company", "unknown"),
                user=data.get("user"),
            )

            timeout_sec = config.get(
                "fileserver.auth.tokens_cache_threshold_sec", 12 * 60 * 60
            )
            self.redis.setex(key, time=timeout_sec, value=json.dumps(attr.asdict(info)))

            return info

        except HTTPException:
            raise
        except Exception:
            log.exception("Failed decoding token")
            abort(500)

    def validate(self, request: Request):
        token = self.get_token(request)
        if not token:
            log.error("Error getting token")
            abort(401)

        self._validate_and_get_token_info(token)

    @staticmethod
    def get_token(request: Request) -> Optional[str]:
        auth_header = request.headers.get("Authorization")
        if auth_header:
            if not auth_header.startswith("Bearer "):
                log.error("Only bearer token authorization is supported")
                abort(
                    Response("Only bearer token authorization is supported", status=401)
                )
            token = auth_header.partition(" ")[2]
            return token

        last_ex = None
        for cookie_name in config.get("fileserver.auth.cookie_names", []):
            cookie = request.cookies.get(cookie_name)
            if not cookie:
                continue
            try:
                return cookie
            except HTTPException as ex:
                last_ex = ex

        if last_ex:
            raise last_ex
