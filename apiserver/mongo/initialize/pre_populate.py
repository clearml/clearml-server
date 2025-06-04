import hashlib
import importlib
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from io import BytesIO
from itertools import chain
from operator import attrgetter
from os.path import splitext
from pathlib import Path
from typing import (
    Optional,
    Any,
    Type,
    Set,
    Dict,
    Sequence,
    Tuple,
    BinaryIO,
    Union,
    Mapping,
    IO,
    Callable,
    Iterable,
)
from urllib.parse import unquote, urlparse
from uuid import uuid4, UUID, uuid5
from zipfile import ZipFile, ZIP_BZIP2

import attr
import mongoengine
from boltons.iterutils import chunked_iter, first
from furl import furl
from mongoengine import Q, Document

from apiserver.bll.event import EventBLL
from apiserver.bll.event.event_common import EventType
from apiserver.bll.project import project_ids_with_children
from apiserver.bll.task.artifacts import get_artifact_id
from apiserver.bll.task.param_utils import (
    split_param_name,
    hyperparams_default_section,
    hyperparams_legacy_type,
)
from apiserver.config_repo import config
from apiserver.config.info import get_default_company
from apiserver.database.model import EntityVisibility, User
from apiserver.database.model.auth import Role, User as AuthUser
from apiserver.database.model.model import Model
from apiserver.database.model.project import Project
from apiserver.database.model.task.task import (
    Task,
    ArtifactModes,
    TaskStatus,
    TaskModelTypes,
    TaskModelNames,
)
from apiserver.database.utils import get_options
from apiserver.service_repo.auth import Identity
from apiserver.utilities import json
from apiserver.utilities.dicts import nested_get, nested_set, nested_delete
from apiserver.utilities.parameter_key_escaper import ParameterKeyEscaper

replace_s3_scheme = os.getenv("CLEARML_REPLACE_S3_SCHEME")


def _print(msg: str):
    time = datetime.now().isoformat(sep=" ", timespec="seconds")
    print(f"{time} {msg}")


UrlTranslation = Tuple[str, str]


