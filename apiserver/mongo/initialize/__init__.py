from datetime import datetime
from pathlib import Path
from typing import Sequence, Union

from apiserver.config_repo import config
from apiserver.config.info import get_default_company
from apiserver.database.model import User
from apiserver.database.model.auth import Role, User as AuthUser
from apiserver.service_repo.auth.fixed_user import FixedUser
from .migration import _apply_migrations, check_mongo_empty, get_last_server_version
from .pre_populate import PrePopulate
from .user import ensure_fixed_user, _ensure_auth_user
from .util import _ensure_company, _ensure_default_queue, _ensure_uuid

log = config.logger(__package__)


def _pre_populate(company_id: str, zip_file: str):
    if not zip_file or not Path(zip_file).is_file():
        msg = f"Invalid pre-populate zip file: {zip_file}"
        if config.get("apiserver.pre_populate.fail_on_error", False):
            log.error(msg)
            raise ValueError(msg)
        else:
            log.warning(msg)
    else:
        log.info(f"Pre-populating using {zip_file}")

        PrePopulate.import_from_zip(
            zip_file,
            artifacts_path=config.get("apiserver.pre_populate.artifacts_path", None),
        )


def _resolve_zip_files(zip_files: Union[Sequence[str], str]) -> Sequence[str]:
    if isinstance(zip_files, str):
        zip_files = [zip_files]
    for p in map(Path, zip_files):
        if p.is_file():
            yield p
        if p.is_dir():
            yield from p.glob("*.zip")
        log.warning(f"Invalid pre-populate entry {str(p)}, skipping")


def pre_populate_data():
    for zip_file in _resolve_zip_files(config.get("apiserver.pre_populate.zip_files")):
        _pre_populate(company_id=get_default_company(), zip_file=zip_file)

    PrePopulate.update_featured_projects_order()


def init_mongo_data():
    try:
        _apply_migrations(log)

        _ensure_uuid()

        company_id = _ensure_company(get_default_company(), "clearml", log)

        _ensure_default_queue(company_id)

        fixed_mode = FixedUser.enabled()

        internal_user_emails = set()
        for user, credentials in config.get("secure.credentials", {}).items():
            email = f"{user}@example.com"
            user_data = {
                "name": user,
                "role": credentials.role,
                "email": email,
                "key": credentials.user_key,
                "secret": credentials.user_secret,
                "autocreated": True,
            }
            internal_user_emails.add(email.lower())
            revoke = fixed_mode and credentials.get("revoke_in_fixed_mode", False)
            user_id = _ensure_auth_user(
                user_data, company_id, log=log, revoke=revoke, internal_user=True
            )
            if credentials.role == Role.user:
                backend_user = User.objects(id=user_id).first()
                if not backend_user:
                    User(
                        id=user_id,
                        company=company_id,
                        name=credentials.display_name,
                        created=datetime.utcnow(),
                    ).save()

        fixed_users = None
        if fixed_mode:
            log.info("Fixed users mode is enabled")
            FixedUser.validate()
            fixed_users = FixedUser.from_config()
            internal_user_emails.update(user.email for user in fixed_users)

        if internal_user_emails and config.get(
            f"apiserver.auth.delete_missing_autocreated_users", True
        ):
            for user in AuthUser.objects(
                company=company_id, autocreated=True, email__nin=internal_user_emails
            ):
                log.info(
                    f"Removing user that is no longer in configuration: {user['id']}\t{user['email']}\t{user['name']}"
                )
                user.delete()

        if fixed_users:
            log.info("Adding fixed users")

            if FixedUser.guest_enabled():
                _ensure_company(FixedUser.get_guest_user().company, "guests", log)

            for user in fixed_users:
                try:
                    ensure_fixed_user(user, log=log)
                except Exception as ex:
                    log.error(f"Failed creating fixed user {user.name}: {ex}")

    except Exception as ex:
        log.exception(f"Failed initializing mongodb: {str(ex)}")
