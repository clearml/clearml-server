from apiserver.apierrors import errors
from apiserver.apimodels.auth import (
    GetTokenResponse,
    GetTokenForUserRequest,
    GetTokenRequest,
    ValidateTokenRequest,
    ValidateResponse,
    CreateUserRequest,
    CreateUserResponse,
    CreateCredentialsResponse,
    GetCredentialsResponse,
    RevokeCredentialsResponse,
    CredentialsResponse,
    RevokeCredentialsRequest,
    EditUserReq,
    CreateCredentialsRequest,
    EditCredentialsRequest,
)
from apiserver.apimodels.base import UpdateResponse
from apiserver.bll.auth import AuthBLL
from apiserver.config import info
from apiserver.config_repo import config
from apiserver.database.errors import translate_errors_context
from apiserver.database.model.auth import User, Role
from apiserver.service_repo import APICall, endpoint
from apiserver.service_repo.auth import Token
from apiserver.service_repo.auth.auth import is_token_revoked, revoke_auth_token
from apiserver.service_repo.auth.fixed_user import FixedUser

log = config.logger(__file__)


@endpoint(
    "auth.login",
    request_data_model=GetTokenRequest,
    response_data_model=GetTokenResponse,
)
def login(call: APICall, *_, **__):
    """Generates a token based on the authenticated user (intended for use with credentials)"""
    call.result.data_model = AuthBLL.get_token_for_user(
        user_id=call.identity.user,
        company_id=call.identity.company,
        expiration_sec=call.data_model.expiration_sec,
    )

    # Add authorization cookie
    call.result.set_auth_cookie(call.result.data_model.token)


@endpoint("auth.logout", min_version="2.2")
def logout(call: APICall, *_, **__):
    revoke_auth_token(call.auth)
    call.result.set_auth_cookie(None)


@endpoint(
    "auth.get_token_for_user",
    request_data_model=GetTokenForUserRequest,
    response_data_model=GetTokenResponse,
)
def get_token_for_user(call: APICall, _: str, request: GetTokenForUserRequest):
    """Generates a token based on a requested user and company. INTERNAL."""
    if call.identity.role not in Role.get_system_roles():
        if call.identity.role != Role.admin and call.identity.user != request.user:
            raise errors.bad_request.InvalidUserId(
                "cannot generate token for another user"
            )
        if call.identity.company != request.company:
            raise errors.bad_request.InvalidId(
                "cannot generate token in another company"
            )

    call.result.data_model = AuthBLL.get_token_for_user(
        user_id=request.user,
        company_id=request.company,
        expiration_sec=request.expiration_sec,
    )


@endpoint(
    "auth.validate_token",
    request_data_model=ValidateTokenRequest,
    response_data_model=ValidateResponse,
)
def validate_token_endpoint(call: APICall, _, __):
    """Validate a token and return identity if valid. INTERNAL."""
    try:
        # if invalid, decoding will fail
        token = Token.from_encoded_token(call.data_model.token)
        call.result.data_model = ValidateResponse(
            valid=not is_token_revoked(token),
            user=token.identity.user,
            company=token.identity.company,
        )
    except Exception as e:
        call.result.data_model = ValidateResponse(valid=False, msg=e.args[0])


@endpoint(
    "auth.create_user",
    request_data_model=CreateUserRequest,
    response_data_model=CreateUserResponse,
)
def create_user(call: APICall, _, request: CreateUserRequest):
    """Create a user from. INTERNAL."""
    if (
        call.identity.role not in Role.get_system_roles()
        and request.company != call.identity.company
    ):
        raise errors.bad_request.InvalidId("cannot create user in another company")

    user_id = AuthBLL.create_user(request=request, call=call)
    call.result.data_model = CreateUserResponse(id=user_id)


@endpoint("auth.create_credentials", response_data_model=CreateCredentialsResponse)
def create_credentials(call: APICall, _, request: CreateCredentialsRequest):
    if _is_protected_user(call.identity.user):
        raise errors.bad_request.InvalidUserId("protected identity")

    credentials = AuthBLL.create_credentials(
        user_id=call.identity.user,
        company_id=call.identity.company,
        role=call.identity.role,
        label=request.label,
    )
    call.result.data_model = CreateCredentialsResponse(credentials=credentials)


@endpoint("auth.edit_credentials")
def edit_credentials(call: APICall, company_id: str, request: EditCredentialsRequest):
    identity = call.identity
    access_key = request.access_key

    updated = User.objects(
        id=identity.user,
        company=company_id,
        credentials__match={"key": access_key},
    ).update_one(set__credentials__S__label=request.label)
    if not updated:
        raise errors.bad_request.InvalidAccessKey(
            "invalid user or invalid access key",
            user=identity.user,
            access_key=access_key,
            company=company_id,
        )

    call.result.data = {"updated": updated}


@endpoint(
    "auth.revoke_credentials",
    request_data_model=RevokeCredentialsRequest,
    response_data_model=RevokeCredentialsResponse,
)
def revoke_credentials(call: APICall, _, __):
    identity = call.identity
    access_key = call.data_model.access_key

    if _is_protected_user(call.identity.user):
        raise errors.bad_request.InvalidUserId("protected identity")

    with translate_errors_context():
        query = dict(
            id=identity.user, company=identity.company, credentials__key=access_key
        )
        updated = User.objects(**query).update_one(pull__credentials__key=access_key)
        if not updated:
            raise errors.bad_request.InvalidUser(
                "invalid user or invalid access key", **query
            )

        call.result.data_model = RevokeCredentialsResponse(revoked=updated)


@endpoint("auth.get_credentials", response_data_model=GetCredentialsResponse)
def get_credentials(call: APICall, _, __):
    identity = call.identity

    with translate_errors_context():
        query = dict(id=identity.user, company=identity.company)
        user = User.objects(**query).first()
        if not user:
            raise errors.bad_request.InvalidUserId(**query)

        # we return ONLY the key IDs, never the secrets (want a secret? create new credentials)
        call.result.data_model = GetCredentialsResponse(
            credentials=[
                CredentialsResponse(
                    access_key=c.key,
                    last_used=c.last_used,
                    label=c.label,
                    last_used_from=c.last_used_from,
                    created=c.created,
                )
                for c in user.credentials
            ]
        )


@endpoint(
    "auth.edit_user", request_data_model=EditUserReq, response_data_model=UpdateResponse
)
def update(call: APICall, company_id: str, _):
    fields = {
        k: v
        for k, v in call.data_model.to_struct().items()
        if k != "user" and v is not None
    }

    with translate_errors_context():
        result = User.objects(company=company_id, id=call.data_model.user).update(
            **fields, full_result=True, upsert=False
        )

        if not result.matched_count:
            raise errors.bad_request.InvalidUserId()

    call.result.data_model = UpdateResponse(
        updated=result.modified_count, fields=fields
    )


@endpoint("auth.fixed_users_mode")
def fixed_users_mode(call: APICall, *_, **__):
    server_errors = {
        name: error
        for name, error in zip(
            ("missed_es_upgrade", "es_connection_error"),
            (info.missed_es_upgrade, info.es_connection_error),
        )
        if error
    }

    data = {
        "enabled": FixedUser.enabled(),
        "guest": {"enabled": FixedUser.guest_enabled()},
        "server_errors": server_errors,
    }
    guest_user = FixedUser.get_guest_user()
    if guest_user:
        data["guest"]["name"] = guest_user.name
        data["guest"]["username"] = guest_user.username
        data["guest"]["password"] = guest_user.password

    call.result.data = data


def _is_protected_user(user_id):
    return user_id.strip("_") in config.get("secure.credentials")
