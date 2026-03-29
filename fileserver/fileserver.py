""" A Simple file server for uploading and downloading files """
import json
import mimetypes
import os
import shutil
import urllib.parse
from argparse import ArgumentParser
from collections import defaultdict
from pathlib import Path
from typing import Optional

from boltons.iterutils import first
from flask import Flask, request, send_from_directory, abort, Response
from flask_compress import Compress
from flask_cors import CORS
from werkzeug.exceptions import NotFound
from werkzeug.security import safe_join

from auth import AuthHandler
from config import config
from utils import get_env_bool

log = config.logger(__file__)
DEFAULT_UPLOAD_FOLDER = "/mnt/fileserver"

app = Flask(__name__)
CORS(app, **config.get("fileserver.cors"))

if get_env_bool("CLEARML_COMPRESS_RESP", default=True):
    Compress(app)

app.config["UPLOAD_FOLDER"] = first(
    (os.environ.get(f"{prefix}_UPLOAD_FOLDER") for prefix in ("CLEARML", "TRAINS")),
    default=DEFAULT_UPLOAD_FOLDER,
)
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = config.get(
    "fileserver.download.cache_timeout_sec", 5 * 60
)
if max_upload_size := config.get("fileserver.upload.max_upload_size_mb", None):
    app.config["MAX_CONTENT_LENGTH"] = max_upload_size * 1024 * 1024

auth_handler = AuthHandler.instance()


@app.route("/", methods=["GET"])
def ping():
    if auth_handler and auth_handler.get_token(request):
        auth_handler.validate(request)

    return "OK", 200


@app.before_request
def before_request():
    if request.content_encoding:
        return f"Content encoding is not supported ({request.content_encoding})", 415


@app.after_request
def after_request(response):
    response.headers["server"] = config.get(
        "fileserver.response.headers.server", "clearml"
    )
    return response


@app.route("/", methods=["POST"])
def upload():
    if auth_handler:
        auth_handler.validate(request)

    results = []
    for filename, file in request.files.items():
        if not filename:
            continue
        file_path = filename.lstrip(os.sep)
        safe_path = safe_join(app.config["UPLOAD_FOLDER"], file_path)
        if safe_path is None:
            raise NotFound()
        target = Path(safe_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        file.save(str(target))
        results.append(file_path)

    log.info(f"Uploaded {len(results)} files")
    return json.dumps(results), 200


@app.route("/<path:path>", methods=["GET"])
def download(path):
    if auth_handler:
        auth_handler.validate(request)

    as_attachment = "download" in request.args

    _, encoding = mimetypes.guess_type(os.path.basename(path))
    mimetype = "application/octet-stream" if encoding == "gzip" else None

    response = send_from_directory(
        app.config["UPLOAD_FOLDER"],
        path,
        as_attachment=as_attachment,
        mimetype=mimetype,
    )
    if config.get("fileserver.download.disable_browser_caching", False):
        headers = response.headers
        headers["Pragma-directive"] = "no-cache"
        headers["Cache-directive"] = "no-cache"
        headers["Cache-control"] = "no-cache"
        headers["Pragma"] = "no-cache"
        headers["Expires"] = "0"

    log.info(f"Downloaded file {str(path)}")
    return response


def _get_full_path(path: str) -> Optional[Path]:
    path_str = safe_join(os.fspath(app.config["UPLOAD_FOLDER"]), os.fspath(path))
    if path_str is None:
        return path_str

    return Path(path_str)


@app.route("/<path:path>", methods=["DELETE"])
def delete(path):
    if auth_handler:
        auth_handler.validate(request)

    full_path = _get_full_path(path)
    if not (full_path and full_path.exists() and full_path.is_file()):
        log.error(f"Error deleting file {str(full_path)}. Not found or not a file")
        abort(Response(f"File {str(path)} not found", 404))

    full_path.unlink()

    log.info(f"Deleted file {str(full_path)}")
    return json.dumps(str(path)), 200


def batch_delete():
    if auth_handler:
        auth_handler.validate(request)

    body = request.get_json(force=True, silent=False)
    if not body:
        abort(Response("Json payload is missing", 400))
    files = body.get("files")
    if not files:
        abort(Response("files are missing", 400))

    deleted = {}
    errors = defaultdict(list)
    log_errors = defaultdict(list)

    def record_error(msg: str, file_, path_):
        errors[msg].append(str(file_))
        log_errors[msg].append(str(path_))

    for file in files:
        path = urllib.parse.unquote_plus(file)
        if not path or not path.strip("/"):
            # empty path may result in deleting all company data. Too dangerous
            record_error("Empty path not allowed", file, path)
            continue

        full_path = _get_full_path(path)

        if not (full_path and full_path.exists()):
            record_error("Not found", file, path)
            continue

        try:
            if full_path.is_file():
                full_path.unlink()
            elif full_path.is_dir():
                shutil.rmtree(full_path)
            else:
                record_error("Not a file or folder", file, path)
                continue
        except OSError as ex:
            record_error(ex.strerror, file, path)
            continue
        except Exception as ex:
            record_error(str(ex).replace(str(full_path), ""), file, path)
            continue

        deleted[file] = str(path)

    for error, paths in log_errors.items():
        log.error(f"{len(paths)} files/folders cannot be deleted due to the {error}")

    log.info(f"Deleted {len(deleted)} files/folders")
    return json.dumps({"deleted": deleted, "errors": errors}), 200


if config.get("fileserver.delete.allow_batch"):
    app.route("/delete_many", methods=["POST"])(batch_delete)


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument(
        "--port", "-p", type=int, default=config.get("fileserver.listen.port"), help="Port (default %(default)d)"
    )
    parser.add_argument(
        "--ip", "-i", type=str, default=config.get("fileserver.listen.ip"), help="Address (default %(default)s)"
    )
    parser.add_argument("--debug", action="store_true", default=config.get("fileserver.debug"))
    parser.add_argument(
        "--upload-folder",
        "-u",
        help=f"Upload folder (default {DEFAULT_UPLOAD_FOLDER})",
    )
    args = parser.parse_args()

    if args.upload_folder is not None:
        app.config["UPLOAD_FOLDER"] = args.upload_folder

    app.run(debug=args.debug, host=args.ip, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
