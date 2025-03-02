from itertools import chain
from operator import attrgetter
from typing import Sequence, Dict

from boltons import iterutils

from apiserver.apierrors import errors
from apiserver.apimodels.tasks import (
    HyperParamKey,
    HyperParamItem,
    ReplaceHyperparams,
    Configuration,
)
from apiserver.bll.task import TaskBLL
from apiserver.bll.task.utils import get_task_for_update, update_task
from apiserver.config_repo import config
from apiserver.database.model.task.task import ParamsItem, Task, ConfigurationItem
from apiserver.service_repo.auth import Identity
from apiserver.utilities.parameter_key_escaper import (
    ParameterKeyEscaper,
    mongoengine_safe,
)

log = config.logger(__file__)
task_bll = TaskBLL()


class HyperParams:
    _properties_section = "properties"

    @classmethod
    def get_params(cls, company_id: str, task_ids: Sequence[str]) -> Dict[str, dict]:
        only = ("id", "hyperparams")
        tasks = task_bll.assert_exists(
            company_id=company_id,
            task_ids=task_ids,
            only=only,
            allow_public=True,
        )

        return {
            task.id: {"hyperparams": cls._get_params_list(items=task.hyperparams)}
            for task in tasks
        }

    @classmethod
    def _get_params_list(
        cls, items: Dict[str, Dict[str, ParamsItem]]
    ) -> Sequence[dict]:
        ret = list(chain.from_iterable(v.values() for v in items.values()))
        return [
            p.to_proper_dict() for p in sorted(ret, key=attrgetter("section", "name"))
        ]

    @classmethod
    def _normalize_params(cls, params: Sequence) -> bool:
        """
        Lower case properties section and return True if it is the only section
        """
        for p in params:
            if p.section.lower() == cls._properties_section:
                p.section = cls._properties_section

        return all(p.section == cls._properties_section for p in params)

    @classmethod
    def delete_params(
        cls,
        company_id: str,
        identity: Identity,
        task_id: str,
        hyperparams: Sequence[HyperParamKey],
        force: bool,
    ) -> int:
        properties_only = cls._normalize_params(hyperparams)
        task = get_task_for_update(
            company_id=company_id,
            task_id=task_id,
            allow_all_statuses=properties_only,
            force=force,
            identity=identity,
        )

        with_param, without_param = iterutils.partition(
            hyperparams, key=lambda p: bool(p.name)
        )
        sections_to_delete = {p.section for p in without_param}
        delete_cmds = {
            f"unset__hyperparams__{ParameterKeyEscaper.escape(section)}": 1
            for section in sections_to_delete
        }

        for item in with_param:
            section = ParameterKeyEscaper.escape(item.section)
            if item.section in sections_to_delete:
                raise errors.bad_request.FieldsConflict(
                    "Cannot delete section field if the whole section was scheduled for deletion"
                )
            name = ParameterKeyEscaper.escape(item.name)
            delete_cmds[f"unset__hyperparams__{section}__{name}"] = 1

        return update_task(
            task,
            user_id=identity.user,
            update_cmds=delete_cmds,
            set_last_update=not properties_only,
        )

    @classmethod
    def edit_params(
        cls,
        company_id: str,
        identity: Identity,
        task_id: str,
        hyperparams: Sequence[HyperParamItem],
        replace_hyperparams: str,
        force: bool,
    ) -> int:
        properties_only = cls._normalize_params(hyperparams)
        task = get_task_for_update(
            company_id=company_id,
            task_id=task_id,
            allow_all_statuses=properties_only,
            force=force,
            identity=identity,
        )

        update_cmds = dict()
        hyperparams = cls._db_dicts_from_list(hyperparams)
        if replace_hyperparams == ReplaceHyperparams.all:
            update_cmds["set__hyperparams"] = hyperparams
        elif replace_hyperparams == ReplaceHyperparams.section:
            for section, value in hyperparams.items():
                update_cmds[f"set__hyperparams__{mongoengine_safe(section)}"] = value
        else:
            for section, section_params in hyperparams.items():
                for name, value in section_params.items():
                    update_cmds[
                        f"set__hyperparams__{section}__{mongoengine_safe(name)}"
                    ] = value

        return update_task(
            task,
            user_id=identity.user,
            update_cmds=update_cmds,
            set_last_update=not properties_only,
        )

    @classmethod
    def _db_dicts_from_list(cls, items: Sequence[HyperParamItem]) -> Dict[str, dict]:
        sections = iterutils.bucketize(items, key=attrgetter("section"))
        return {
            ParameterKeyEscaper.escape(section): {
                ParameterKeyEscaper.escape(param.name): ParamsItem(**param.to_struct())
                for param in params
            }
            for section, params in sections.items()
        }

    @classmethod
    def get_configurations(
        cls, company_id: str, task_ids: Sequence[str], names: Sequence[str]
    ) -> Dict[str, dict]:
        only = ["id"]
        if names:
            only.extend(
                f"configuration.{ParameterKeyEscaper.escape(name)}" for name in names
            )
        else:
            only.append("configuration")
        tasks = task_bll.assert_exists(
            company_id=company_id,
            task_ids=task_ids,
            only=only,
            allow_public=True,
        )

        return {
            task.id: {
                "configuration": [
                    c.to_proper_dict()
                    for c in sorted(task.configuration.values(), key=attrgetter("name"))
                ]
            }
            for task in tasks
        }

    @classmethod
    def get_configuration_names(
        cls, company_id: str, task_ids: Sequence[str], skip_empty: bool
    ) -> Dict[str, list]:
        skip_empty_condition = {"$match": {"items.v.value": {"$nin": [None, ""]}}}
        pipeline = [
            {
                "$match": {
                    "company": {"$in": ["", company_id]},
                    "_id": {"$in": task_ids},
                }
            },
            {"$project": {"items": {"$objectToArray": "$configuration"}}},
            {"$unwind": "$items"},
            *([skip_empty_condition] if skip_empty else []),
            {"$group": {"_id": "$_id", "names": {"$addToSet": "$items.k"}}},
        ]

        tasks = Task.aggregate(pipeline)

        return {
            task["_id"]: {
                "names": sorted(
                    ParameterKeyEscaper.unescape(name) for name in task["names"]
                )
            }
            for task in tasks
        }

    @classmethod
    def edit_configuration(
        cls,
        company_id: str,
        identity: Identity,
        task_id: str,
        configuration: Sequence[Configuration],
        replace_configuration: bool,
        force: bool,
    ) -> int:
        task = get_task_for_update(
            company_id=company_id, task_id=task_id, force=force, identity=identity
        )

        update_cmds = dict()
        configuration = {
            ParameterKeyEscaper.escape(c.name): ConfigurationItem(**c.to_struct())
            for c in configuration
        }
        if replace_configuration:
            update_cmds["set__configuration"] = configuration
        else:
            for name, value in configuration.items():
                update_cmds[f"set__configuration__{mongoengine_safe(name)}"] = value

        return update_task(task, user_id=identity.user, update_cmds=update_cmds)

    @classmethod
    def delete_configuration(
        cls,
        company_id: str,
        identity: Identity,
        task_id: str,
        configuration: Sequence[str],
        force: bool,
    ) -> int:
        task = get_task_for_update(
            company_id=company_id, task_id=task_id, force=force, identity=identity
        )

        delete_cmds = {
            f"unset__configuration__{ParameterKeyEscaper.escape(name)}": 1
            for name in set(configuration)
        }

        return update_task(task, user_id=identity.user, update_cmds=delete_cmds)