class PrePopulate:
    module_name_prefix = "apiserver."
    event_bll = EventBLL()
    events_file_suffix = "_events"
    export_tag_prefix = "Exported:"
    export_tag = f"{export_tag_prefix} %Y-%m-%d %H:%M:%S"
    metadata_filename = "metadata.json"
    users_filename = "users.json"
    zip_args = dict(mode="w", compression=ZIP_BZIP2)
    artifacts_ext = ".artifacts"
    img_source_regex = re.compile(
        r"['\"]source['\"]:\s?['\"](https?://(?:localhost:8081|files.*?)/.*?)['\"]",
        flags=re.IGNORECASE,
    )
    _name_guid_ns = UUID("bda3acc1-e612-506c-bade-80071b6cf039")
    _example_id_prefix = "e-"
    task_cls: Type[Task]
    project_cls: Type[Project]
    model_cls: Type[Model]
    user_cls: Type[User]
    auth_user_cls: Type[AuthUser]

    @attr.s(auto_attribs=True)
    class ParentPrefix:
        prefix: str
        path: Sequence[str]

    # noinspection PyTypeChecker
    @classmethod
    def _init_entity_types(cls):
        if not hasattr(cls, "task_cls"):
            cls.task_cls = cls._get_entity_type("database.model.task.task.Task")
        if not hasattr(cls, "model_cls"):
            cls.model_cls = cls._get_entity_type("database.model.model.Model")
        if not hasattr(cls, "project_cls"):
            cls.project_cls = cls._get_entity_type("database.model.project.Project")
        if not hasattr(cls, "user_cls"):
            cls.user_cls = cls._get_entity_type("database.model.User")
        if not hasattr(cls, "auth_user_cls"):
            cls.auth_user_cls = cls._get_entity_type("database.model.auth.User")

    class JsonLinesWriter:
        def __init__(self, file: BinaryIO):
            self.file = file
            self.empty = True

        def __enter__(self):
            self._write("[")
            return self

        def __exit__(self, exc_type, exc_value, exc_traceback):
            self._write("\n]")

        def _write(self, data: str):
            self.file.write(data.encode("utf-8"))

        def write(self, line: str):
            if not self.empty:
                self._write(",")
            self._write("\n" + line)
            self.empty = False

    @staticmethod
    def _get_last_update_time(entity) -> datetime:
        return getattr(entity, "last_update", None) or getattr(entity, "created")

    @classmethod
    def _check_for_update(
        cls, map_file: Path, entities: dict, metadata_hash: str
    ) -> Tuple[bool, Sequence[str]]:
        if not map_file.is_file():
            return True, []

        files = []
        try:
            map_data = json.loads(map_file.read_text())
            files = map_data.get("files", [])
            for file in files:
                if not Path(file).is_file():
                    return True, files

            new_times = {
                item.id: cls._get_last_update_time(item).replace(tzinfo=timezone.utc)
                for item in chain.from_iterable(entities.values())
            }
            old_times = map_data.get("entities", {})

            if set(new_times.keys()) != set(old_times.keys()):
                return True, files

            for id_, new_timestamp in new_times.items():
                if new_timestamp != old_times[id_]:
                    return True, files

            if metadata_hash != map_data.get("metadata_hash", ""):
                return True, files

        except Exception as ex:
            _print("Error reading map file. " + str(ex))
            return True, files

        return False, files

    @classmethod
    def _write_update_file(
        cls,
        map_file: Path,
        entities: dict,
        created_files: Sequence[str],
        metadata_hash: str,
    ):
        map_file.write_text(
            json.dumps(
                dict(
                    files=created_files,
                    entities={
                        entity.id: cls._get_last_update_time(entity)
                        for entity in chain.from_iterable(entities.values())
                    },
                    metadata_hash=metadata_hash,
                )
            )
        )

    @staticmethod
    def _filter_artifacts(artifacts: Sequence[str]) -> Sequence[str]:
        def is_fileserver_link(a: str) -> bool:
            a = a.lower()
            if a.startswith("https://files."):
                return True
            if a.startswith("http"):
                parsed = urlparse(a)
                if parsed.scheme in {"http", "https"} and parsed.netloc.endswith(
                    "8081"
                ):
                    return True
            return False

        fileserver_links = [a for a in artifacts if is_fileserver_link(a)]
        _print(
            f"Found {len(fileserver_links)} files on the fileserver from {len(artifacts)} total"
        )

        return fileserver_links

    @classmethod
    def export_to_zip(
        cls,
        filename: str,
        experiments: Sequence[str] = None,
        projects: Sequence[str] = None,
        company: str = None,
        artifacts_path: str = None,
        task_statuses: Sequence[str] = None,
        tag_exported_entities: bool = False,
        metadata: Mapping[str, Any] = None,
        export_events: bool = True,
        export_users: bool = False,
        project_split: bool = False,
        url_trans: UrlTranslation = None,
    ) -> Sequence[str]:
        cls._init_entity_types()

        if task_statuses and not set(task_statuses).issubset(get_options(TaskStatus)):
            raise ValueError("Invalid task statuses")

        def export_to_zip_core(file_base_name: Path, projects_: Sequence[str]):
            entities = cls._resolve_entities(
                experiments=experiments, projects=projects_, task_statuses=task_statuses
            )

            hash_ = hashlib.md5()
            if metadata:
                meta_str = json.dumps(metadata)
                hash_.update(meta_str.encode())
                metadata_hash = hash_.hexdigest()
            else:
                meta_str, metadata_hash = "", ""

            map_file = file_base_name.with_suffix(".map")
            updated, old_files = cls._check_for_update(
                map_file, entities=entities, metadata_hash=metadata_hash
            )
            if not updated:
                _print(f"There are no updates from the last export")
                return old_files

            for old in old_files:
                old_path = Path(old)
                if old_path.is_file():
                    old_path.unlink()

            temp_file = file_base_name.with_suffix(file_base_name.suffix + "$")
            try:
                with ZipFile(temp_file, **cls.zip_args) as zfile:
                    if metadata:
                        zfile.writestr(cls.metadata_filename, meta_str)
                    if export_users:
                        cls._export_users(zfile)
                    artifacts = cls._export(
                        zfile,
                        entities=entities,
                        hash_=hash_,
                        tag_entities=tag_exported_entities,
                        export_events=export_events,
                        cleanup_users=not export_users,
                        url_trans=url_trans,
                    )
            except:
                temp_file.unlink(missing_ok=True)
                raise

            file_with_hash = file_base_name.with_stem(
                f"{file_base_name.stem}_{hash_.hexdigest()}"
            )
            temp_file.replace(file_with_hash)
            files = [str(file_with_hash)]

            artifacts = cls._filter_artifacts(artifacts)
            if artifacts and artifacts_path and os.path.isdir(artifacts_path):
                artifacts_file = file_with_hash.with_suffix(cls.artifacts_ext)
                with ZipFile(artifacts_file, **cls.zip_args) as zfile:
                    cls._export_artifacts(zfile, artifacts, artifacts_path)
                files.append(str(artifacts_file))

            cls._write_update_file(
                map_file,
                entities=entities,
                created_files=files,
                metadata_hash=metadata_hash,
            )

            if files:
                _print("Created files:\n" + "\n".join(file for file in files))

            return files

        filename = Path(filename)
        if not (experiments or projects):
            query = dict(parent=None)
            if company:
                query["company"] = company
            projects = list(cls.project_cls.objects(**query).scalar("id"))
            # projects.append(None)

        if projects and project_split:
            created_files = list(
                chain.from_iterable(
                    export_to_zip_core(
                        file_base_name=filename.with_stem(f"{filename.stem}_{pid}"),
                        projects_=[pid],
                    )
                    for pid in projects
                )
            )
        else:
            created_files = export_to_zip_core(
                file_base_name=filename, projects_=projects
            )

        return created_files

    @classmethod
    def import_from_zip(
        cls,
        filename: str,
        artifacts_path: str,
        company_id: Optional[str] = None,
        user_id: str = "",
        user_name: str = "",
    ):
        cls._init_entity_types()

        metadata = None

        with ZipFile(filename) as zfile:
            try:
                with zfile.open(cls.metadata_filename) as f:
                    metadata = json.loads(f.read())

                    meta_public = metadata.get("public")
                    if company_id is None and meta_public is not None:
                        company_id = "" if meta_public else get_default_company()

                    if not user_id:
                        meta_user_id = metadata.get("user_id", "")
                        meta_user_name = metadata.get("user_name", "")
                        user_id, user_name = meta_user_id, meta_user_name
            except Exception as ex:
                _print(
                    f"Error getting metadata from {cls.metadata_filename}: {str(ex)}"
                )

            # Make sure we won't end up with an invalid company ID
            if company_id is None:
                company_id = ""

            user_mapping = cls._import_users(zfile, company_id)

            if not user_id:
                user_id, user_name = "__allegroai__", "Allegro.ai"

            existing_user = cls.user_cls.objects(id=user_id).only("id").first()
            if not existing_user:
                cls.user_cls(id=user_id, name=user_name, company=company_id).save()

            cls._import(
                zfile,
                company_id=company_id,
                user_id=user_id,
                metadata=metadata,
                user_mapping=user_mapping,
            )

        if artifacts_path and os.path.isdir(artifacts_path):
            artifacts_file = Path(filename).with_suffix(cls.artifacts_ext)
            if artifacts_file.is_file():
                _print(f"Unzipping artifacts into {artifacts_path}")
                with ZipFile(artifacts_file) as zfile:
                    zfile.extractall(artifacts_path)

    @classmethod
    def upgrade_zip(cls, filename) -> Sequence:
        hash_ = hashlib.md5()
        task_file = cls._get_base_filename(cls.task_cls) + ".json"
        temp_file = Path("temp.zip")
        file = Path(filename)
        with ZipFile(file) as reader, ZipFile(temp_file, **cls.zip_args) as writer:
            for file_info in reader.filelist:
                if file_info.orig_filename == task_file:
                    with reader.open(file_info) as f:
                        content = cls._upgrade_tasks(f)
                else:
                    content = reader.read(file_info)
                writer.writestr(file_info, content)
                hash_.update(content)

        base_file_name, _, old_hash = file.stem.rpartition("_")
        new_hash = hash_.hexdigest()
        if old_hash == new_hash:
            _print(f"The file {filename} was not updated")
            temp_file.unlink()
            return []

        new_file = file.with_name(f"{base_file_name}_{new_hash}{file.suffix}")
        temp_file.replace(new_file)
        upadated = [str(new_file)]

        artifacts_file = file.with_suffix(cls.artifacts_ext)
        if artifacts_file.is_file():
            new_artifacts = new_file.with_suffix(cls.artifacts_ext)
            artifacts_file.replace(new_artifacts)
            upadated.append(str(new_artifacts))

        _print(f"File {str(file)} replaced with {str(new_file)}")
        file.unlink()

        return upadated

    @classmethod
    def _upgrade_tasks(cls, f: IO[bytes]) -> bytes:
        """
        Build content array that contains upgraded tasks from the passed file
        For each task the old execution.parameters and model.design are
        converted to the new structure.
        The fix is done on Task objects (not the dictionary) so that
        the fields are serialized back in the same order as they were in the original file
        """
        with BytesIO() as temp:
            with cls.JsonLinesWriter(temp) as w:
                for line in cls.json_lines(f):
                    task_data = cls.task_cls.from_json(line).to_proper_dict()
                    cls._upgrade_task_data(task_data)
                    new_task = cls.task_cls(**task_data)
                    w.write(new_task.to_json())
            return temp.getvalue()

    @classmethod
    def update_featured_projects_order(cls):
        order = config.get("services.projects.featured.order", [])
        if not order:
            return

        public_default = config.get("services.projects.featured.public_default", 9999)

        def get_index(p: Project):
            for index, entry in enumerate(order):
                if (
                    entry.get("id", None) == p.id
                    or entry.get("name", None) == p.name
                    or ("name_regex" in entry and re.match(entry["name_regex"], p.name))
                ):
                    return index
            return public_default

        for project in cls.project_cls.get_many_public(projection=["id", "name"]):
            featured_index = get_index(project)
            cls.project_cls.objects(id=project.id).update(featured=featured_index)

    @classmethod
    def _resolve_entity_type(
        cls, entity_type: Type[mongoengine.Document], ids: Optional[Sequence[str]]
    ) -> Sequence[Any]:
        ids = set(ids)
        items = list(entity_type.objects(id__in=list(ids)))
        resolved = {i.id for i in items}
        missing = ids - resolved
        if not missing:
            return items

        resolved_by_name = defaultdict(list)
        for entity in entity_type.objects(name__in=list(missing)):
            resolved_by_name[entity.name].append(entity)

        not_found = missing - set(resolved_by_name)
        if not_found:
            _print(f"ERROR: no match for {', '.join(not_found)}")
            exit(1)

        duplicates = [k for k, v in resolved_by_name.items() if len(v) > 1]
        if duplicates:
            _print(f"ERROR: more than one match for {', '.join(duplicates)}")
            exit(1)

        def get_new_items(input_: Iterable) -> list:
            return [item for item in input_ if item.id not in resolved]

        def get_projects_with_children(projects: list) -> list:
            project_ids = set(item.id for item in projects)
            ids_with_children = project_ids_with_children(list(project_ids))
            if project_ids == set(ids_with_children):
                return projects

            return get_new_items(entity_type.objects(id__in=ids_with_children))

        new_items = get_new_items(chain(*resolved_by_name.values()))
        if not new_items:
            return items

        if entity_type == cls.project_cls:
            new_items = get_projects_with_children(new_items)

        return items + new_items

    @classmethod
    def _check_projects_hierarchy(cls, projects: Set[Project]):
        """
        For the projects that are exported not from the root
        fix their parents tree to exclude the not exported parents
        """
        if not projects:
            return

        project_ids = {p.id for p in projects}
        orphans = [p for p in projects if p.parent and p.parent not in project_ids]
        if not orphans:
            return

        prefixes = [
            cls.ParentPrefix(
                prefix=f"{project.name.rpartition('/')[0]}/", path=project.path
            )
            for project in orphans
        ]
        prefixes.sort(key=lambda p: len(p.path), reverse=True)
        for project in projects:
            prefix = first(
                pref for pref in prefixes if project.path[: len(pref.path)] == pref.path
            )
            if not prefix:
                continue
            project.path = project.path[len(prefix.path) :]
            if not project.path:
                project.parent = None
            project.name = project.name.removeprefix(prefix.prefix)

        # _print(
        #     f"ERROR: the following projects are exported without their parents: {orphans}"
        # )
        # exit(1)

    @classmethod
    def _resolve_entities(
        cls,
        experiments: Sequence[str] = None,
        projects: Sequence[str] = None,
        task_statuses: Sequence[str] = None,
    ) -> Dict[Type[mongoengine.Document], Set[mongoengine.Document]]:
        # noinspection PyTypeChecker
        entities: Dict[Any] = defaultdict(set)

        if projects:
            _print("Reading projects...")
            root = None in projects
            projects = [p for p in projects if p]
            if projects:
                projects = project_ids_with_children(projects)
                entities[cls.project_cls].update(
                    cls._resolve_entity_type(cls.project_cls, projects)
                )
            _print("--> Reading project experiments...")
            p_ids = list(set(p.id for p in entities[cls.project_cls]))
            if root:
                p_ids.append(None)
            query = Q(
                project__in=p_ids,
                system_tags__nin=[EntityVisibility.archived.value],
            )
            if task_statuses:
                query &= Q(status__in=list(set(task_statuses)))
            objs = cls.task_cls.objects(query)
            entities[cls.task_cls].update(
                o for o in objs if o.id not in (experiments or [])
            )

        if experiments:
            _print("Reading experiments...")
            entities[cls.task_cls].update(
                cls._resolve_entity_type(cls.task_cls, experiments)
            )
            _print("--> Reading experiments projects...")
            objs = cls.project_cls.objects(
                id__in=list(
                    set(filter(None, (p.project for p in entities[cls.task_cls])))
                )
            )
            project_ids = {p.id for p in entities[cls.project_cls]}
            entities[cls.project_cls].update(o for o in objs if o.id not in project_ids)

        cls._check_projects_hierarchy(entities[cls.project_cls])

        task_models = chain.from_iterable(
            models
            for task in entities[cls.task_cls]
            if task.models
            for models in (task.models.input, task.models.output)
            if models
        )
        model_ids = {tm.model for tm in task_models}
        if model_ids:
            _print("Reading models...")
            entities[cls.model_cls] = set(cls.model_cls.objects(id__in=list(model_ids)))

        # noinspection PyTypeChecker
        return entities

    @classmethod
    def _filter_out_export_tags(cls, tags: Sequence[str]) -> Sequence[str]:
        if not tags:
            return tags
        return [tag for tag in tags if not tag.startswith(cls.export_tag_prefix)]

    @classmethod
    def _cleanup_model(cls, model: Model):
        model.company = ""
        model.tags = cls._filter_out_export_tags(model.tags)

    @classmethod
    def _cleanup_task(cls, task: Task):
        task.comment = "Auto generated by Allegro.ai"
        task.status_message = ""
        task.status_reason = ""
        task.company = ""
        task.tags = cls._filter_out_export_tags(task.tags)
        if task.output:
            task.output.destination = None

    @classmethod
    def _cleanup_project(cls, project: Project):
        project.company = ""
        project.tags = cls._filter_out_export_tags(project.tags)

    @classmethod
    def _cleanup_auth_user(cls, user: AuthUser):
        user.company = ""
        for cred in user.credentials:
            if getattr(cred, "company", None):
                cred["company"] = ""
        return user

    @classmethod
    def _cleanup_be_user(cls, user: User):
        user.company = ""
        user.preferences = None
        return user

    @classmethod
    def _cleanup_entity(cls, entity_cls, entity, cleanup_users):
        if cleanup_users:
            entity.user = ""
        if entity_cls == cls.task_cls:
            cls._cleanup_task(entity)
        elif entity_cls == cls.model_cls:
            cls._cleanup_model(entity)
        elif entity_cls == cls.project_cls:
            cls._cleanup_project(entity)

    @classmethod
    def _add_tag(cls, items: Sequence[Union[Project, Task, Model]], tag: str):
        try:
            for item in items:
                item.update(upsert=False, tags=sorted(item.tags + [tag]))
        except AttributeError:
            pass

    @staticmethod
    def _translate_url(url_: str, url_trans: UrlTranslation) -> str:
        if not (url_ and url_trans):
            return url_

        source, target = url_trans
        if not url_.startswith(source):
            return url_

        return target + url_[len(source):]

    @classmethod
    def _export_task_events(
        cls,
        task: Task,
        base_filename: str,
        writer: ZipFile,
        hash_,
        url_trans: UrlTranslation,
    ) -> Sequence[str]:
        artifacts = []
        filename = f"{base_filename}_{task.id}{cls.events_file_suffix}.json"
        _print(f"Writing task events into {writer.filename}:{filename}")

        with BytesIO() as f:
            with cls.JsonLinesWriter(f) as w:
                scroll_id = None
                events_count = 0
                while True:
                    res = cls.event_bll.get_task_events(
                        company_id=task.company,
                        task_id=task.id,
                        event_type=EventType.all,
                        scroll_id=scroll_id,
                        size=10_000,
                    )
                    if not res.events:
                        break
                    scroll_id = res.next_scroll_id
                    for event in res.events:
                        event_type = event.get("type")
                        if event_type == EventType.metrics_image.value:
                            url = cls._get_fixed_url(event.get("url"))
                            if url:
                                artifacts.append(url)
                                event["url"] = cls._translate_url(url, url_trans)
                        elif event_type == EventType.metrics_plot.value:
                            plot_str: str = event.get("plot_str", "")
                            if plot_str:
                                for match in cls.img_source_regex.findall(plot_str):
                                    url = cls._get_fixed_url(match)
                                    artifacts.append(url)
                                    new_url = cls._translate_url(url, url_trans)
                                    if match != new_url:
                                        plot_str = plot_str.replace(match, new_url)
                                event["plot_str"] = plot_str
                        w.write(json.dumps(event))
                        events_count += 1
                    _print(f"Got {events_count} events for task {task.id}")
            _print(f"Writing {events_count} events for task {task.id}")
            data = f.getvalue()
            hash_.update(data)
            writer.writestr(filename, data)

        return artifacts

    @staticmethod
    def _get_fixed_url(url: Optional[str]) -> Optional[str]:
        if not (replace_s3_scheme and url and url.lower().startswith("s3://")):
            return url

        try:
            fixed = furl(url)
            fixed.scheme = "https"
            fixed.host += ".s3.amazonaws.com"
            return fixed.url
        except Exception as ex:
            _print(f"Failed processing link {url}. " + str(ex))
            return url

    @classmethod
    def _export_entity_related_data(
        cls,
        entity_cls,
        entity,
        base_filename: str,
        writer: ZipFile,
        hash_,
        url_trans: UrlTranslation,
    ):
        if entity_cls == cls.task_cls:
            return [
                *cls._get_task_output_artifacts(entity, url_trans),
                *cls._export_task_events(
                    entity, base_filename, writer, hash_, url_trans
                ),
            ]

        if entity_cls == cls.model_cls:
            url = cls._get_fixed_url(entity.uri)
            entity.uri = cls._translate_url(url, url_trans)
            return [url] if url else []

        return []

    @classmethod
    def _get_task_output_artifacts(cls, task: Task, url_trans: UrlTranslation) -> Sequence[str]:
        if not task.execution.artifacts:
            return []

        artifact_urls = []
        for a in task.execution.artifacts.values():
            if a.mode == ArtifactModes.output:
                url = cls._get_fixed_url(a.uri)
                a.uri = cls._translate_url(url, url_trans)
                if url and a.mode == ArtifactModes.output:
                    artifact_urls.append(url)

        return artifact_urls

    @classmethod
    def _export_artifacts(
        cls, writer: ZipFile, artifacts: Sequence[str], artifacts_path: str
    ):
        unique_paths = set(unquote(str(furl(artifact).path)) for artifact in artifacts)
        _print(f"Writing {len(unique_paths)} artifacts into {writer.filename}")
        for path in unique_paths:
            path = path.lstrip("/")
            full_path = os.path.join(artifacts_path, path)
            if os.path.isfile(full_path):
                writer.write(full_path, path)
            else:
                _print(f"Artifact {full_path} not found")

    @classmethod
    def _export_users(cls, writer: ZipFile):
        auth_users = {
            user.id: cls._cleanup_auth_user(user)
            for user in cls.auth_user_cls.objects(role__in=(Role.admin, Role.user))
        }
        if not auth_users:
            return

        be_users = {
            user.id: cls._cleanup_be_user(user)
            for user in cls.user_cls.objects(id__in=list(auth_users))
        }
        if not be_users:
            return

        auth_users = {uid: data for uid, data in auth_users.items() if uid in be_users}
        _print(f"Writing {len(auth_users)} users into {writer.filename}")
        data = {}
        for field, users in (("auth", auth_users), ("backend", be_users)):
            with BytesIO() as f:
                with cls.JsonLinesWriter(f) as w:
                    for user in users.values():
                        w.write(user.to_json())
                data[field] = f.getvalue()

        def get_field_bytes(k: str, v: bytes) -> bytes:
            return f'"{k}": '.encode("utf-8") + v

        data_str = b",\n".join(get_field_bytes(k, v) for k, v in data.items())
        writer.writestr(cls.users_filename, b"{\n" + data_str + b"\n}")

    @classmethod
    def _get_base_filename(cls, cls_: type):
        name = f"{cls_.__module__}.{cls_.__name__}"
        if cls.module_name_prefix and name.startswith(cls.module_name_prefix):
            name = name[len(cls.module_name_prefix) :]
        return name

    @classmethod
    def _export(
        cls,
        writer: ZipFile,
        entities: dict,
        hash_,
        tag_entities: bool = False,
        export_events: bool = True,
        cleanup_users: bool = True,
        url_trans: UrlTranslation = None,
    ) -> Sequence[str]:
        """
        Export the requested experiments, projects and models and return the list of artifact files
        Always do the export on sorted items since the order of items influence hash
        The projects should be sorted by name so that on import the hierarchy is correctly restored from top to bottom
        """
        artifacts = []
        now = datetime.now(timezone.utc)
        for cls_ in sorted(entities, key=attrgetter("__name__")):
            items = sorted(entities[cls_], key=attrgetter("name", "id"))
            if not items:
                continue
            base_filename = cls._get_base_filename(cls_)
            if export_events:
                for item in items:
                    artifacts.extend(
                        cls._export_entity_related_data(
                            cls_, item, base_filename, writer, hash_, url_trans
                        )
                    )
            filename = base_filename + ".json"
            _print(f"Writing {len(items)} items into {writer.filename}:{filename}")
            with BytesIO() as f:
                with cls.JsonLinesWriter(f) as w:
                    for item in items:
                        cls._cleanup_entity(cls_, item, cleanup_users=cleanup_users)
                        w.write(item.to_json())
                data = f.getvalue()
                hash_.update(data)
                writer.writestr(filename, data)

            if tag_entities:
                cls._add_tag(items, now.strftime(cls.export_tag))

        return artifacts

    @staticmethod
    def json_lines(file: IO[bytes]):
        for line in file:
            clean = (
                line.decode("utf-8")
                .rstrip("\r\n")
                .strip()
                .lstrip("[")
                .rstrip(",]")
                .strip()
            )
            if not clean:
                continue
            yield clean

    @staticmethod
    def _new_id(_):
        return str(uuid4()).replace("-", "")

    @classmethod
    def _hash_id(cls, name: str):
        return str(uuid5(cls._name_guid_ns, name)).replace("-", "")

    @classmethod
    def _example_id(cls, orig_id: str):
        if not orig_id or orig_id.startswith(cls._example_id_prefix):
            return orig_id

        return cls._example_id_prefix + orig_id

    @classmethod
    def _private_id(cls, orig_id: str):
        if not orig_id or not orig_id.startswith(cls._example_id_prefix):
            return orig_id

        return orig_id[len(cls._example_id_prefix) :]

    @classmethod
    def _generate_new_ids(
        cls,
        reader: ZipFile,
        entity_files: Sequence,
        metadata: Mapping[str, Any],
    ) -> Mapping[str, str]:
        if not metadata or not any(
            metadata.get(key) for key in ("new_ids", "example_ids", "private_ids")
        ):
            return {}

        ids = {}
        for entity_file in entity_files:
            with reader.open(entity_file) as f:
                is_project = splitext(entity_file.orig_filename)[0].endswith(".Project")
                if metadata.get("new_ids"):
                    id_func = cls._new_id
                elif metadata.get("example_ids"):
                    id_func = cls._example_id if not is_project else cls._hash_id
                elif metadata.get("private_ids"):
                    id_func = cls._private_id if not is_project else cls._new_id
                for item in cls.json_lines(f):
                    doc = json.loads(item)
                    orig_id = doc.get("_id")
                    if orig_id:
                        ids[orig_id] = (
                            id_func(orig_id)
                            if id_func != cls._hash_id
                            else id_func(doc.get("name"))
                        )
        return ids

    @classmethod
    def _import_users(cls, reader: ZipFile, company_id: str = "") -> dict:
        """
        Import users to db and return the mapping of old user ids to the new ones
        If no users were in the users file then the mapping was empty
        If the user in the file has the same email as one of the existing ones then this user is skipped
        and its id is mapped to the existing user with the same email
        If the user with the same id exists in backend or auth db then its creation is skipped
        """
        users_file = first(
            fi for fi in reader.filelist if fi.orig_filename == cls.users_filename
        )
        if not users_file:
            return {}

        existing_user_ids = set(cls.user_cls.objects().scalar("id")) | set(
            cls.auth_user_cls.objects().scalar("id")
        )
        existing_user_emails = {u.email: u.id for u in cls.auth_user_cls.objects()}
        user_id_mappings = {}

        with reader.open(users_file) as f:
            data = json.loads(f.read())

        auth_users = {u["_id"]: u for u in data["auth"]}
        be_users = {u["_id"]: u for u in data["backend"]}
        for uid, user in auth_users.items():
            email = user.get("email")
            existing_user_id = existing_user_emails.get(email)
            if existing_user_id:
                user_id_mappings[uid] = existing_user_id
                continue

            user_id_mappings[uid] = uid
            if uid in existing_user_ids:
                continue

            credentials = user.get("credentials", [])
            for c in credentials:
                if c.get("company") == "":
                    c["company"] = company_id

            if hasattr(cls.auth_user_cls, "sec_groups"):
                user_role = user.get("role", Role.user)
                if user_role == Role.user:
                    user["sec_groups"] = ["30795571-a470-4717-a80d-e8705fc776bf"]
                else:
                    user["sec_groups"] = [
                        "c14a3cc6-1144-4896-8ea6-fb186ee19896",
                        "30795571-a470-4717-a80d-e8705fc776bf",
                        "30795571a4704717a80de8705897ytuyg",
                    ]

            auth_user = cls.auth_user_cls.from_json(json.dumps(user), created=True)
            auth_user.company = company_id
            auth_user.save()
            be_user = cls.user_cls.from_json(json.dumps(be_users[uid]), created=True)
            be_user.company = company_id
            be_user.save()

        return user_id_mappings

    @classmethod
    def _import(
        cls,
        reader: ZipFile,
        company_id: str = "",
        user_id: str = None,
        metadata: Mapping[str, Any] = None,
        sort_tasks_by_last_updated: bool = True,
        user_mapping: Mapping[str, str] = None,
    ):
        """
        Import entities and events from the zip file
        Start from entities since event import will require the tasks already in DB
        """
        event_file_ending = cls.events_file_suffix + ".json"
        entity_files = [
            fi
            for fi in reader.filelist
            if not fi.orig_filename.endswith(event_file_ending)
            and fi.orig_filename not in (cls.metadata_filename, cls.users_filename)
        ]
        metadata = metadata or {}
        old_to_new_ids = cls._generate_new_ids(reader, entity_files, metadata)
        tasks = []
        for entity_file in entity_files:
            with reader.open(entity_file) as f:
                full_name = splitext(entity_file.orig_filename)[0]
                _print(f"Reading {reader.filename}:{full_name}...")
                res = cls._import_entity(
                    f,
                    full_name=full_name,
                    company_id=company_id,
                    user_id=user_id,
                    metadata=metadata,
                    old_to_new_ids=old_to_new_ids,
                    user_mapping=user_mapping,
                )
                if res:
                    tasks = res

        if sort_tasks_by_last_updated:
            tasks = sorted(tasks, key=attrgetter("last_update"))

        new_to_old_ids = {v: k for k, v in old_to_new_ids.items()}
        for task in tasks:
            old_task_id = new_to_old_ids.get(task.id, task.id)
            events_file = first(
                fi
                for fi in reader.filelist
                if fi.orig_filename.endswith(old_task_id + event_file_ending)
            )
            if not events_file:
                continue
            with reader.open(events_file) as f:
                full_name = splitext(events_file.orig_filename)[0]
                _print(f"Reading {reader.filename}:{full_name}...")
                cls._import_events(f, company_id, task.user, task.id)

    @classmethod
    def _get_entity_type(cls, full_name) -> Type[mongoengine.Document]:
        module_name, _, class_name = full_name.rpartition(".")
        if cls.module_name_prefix and not module_name.startswith(
            cls.module_name_prefix
        ):
            module_name = cls.module_name_prefix + module_name
        module = importlib.import_module(module_name)
        return getattr(module, class_name)

    @classmethod
    def _upgrade_project_data(cls, project_data: dict) -> dict:
        cls._remove_incompatible_fields(cls.project_cls, project_data)

        if not project_data.get("basename"):
            name: str = project_data["name"]
            _, _, basename = name.rpartition("/")
            project_data["basename"] = basename

        return project_data

    @classmethod
    def _upgrade_model_data(cls, model_data: dict) -> dict:
        cls._remove_incompatible_fields(cls.model_cls, model_data)

        metadata_key = "metadata"
        metadata = model_data.get(metadata_key)
        if isinstance(metadata, list):
            metadata = {
                ParameterKeyEscaper.escape(item["key"]): item
                for item in metadata
                if isinstance(item, dict) and "key" in item
            }
            model_data[metadata_key] = metadata
        return model_data

    @staticmethod
    def _remove_incompatible_fields(cls_: Type[Document], data: dict):
        for field in ("company_origin",):
            if field not in cls_._db_field_map:
                data.pop(field, None)

    @classmethod
    def _upgrade_task_data(cls, task_data: dict) -> dict:
        """
        Migrate from execution/parameters and model_desc to hyperparams and configuration fiields
        Upgrade artifacts list to dict
        Migrate from execution.model and output.model to the new models field
        Move docker_cmd contents into the container field
        :param task_data: Upgraded in place
        :return: The upgraded task data
        """
        cls._remove_incompatible_fields(cls.task_cls, task_data)

        for old_param_field, new_param_field, default_section in (
            ("execution.parameters", "hyperparams", hyperparams_default_section),
            ("execution.model_desc", "configuration", None),
        ):
            legacy_path = old_param_field.split(".")
            legacy = nested_get(task_data, legacy_path)
            if legacy:
                for full_name, value in legacy.items():
                    section, name = split_param_name(full_name, default_section)
                    new_path = list(filter(None, (new_param_field, section, name)))
                    if not nested_get(task_data, new_path):
                        new_param = dict(
                            name=name, type=hyperparams_legacy_type, value=str(value)
                        )
                        if section is not None:
                            new_param["section"] = section
                        nested_set(task_data, path=new_path, value=new_param)
            nested_delete(task_data, legacy_path)

        artifacts_path = ("execution", "artifacts")
        artifacts = nested_get(task_data, artifacts_path)
        if isinstance(artifacts, list):
            nested_set(
                task_data,
                path=artifacts_path,
                value={get_artifact_id(a): a for a in artifacts},
            )

        models = task_data.get("models", {})
        now = datetime.now(timezone.utc)
        for old_field, type_ in (
            ("execution.model", TaskModelTypes.input),
            ("output.model", TaskModelTypes.output),
        ):
            old_path = old_field.split(".")
            old_model = nested_get(task_data, old_path)
            new_models = [
                m for m in models.get(type_, []) if m.get("model") is not None
            ]
            name = TaskModelNames[type_]
            if old_model and not any(
                m
                for m in new_models
                if m.get("model") == old_model or m.get("name") == name
            ):
                model_item = {"model": old_model, "name": name, "updated": now}
                if type_ == TaskModelTypes.input:
                    new_models = [model_item, *new_models]
                else:
                    new_models = [*new_models, model_item]
            models[type_] = new_models
            nested_delete(task_data, old_path)
        task_data["models"] = models

        docker_cmd_path = ("execution", "docker_cmd")
        docker_cmd = nested_get(task_data, docker_cmd_path)
        if docker_cmd and not task_data.get("container"):
            image, _, arguments = docker_cmd.partition(" ")
            task_data["container"] = {"image": image, "arguments": arguments}
        nested_delete(task_data, docker_cmd_path)

        return task_data

    @classmethod
    def _import_entity(
        cls,
        f: IO[bytes],
        full_name: str,
        company_id: str,
        user_id: str,
        metadata: Mapping[str, Any],
        old_to_new_ids: Mapping[str, str] = None,
        user_mapping: Mapping[str, str] = None,
    ) -> Optional[Sequence[Task]]:
        user_mapping = user_mapping or {}
        cls_ = cls._get_entity_type(full_name)
        _print(f"Writing {cls_.__name__.lower()}s into database")
        tasks = []
        override_project_count = 0
        data_upgrade_funcs: Mapping[Type, Callable] = {
            cls.task_cls: cls._upgrade_task_data,
            cls.model_cls: cls._upgrade_model_data,
            cls.project_cls: cls._upgrade_project_data,
        }
        for item in cls.json_lines(f):
            if old_to_new_ids:
                for old_id, new_id in old_to_new_ids.items():
                    # replace ids only when they are standalone strings
                    # otherwise artifacts uris that contain old ids may get damaged
                    item = item.replace(f'"{old_id}"', f'"{new_id}"')
            upgrade_func = data_upgrade_funcs.get(cls_)
            if upgrade_func:
                item = json.dumps(upgrade_func(json.loads(item)))

            doc = cls_.from_json(item, created=True)
            if hasattr(doc, "user"):
                doc.user = user_mapping.get(doc.user, user_id) if doc.user else user_id
            if hasattr(doc, "company"):
                doc.company = company_id
            if isinstance(doc, cls.project_cls):
                override_project_name = metadata.get("project_name", None)
                if override_project_name:
                    if override_project_count:
                        override_project_name = (
                            f"{override_project_name} {override_project_count + 1}"
                        )
                    override_project_count += 1
                    doc.name = override_project_name

                doc.logo_url = metadata.get("logo_url", None)
                doc.logo_blob = metadata.get("logo_blob", None)

                cls_.objects(company=company_id, name=doc.name, id__ne=doc.id).update(
                    set__name=f"{doc.name}_{datetime.now(timezone.utc).strftime('%Y-%m-%d_%H-%M-%S')}"
                )

            doc.save()

            if isinstance(doc, cls.task_cls):
                tasks.append(doc)
                cls.event_bll.delete_task_events(
                    company_id, doc.id, wait_for_delete=True
                )

        if tasks:
            return tasks

    @classmethod
    def _import_events(cls, f: IO[bytes], company_id: str, user_id: str, task_id: str):
        _print(f"Writing events for task {task_id} into database")
        for events_chunk in chunked_iter(cls.json_lines(f), 1000):
            events = [json.loads(item) for item in events_chunk]
            for ev in events:
                ev["task"] = task_id
                ev["company_id"] = company_id
                ev["allow_locked"] = True
            cls.event_bll.add_events(
                company_id=company_id,
                identity=Identity(user_id, company=company_id, role=Role.admin),
                events=events,
                worker="",
            )
