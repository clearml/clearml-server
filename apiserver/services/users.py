from copy import deepcopy
from typing import Tuple

import dpath
from boltons.iterutils import remap
from mongoengine import Q

from apiserver.apierrors import errors
from apiserver.apimodels.base import UpdateResponse
from apiserver.apimodels.users import (
    CreateRequest,
    SetPreferencesRequest,
    UserRequest,
)
from apiserver.bll.project import ProjectBLL
from apiserver.bll.user import UserBLL
from apiserver.config_repo import config
from apiserver.database.errors import translate_errors_context
from apiserver.database.model.auth import Role, User as AuthUser
from apiserver.database.model.company import Company
from apiserver.database.model.user import User
from apiserver.database.utils import parse_from_call
from apiserver.service_repo import APICall, endpoint
from apiserver.utilities.json import loads, dumps

log = config.logger(__file__)
project_bll = ProjectBLL()


def get_user(call, company_id, user_id, only=None):
    """
    Get user object by the user's ID
    :param call: API call
    :param user_id: user ID
    :param only: fields to include in projection, by default all
    :return: User object
    """
    if call.identity.role in (Role.system,):
        # allow system users to get info for all users
        query = dict(id=user_id)
    else:
        query = dict(id=user_id, company=company_id)

    with translate_errors_context("retrieving user"):
        user = User.objects(**query)
        if only:
            user = user.only(*only)
        res = user.first()
        if not res:
            raise errors.bad_request.InvalidUserId(**query)

        return res.to_proper_dict()


@endpoint("users.get_by_id")
def get_by_id(call: APICall, company_id, request: UserRequest):
    user_id = request.user
    call.result.data = {"user": get_user(call, company_id, user_id)}


@endpoint("users.get_all_ex")
def get_all_ex(call: APICall, company_id, _):
    with translate_errors_context("retrieving users"):
        res = User.get_many_with_join(company=company_id, query_dict=call.data)

        call.result.data = {"users": res}


@endpoint("users.get_all_ex", min_version="2.8")
def get_all_ex2_8(call: APICall, company_id, _):
    with translate_errors_context("retrieving users"):
        data = call.data
        active_in_projects = call.data.get("active_in_projects", None)
        if active_in_projects is not None:
            active_users = project_bll.get_active_users(
                company_id, active_in_projects, call.data.get("id")
            )
            active_users.discard(None)
            if not active_users:
                call.result.data = {"users": []}
                return
            data = data.copy()
            data["id"] = list(active_users)

        res = User.get_many_with_join(company=company_id, query_dict=data)

        call.result.data = {"users": res}


@endpoint("users.get_all")
def get_all(call: APICall, company_id, _):
    with translate_errors_context("retrieving users"):
        res = User.get_many(
            company=company_id, parameters=call.data, query_dict=call.data
        )

        call.result.data = {"users": res}


@endpoint("users.get_current_user")
def get_current_user(call: APICall, company_id, _):
    user_id = call.identity.user

    projection = (
        {"company.name"}.union(User.get_fields()).difference(User.get_exclude_fields())
    )
    res = User.get_many_with_join(
        query=Q(id=user_id),
        company=company_id,
        override_projection=projection,
    )

    if not res:
        raise errors.bad_request.InvalidUser("failed loading user")

    user = res[0]
    user["role"] = call.identity.role

    resp = dict(
        user=user, getting_started=config.get("apiserver.getting_started_info", None)
    )
    resp["settings"] = {
        "max_download_items": int(
            config.get("services.organization.download.max_download_items", 1000)
        )
    }
    call.result.data = resp


create_fields = {
    "name": None,
    "family_name": None,
    "given_name": None,
    "avatar": None,
    "company": Company,
    "preferences": dict,
}


@endpoint("users.create", request_data_model=CreateRequest)
def create(call: APICall):
    UserBLL.create(call.data_model)


@endpoint("users.delete")
def delete(_: APICall, __, request: UserRequest):
    UserBLL.delete(request.user)


def update_user(user_id, company_id, data: dict) -> Tuple[int, dict]:
    """
    Update user.
    :param user_id: user ID to update
    :param company_id: ID of company user belongs to
    :param data: mapping to update user by
    :return: (updated fields count, updated fields) pair
    """
    update_fields = {
        k: v for k, v in create_fields.items() if k in User.user_set_allowed()
    }
    auth_user_update_fields = ("name",)
    partial_update_dict = parse_from_call(data, update_fields, User.get_fields())
    with translate_errors_context("updating user"):
        ret = User.safe_update(company_id, user_id, partial_update_dict)
        auth_update = {
            k: v for k, v in partial_update_dict.items() if k in auth_user_update_fields
        }
        if auth_update:
            AuthUser.objects(id=user_id).update(**auth_update)

    return ret


@endpoint("users.update", response_data_model=UpdateResponse)
def update(call, company_id, request: UserRequest):
    user_id = request.user
    update_count, updated_fields = update_user(user_id, company_id, call.data)
    call.result.data_model = UpdateResponse(updated=update_count, fields=updated_fields)


def get_user_preferences(call: APICall, company_id):
    user_id = call.identity.user
    preferences = get_user(call, company_id, user_id, only=["preferences"]).get(
        "preferences"
    )
    if preferences and isinstance(preferences, str):
        preferences = loads(preferences)
    return preferences or {}


@endpoint("users.get_preferences")
def get_preferences(call: APICall, company_id, _):
    return {"preferences": get_user_preferences(call, company_id)}


@endpoint("users.set_preferences", request_data_model=SetPreferencesRequest)
def set_preferences(call: APICall, company_id, request: SetPreferencesRequest):
    changes = request.preferences

    def invalid_key(path, key, __):
        """
        key cannot start with "$"
        only top level keys may contain "."
        """
        if not isinstance(key, str):
            return True
        elif key.startswith("$") or (path and "." in key):
            raise errors.bad_request.FieldsValueError(
                f"Key {key} is invalid. Keys cannot start with '$' or contain '.'."
            )
        return True

    remap(changes, visit=invalid_key)

    base_preferences = get_user_preferences(call, company_id)
    new_preferences = deepcopy(base_preferences)
    for key, value in changes.items():
        try:
            dpath.new(new_preferences, key, value, separator=".")
        except Exception:
            log.exception(
                'invalid preferences update for user "{}": key=`%s`, value=`%s`',
                key,
                value,
            )
            raise errors.bad_request.InvalidPreferencesUpdate(key=key, value=value)

    if new_preferences == base_preferences:
        updated, fields = 0, {}
    else:
        with translate_errors_context("updating user preferences"):
            updated = User.objects(id=call.identity.user, company=company_id).update(
                upsert=False, preferences=dumps(new_preferences)
            )

    fields = {}
    if updated and request.return_updated:
        fields["preferences"] = new_preferences

    return {
        "updated": updated,
        "fields": fields,
    }
