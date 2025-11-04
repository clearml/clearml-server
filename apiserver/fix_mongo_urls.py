import logging
from argparse import (
    ArgumentDefaultsHelpFormatter,
    ArgumentParser,
    ArgumentTypeError,
)

from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.database import Database


logging.getLogger().setLevel(logging.INFO)


def fix_mongo_urls(
    mongo_host: str,
    host_source: str,
    host_target: str,
    user: str = None,
    password: str = None
):
    logging.info(f"Connecting to Mongo on {mongo_host}")
    client = MongoClient(
        host=mongo_host,
        username=user,
        password=password,
    )
    backend_db: Database = client.backend

    def get_updated_uri(uri: str):
        if not uri or not uri.startswith(host_source):
            return
        relative_url = uri[len(host_source):]
        return f"{host_target.rstrip('/')}/{relative_url.lstrip('/')}"

    host_source = host_source
    host_target = host_target
    model_collection: Collection = backend_db.get_collection("model")
    if model_collection is not None:
        logging.info("Updating model uris")
        models_count = model_collection.count_documents({})
        updated_models = 0
        for model in model_collection.find(
            {"uri": {"$regex": "^{}".format(host_source)}}, projection=["uri"]
        ):
            updated_uri = get_updated_uri(model.get("uri"))
            if updated_uri:
                result = model_collection.update_one(
                    {"_id": model["_id"]}, {"$set": {"uri": updated_uri}}
                )
                updated_models += result.modified_count

        logging.info(f"Updated {updated_models} models from {models_count}")

    task_collection: Collection = backend_db.get_collection("task")
    if task_collection is not None:
        logging.info("Updating task uris")
        tasks_count = task_collection.count_documents({})
        updated_tasks = 0
        for task in task_collection.find(
            {"execution.artifacts": {"$exists": 1, "$ne": {}}},
            projection=["execution.artifacts"],
        ):
            artifacts = task.get("execution", {}).get("artifacts")
            if not artifacts:
                continue

            uri_updated = False
            for artifact in artifacts.values():
                updated_uri = get_updated_uri(artifact.get("uri"))
                if updated_uri:
                    artifact["uri"] = updated_uri
                    uri_updated = True

            if uri_updated:
                result = task_collection.update_one(
                    {"_id": task["_id"]}, {"$set": {"execution.artifacts": artifacts}}
                )
                updated_tasks += result.modified_count

        logging.info(f"Updated {updated_tasks} tasks from {tasks_count}")


def normalise_host(host):
    if not host.endswith("/"):
        return host
    return host[:-1]


def main():
    def valid_url_prefix(url: str):
        if "://" not in url:
            raise ArgumentTypeError("url schema is missing")
        return url

    parser = ArgumentParser(
        description=__doc__, formatter_class=ArgumentDefaultsHelpFormatter
    )
    parser.add_argument(
        "--mongo-host",
        "-mh",
        type=str,
        default="mongodb://mongo:27017",
        help="Mongo server host. The default is mongodb://mongo:27017",
    )
    parser.add_argument(
        "--mongo-user", "-mu", help="Mongo db user", type=str, default=None,
    )
    parser.add_argument(
        "--mongo-password", "-mp", help="Mongo db password", type=str, default=None,
    )
    parser.add_argument(
        "--host-source",
        "-hs",
        type=valid_url_prefix,
        required=True,
        help="Source host for the files uploaded to the fileserver (in the form http://<host>:<port>)",
    )
    parser.add_argument(
        "--host-target",
        "-ht",
        type=valid_url_prefix,
        required=True,
        help="Target host for the files uploaded to the fileserver (in the form http://<host>:<port>)",
    )
    args = parser.parse_args()

    fix_mongo_urls(
        mongo_host=args.mongo_host,
        host_source=args.host_source,
        host_target=args.host_target,
        user=args.mongo_user,
        password=args.mongo_password,
    )
    logging.info("Completed successfully")


if __name__ == "__main__":
    main()
