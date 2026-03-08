from datetime import datetime

from apiserver import database
from apiserver.apierrors import errors
from apiserver.apimodels.auth import (
    GetTokenResponse,
    CreateUserRequest,
    Credentials as CredModel,
)
from apiserver.apimodels.users import CreateRequest as Users_CreateRequest
from apiserver.bll.user import UserBLL
from apiserver.config_repo import config
from apiserver.config.info import get_version, get_build_number
from apiserver.database.errors import translate_errors_context
from apiserver.database.model.auth import User, Role, Credentials
from apiserver.database.model.company import Company
from apiserver.service_repo import APICall, ServiceRepo
from apiserver.service_repo.auth import Identity, Token, get_client_id, get_secret_key

log = config.logger("AuthBLL")


class AuthBLL:
    @staticmethod
    def get_token_for_user(
        user_id: str,
        company_id: str = None,
        expiration_sec: int = None,
        entities: dict = None,
    ) -> GetTokenResponse:

        with translate_errors_context():
            query = dict(id=user_id)

            if company_id:
                query.update(company=company_id)

            user = User.objects(**query).first()
            if not user:
                raise errors.bad_request.InvalidUserId(**query)

            company_id = company_id or user.company
            company = Company.objects(id=company_id).only("id", "name").first()
            if not company:
                raise errors.bad_request.InvalidId(
                    "invalid company associated with user", company=company
                )

            identity = Identity(
                user=user_id,
                company=company_id,
                role=user.role,
                user_name=user.name,
                company_name=company.name,
            )

            token = Token.create_encoded_token(
                identity=identity,
                entities=entities,
                expiration_sec=expiration_sec,
                api_version=str(ServiceRepo.max_endpoint_version()),
                server_version=str(get_version()),
                server_build=str(get_build_number()),
                feature_set="basic",
            )

            return GetTokenResponse(token=token)

    @staticmethod
    def create_user(request: CreateUserRequest, call: APICall = None) -> str:
        """
        Create a new user in both the auth database and the backend database
        :param request: New user details
        :param call: API call that triggered this call. If not None, new backend user creation
        will be performed using a new call in the same transaction.
        :return: The new user's ID
        """
        with translate_errors_context():
            if not Company.objects(id=request.company).only("id"):
                raise errors.bad_request.InvalidId(company=request.company)

            user = User(
                id=database.utils.id(),
                name=request.name,
                company=request.company,
                role=request.role or Role.user,
                email=request.email,
                created=datetime.utcnow(),
            )

            user.save()

            users_create_request = Users_CreateRequest(
                id=user.id,
                name=request.name,
                company=request.company,
                family_name=request.family_name,
                given_name=request.given_name,
                avatar=request.avatar,
            )

            try:
                UserBLL.create(users_create_request)
            except Exception as ex:
                user.delete()
                raise errors.server_error.GeneralError(
                    "failed adding new user", ex=str(ex)
                )

            return user.id

    @staticmethod
    def delete_user(
        identity: Identity, user_id: str, company_id: str = None, call: APICall = None
    ):
        """
        Delete an existing user from both the auth database and the backend database
        :param identity: Calling user identity
        :param user_id: ID of user to delete
        :param company_id: Company of user to delete
        :param call: API call that triggered this call. If not None, backend user deletion
        will be performed using a new call in the same transaction.
        """
        if user_id == identity.user:
            raise errors.bad_request.FieldsValueError(
                "cannot delete yourself", user=user_id
            )

        if not company_id:
            company_id = identity.company

        if (
            identity.role not in Role.get_system_roles()
            and company_id != identity.company
        ):
            raise errors.bad_request.FieldsNotAllowedForRole(
                "must be empty or your own company", role=identity.role, field="company"
            )

        with translate_errors_context():
            query = dict(id=user_id, company=company_id)
            res = User.objects(**query).delete()
            if not res:
                raise errors.bad_request.InvalidUserId(**query)
        try:
            UserBLL.delete(user_id)
        except Exception as ex:
            log.error(f"Exception calling users.delete: {str(ex)}")

    @classmethod
    def create_credentials(
        cls, user_id: str, company_id: str, role: str = None, label: str = None,
    ) -> CredModel:

        with translate_errors_context():
            query = dict(id=user_id, company=company_id)
            user = User.objects(**query).first()
            if not user:
                raise errors.bad_request.InvalidUserId(**query)

            cred = CredModel(
                access_key=get_client_id(), secret_key=get_secret_key(), label=label
            )
            user.credentials.append(
                Credentials(
                    key=cred.access_key,
                    secret=cred.secret_key,
                    label=label,
                    created=datetime.utcnow(),
                )
            )
            user.save()

            return cred
