from datetime import datetime
from logging import Logger

import attr

from apiserver.config.info import get_version
from apiserver.database.model.auth import Role
from apiserver.database.model.auth import User as AuthUser, Credentials
from apiserver.database.model.user import User
from apiserver.service_repo.auth.fixed_user import FixedUser


def _ensure_user_credentials(
    user: AuthUser,
    key: str,
    secret: str,
    log: Logger,
    revoke: bool = False,
    internal_user: bool = False,
) -> None:
    if revoke:
        log.info(f"Revoking credentials for existing user {user.id} ({user.name})")
        user.credentials = []
        user.save()
        return

    if not (key and secret):
        if internal_user:
            log.info(f"Resetting credentials for existing user {user.id} ({user.name})")
            user.credentials = []
            user.save()
        return

    new_credentials = Credentials(key=key, secret=secret)
    if internal_user:
        log.info(f"Setting credentials for existing user {user.id} ({user.name})")
        user.credentials = [new_credentials]
        user.save()
        return

    if user.credentials is None:
        user.credentials = []
    if not any((cred.key, cred.secret) == (key, secret) for cred in user.credentials):
        log.info(f"Adding credentials for existing user {user.id} ({user.name})")
        user.credentials.append(new_credentials)
        user.save()


def _ensure_auth_user(
    user_data: dict,
    company_id: str,
    log: Logger,
    revoke: bool = False,
    internal_user: bool = False,
) -> str:
    user_id = user_data.get("id", f"__{user_data['name']}__")
    role = user_data["role"]
    email = user_data["email"]
    autocreated = user_data.get("autocreated", False)
    key, secret = user_data.get("key"), user_data.get("secret")

    user: AuthUser = AuthUser.objects(id=user_id).first()
    if user:
        _ensure_user_credentials(
            user=user,
            key=key,
            secret=secret,
            log=log,
            revoke=revoke,
            internal_user=internal_user,
        )
        if user.role != role or user.email != email or user.autocreated != autocreated:
            user.email = email
            user.role = role
            user.autocreated = autocreated
            user.save()

        return user.id

    credentials = (
        [Credentials(key=key, secret=secret)] if not revoke and key and secret else []
    )
    log.info(f"Creating user: {user_data['name']}")

    user = AuthUser(
        id=user_id,
        name=user_data["name"],
        company=company_id,
        role=role,
        email=email,
        created=datetime.utcnow(),
        credentials=credentials,
        autocreated=autocreated,
    )

    user.save()

    return user.id


def ensure_fixed_user(user: FixedUser, log: Logger, emails: set):
    # noinspection PyTypeChecker
    data = attr.asdict(user)
    data["id"] = user.user_id
    email = f"{user.user_id}@example.com"
    data["email"] = email
    data["role"] = Role.guest if user.is_guest else Role.user
    data["autocreated"] = True

    _ensure_auth_user(user_data=data, company_id=user.company, log=log)

    db_user = User.objects(company=user.company, id=user.user_id).first()
    given_name, _, family_name = user.name.partition(" ")
    if db_user:
        # noinspection PyBroadException
        try:
            log.info(f"Updating user name: {user.name}")
            db_user.update(
                name=user.name, given_name=given_name, family_name=family_name
            )
        except Exception:
            pass
    else:
        User(
            id=user.user_id,
            company=user.company,
            name=user.name,
            given_name=given_name,
            family_name=family_name,
            created=datetime.utcnow(),
            created_in_version=get_version(),
        ).save()

    emails.add(email)
