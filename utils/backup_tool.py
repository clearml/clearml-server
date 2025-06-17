#########################################################################################
# A self-installing script for ClearML local server instance backups.
# This tool provides functionality to create and restore ClearML snapshots.
# It supports backing up Elasticsearch, MongoDB, Redis, and fileserver data.
# It also allows scheduling backups using cron jobs.
# Usage:
#   - Display help and available commands: `uv run backup_tool.py --help`
#   - Create a snapshot: `uv run backup_tool.py create-snapshot --help`
#   - Restore a snapshot: `uv run backup_tool.py restore-snapshot --help`
#   - Setup cron job for automatic backups: `uv run backup_tool.py setup-schedule --help`
#   - Clear existing cron jobs: `uv run backup_tool.py clear-schedule --help`
#########################################################################################


# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "docker",
#     "loguru",
#     "python-crontab",
#     "typer",
#     "pyyaml",
# ]
# ///


import os
import pwd
import subprocess
import sys
import re
import time
import json
from pathlib import Path
from datetime import datetime, timedelta

import yaml
import docker
import typer
from crontab import CronTab
from loguru import logger
from pprint import pformat


app = typer.Typer(add_completion=False)


log_format = (
        "<green>{time:YYYY-MM-DD at HH:mm:ss}</green> | "
        "<level>{level: <7}</level> | "
        "<bold><magenta>{extra[name]}</magenta></bold> - "
        "{message}"
    )


class ClearMLBackupManager:
    def __init__(self, docker_compose_file: str):
        self.timestamp = datetime.now().strftime("%Y-%m-%d-%H%M")
        self.docker_compose_file = docker_compose_file

        # setup logging
        self.logger = logger
        self.logger.remove(0)
        self.logger.add(
            sys.stdout,
            format=log_format,
            colorize=True
        )
        self.logger = self.logger.bind(name="MAIN")

        # parse docker compose file
        if not os.path.exists(docker_compose_file):
            self.logger.error(f"Docker Compose file not found at {docker_compose_file}.")
            raise FileNotFoundError(f"Docker Compose file not found at {docker_compose_file}.")
        self.compose_dict = yaml.safe_load(open(docker_compose_file, "r"))

        # setup containers
        self.containers = self.setup_containers()
        if self.containers is None:
            self.logger.error("Failed to identify containers. Exiting backup process.")
            raise RuntimeError("Failed to identify containers. Exiting backup process.")
        
    def cleanup_old_backups(self, backup_root: str, keep_last: int = 2) -> int:
        """
        Removes old ClearML snapshot backups, keeping only the most recent `keep_last` snapshots.

        Args:
            backup_root (str): The root directory where backups are stored.
            keep_last (int): Number of most recent snapshots to keep.
        """
        backup_root_path = Path(backup_root)
        if not backup_root_path.exists() or not backup_root_path.is_dir():
            self.logger.error(f"Backup root directory does not exist or is not a directory: {backup_root}")
            return 1

        # Match folders like: clearml_snapshot_2025-06-05-1030
        snapshot_dirs = sorted(
            [p for p in backup_root_path.iterdir() if p.is_dir() and re.match(r"clearml_snapshot_\d{4}-\d{2}-\d{2}-\d{4}", p.name)],
            key=lambda p: p.name,
            reverse=True  # most recent first
        )

        if len(snapshot_dirs) <= keep_last:
            self.logger.info(f"Only {len(snapshot_dirs)} snapshots found. Nothing to clean.")
            return 0

        to_delete = snapshot_dirs[keep_last:]
        for folder in to_delete:
            try:
                self.logger.info(f"Removing old snapshot: {folder}")
                subprocess.run(["rm", "-rf", str(folder)], check=True)
            except Exception as e:
                self.logger.error(f"Failed to delete {folder}: {e}")
                return 1
            
        return 0

    def create_snapshot(self, backup_root: str) -> tuple[int, str]:
        """ Main method to create a ClearML snapshot. It will backup Elasticsearch, MongoDB, Redis and fileserver data."""

        backup_path = os.path.join(backup_root, f"clearml_snapshot_{self.timestamp}")
        os.makedirs(backup_path, exist_ok=True)

        # Route logger to the snapshot directory
        self.logger.add(
            os.path.join(backup_path, "clearml_backup.log"),
            format=log_format
        )
        self.logger.info("Starting ClearML snapshot creation...")

        # Copy Docker Compose file to backup directory
        compose_backup_path = os.path.join(backup_path, "docker-compose.yml")
        response = subprocess.run(["cp", self.docker_compose_file, compose_backup_path], check=True)
        if response.returncode != 0:
            self.logger.error(f"Failed to copy Docker Compose file to {compose_backup_path}.")
            return 1, backup_path
        self.logger.info(f"Copied Docker Compose file to {compose_backup_path}.")
        
        # Copy config directory to backup directory
        config_dir_path = None
        for volume in self.compose_dict["services"]["apiserver"]["volumes"]:
            if "/opt/clearml/config" in volume:
                config_dir_path = volume.split(":")[0]
                break
        response = subprocess.run(["cp", "-r", config_dir_path, os.path.join(backup_path, "config")], check=True)
        if not config_dir_path or not os.path.exists(config_dir_path):
            self.logger.error(f"Config directory not found in Docker Compose file or does not exist: {config_dir_path}")
            return 1, backup_path
        self.logger.info(f"Copied config directory from {config_dir_path} to {os.path.join(backup_path, 'config')}.")
        
        # Backup Elasticsearch
        self.logger = self.logger.bind(name="ELASTICSEARCH")
        status = self.backup_elasticsearch(backup_path)
        if status != 0:
            self.logger.error("Elasticsearch backup failed. Exiting backup process.")
            return status, backup_path

        # Backup MongoDB
        self.logger = self.logger.bind(name="MONGODB")
        self.backup_mongodb(backup_path)
        if status != 0:
            self.logger.error("MongoDB backup failed. Exiting backup process.")
            return status, backup_path

        # Backup Redis
        self.logger = self.logger.bind(name="REDIS")
        status = self.backup_redis(backup_path)
        if status != 0:
            self.logger.error("Redis backup failed. Exiting backup process.")
            return status, backup_path

        # Backup fileserver
        self.logger = self.logger.bind(name="FILESERVER")
        status = self.backup_fileserver(backup_path)
        if status != 0:
            self.logger.error("Fileserver backup failed. Exiting backup process.")
            return status, backup_path

        self.logger = self.logger.bind(name="MAIN")
        self.logger.info("ClearML snapshot created successfully.")
        return 0, backup_path
        
    def restore_snapshot(self, backup_path: str) -> int:
        """ Main method to restore a ClearML snapshot. It will restore Elasticsearch, MongoDB, Redis and fileserver data."""

        if not os.path.exists(backup_path):
            self.logger.error(f"Backup path does not exist: {backup_path}")
            return 1

        self.logger.info("Starting ClearML snapshot restoration...")

        # Restore Elasticsearch
        self.logger = self.logger.bind(name="ELASTICSEARCH")
        status = self.restore_elasticsearch(backup_path)
        if status != 0:
            self.logger.error("Elasticsearch restoration failed. Exiting restore process.")
            return status
        
        # Restore MongoDB
        self.logger = self.logger.bind(name="MONGODB")
        status = self.restore_mongodb(backup_path)
        if status != 0:
            self.logger.error("MongoDB restoration failed. Exiting restore process.")
            return status

        # # Restore Redis
        self.logger = self.logger.bind(name="REDIS")
        status = self.restore_redis(backup_path)
        if status != 0:
            self.logger.error("Redis restoration failed. Exiting restore process.")
            return status

        # # Restore fileserver
        self.logger = self.logger.bind(name="FILESERVER")
        status = self.restore_fileserver(backup_path)
        if status != 0:
            self.logger.error("Fileserver restoration failed. Exiting restore process.")
            return status

        self.logger = self.logger.bind(name="MAIN")
        self.logger.info("ClearML snapshot restored successfully.")
        return 0

    def setup_containers(self) -> dict | None:
        """ Identifies ClearML containers and returns them in a dictionary."""

        containers = {}
        docker_client = docker.from_env()
        for container in docker_client.containers.list():
            if "clearml-elastic" in container.name:
                if "elastic" in containers.keys():
                    self.logger.error(f"Multiple Elasticsearch containers found: {containers['elastic'].id} and {container.id}. Using the first one.")
                containers["elastic"] = container
                self.logger.info(f"Found Elasticsearch container: {container.name} ({container}, {container.image})")
            elif "clearml-mongo" in container.name:
                if "mongo" in containers.keys():
                    self.logger.error(f"Multiple MongoDB containers found: {containers['mongo'].id} and {container.id}. Using the first one.")
                containers["mongo"] = container
                self.logger.info(f"Found MongoDB container: {container.name} ({container}, {container.image})")
            elif "clearml-redis" in container.name:
                if "redis" in containers.keys():
                    self.logger.error(f"Multiple Redis containers found: {containers['redis'].id} and {container.id}. Using the first one.")
                containers["redis"] = container
                self.logger.info(f"Found Redis container: {container.name} ({container}, {container.image})")

        if not "elastic" in containers:
            self.logger.error("No Elasticsearch container.")
            return
        if not "mongo" in containers:
            self.logger.error("No MongoDB container found.")
            return
        if not "redis" in containers:
            self.logger.error("No Redis container found.")
            return

        return containers

    def backup_elasticsearch(self, backup_path: str) -> int:
        """ Backs up Elasticsearch data by creating a snapshot and copying it to the host."""
        if not "path.repo" in self.compose_dict["services"]["elasticsearch"]["environment"]:
            self.logger.error("Elasticsearch path.repo environment variable not found in Docker Compose file.")
            return 1
        
        es_container_backup_dir = self.compose_dict["services"]["elasticsearch"]["environment"]["path.repo"]
        es_local_backup_dir = os.path.join(backup_path, os.path.basename(os.path.normpath(es_container_backup_dir)))
        repo_name = "backup"
        snapshot_name = f"snapshot_{self.timestamp}"

        # Register snapshot repo
        self.logger.info(f"Registering Elasticsearch snapshot repository '{repo_name}' at {es_container_backup_dir}...")
        response = self.containers["elastic"].exec_run(
            f"curl -s -X PUT localhost:9200/_snapshot/{repo_name} "
            f"-H 'Content-Type: application/json' "
            f"-d '{{\"type\": \"fs\", \"settings\": {{\"location\": \"{es_container_backup_dir}\"}}}}'"
        )
        response = response.output.decode()
        response = json.loads(response) if response else {}
        if "error" in response:
            self.logger.error(f"Failed to register Elasticsearch snapshot repository: \n{pformat(response['error'])}")
            return 1
        else:
            self.logger.info(f"Elasticsearch snapshot repository registered: \n{pformat(response)}")
        

        # Trigger snapshot
        self.logger.info(f"Elasticsearch snapshot creation started...")
        response = self.containers["elastic"].exec_run(
            f"curl -s -X PUT localhost:9200/_snapshot/{repo_name}/{snapshot_name}?wait_for_completion=true"
        )
        response = response.output.decode()
        response = json.loads(response) if response else {}
        if "error" in response:
            self.logger.error(f"Failed to create Elasticsearch snapshot: \n{pformat(response['error'])}")
            return 1
        else:
            self.logger.info(f"Elasticsearch snapshot created: \n{pformat(response)}")

        # Copy snapshot data from container
        self.logger.info(f"Copying Elasticsearch snapshot data from container to local directory: {es_local_backup_dir}")
        response = subprocess.run([
            "docker", 
            "cp",
            f"{self.containers['elastic'].id}:{es_container_backup_dir}", 
            backup_path,
            "-q"
        ])
        # check files got copied
        if not os.path.exists(es_local_backup_dir) or not os.listdir(es_local_backup_dir):
            self.logger.error("Elasticsearch backup directory is empty. Backup failed.")
            return 1
        else:
            self.logger.info(f"Elasticsearch snapshot data copied to: {es_local_backup_dir}")  
              
        return 0
    
    def restore_elasticsearch(self, backup_path: str) -> int:
        """ Restores Elasticsearch data from a snapshot by copying it to the container's backup directory."""
        # Copy the snapshot files back into the container's repo path
        es_repo = self.compose_dict["services"]["elasticsearch"]["environment"]["path.repo"]
        es_repo_root = os.path.dirname(es_repo)
        host_snapshot_dir = os.path.join(backup_path, os.path.basename(es_repo))
        self.logger.info(f"Copying Elasticsearch snapshot files from {host_snapshot_dir} to container at {es_repo_root}")
        response = subprocess.run([
            "docker", "cp",
            host_snapshot_dir,
            f"{self.containers['elastic'].id}:{es_repo_root}"
        ], check=True)
        if response.returncode != 0:
            self.logger.error(f"Failed to copy Elasticsearch snapshot files from {host_snapshot_dir} to container.")
            return 1
        else:
            self.logger.info(f"Copied Elasticsearch snapshot into container at {es_repo}")

        # Re-register the repo
        self.logger.info("Re-registering Elasticsearch snapshot repository...")
        repo_name = "backup"
        response = self.containers["elastic"].exec_run(
            f"curl -s -X PUT localhost:9200/_snapshot/{repo_name} "
            f"-H 'Content-Type: application/json' "
            f"-d '{{\"type\":\"fs\",\"settings\":{{\"location\":\"{es_repo}\"}}}}'"
        )
        response = response.output.decode()
        response = json.loads(response) if response else {}
        self.logger.info(f"Elasticsearch snapshot repository re-registration response: \n{pformat(response)}")
        if "error" in response:
            self.logger.error(f"Failed to re-register Elasticsearch snapshot repository: \n{pformat(response['error'])}")
            return 1
        else:
            self.logger.info("Elasticsearch snapshot repository re-registered successfully.")

        # Close any existing indices
        self.logger.info("Closing all Elasticsearch indices to avoid conflicts during restore...")
        indices = self.containers["elastic"].exec_run(
            "curl -s localhost:9200/_cat/indices?h=index"
        ).output.decode().strip().splitlines()
        if indices:
            index_list = ",".join(indices)
            response = self.containers["elastic"].exec_run(
                f"curl -s -X POST localhost:9200/{index_list}/_close"
            )
            response = response.output.decode()
            response = json.loads(response) if response else {}
            self.logger.info(f"Close indices response: \n{pformat(response)}")
            if "error" in response:
                self.logger.error(f"Failed to close Elasticsearch indices: \n{pformat(response['error'])}")
                return 1
            else:
                self.logger.info("Closed all Elasticsearch indices.")
        else:
            self.logger.info("No Elasticsearch indices found to close.")

        # Trigger the restore
        snap_timestamp = backup_path.split("_")[-1]
        snap_name = f"snapshot_{snap_timestamp}"
        self.logger.info(f"Restoring Elasticsearch snapshot: {snap_name} from repository: {repo_name}...")
        response = self.containers["elastic"].exec_run(
            f"curl -s -X POST localhost:9200/_snapshot/{repo_name}/{snap_name}/_restore?wait_for_completion=true "
            f"-H 'Content-Type: application/json' -d '{{\"include_global_state\":true}}'"
        )
        response = response.output.decode()
        response = json.loads(response) if response else {}
        if "error" in response:
            self.logger.error(f"Failed to restore Elasticsearch snapshot: {pformat(response['error'])}")
            return 1
        else:
            self.logger.info(f"Elasticsearch snapshot restored: {pformat(response)}")
        
        self.logger.info("Elasticsearch snapshot restored.")
        return 0
    
    def backup_mongodb(self, backup_path: str) -> int:
        """ Backs up MongoDB data by creating a dump and copying it to the host."""
        mongo_container_backup_dir = "/tmp/mongodump"
        mongo_backup_dir = os.path.join(backup_path, "mongo_backup")

        # clean up old backup directory if exists
        self.logger.info(f"Cleaning up old MongoDB backup directory: {mongo_container_backup_dir}")
        self.containers["mongo"].exec_run(f"rm -rf {mongo_container_backup_dir}")
        # create backup directory on host
        self.logger.info(f"Creating MongoDB backup directory on host: {mongo_container_backup_dir}")
        response = self.containers["mongo"].exec_run(f"mongodump --out {mongo_container_backup_dir}")
        if response.exit_code != 0:
            self.logger.error(f"Failed to create MongoDB dump: {response.output.decode()}")
            return 1
        self.logger.info(f"MongoDB dumped: {response.output.decode()}")
        # copy backup from container to host
        self.logger.info(f"Copying MongoDB backup data from container to local directory: {mongo_backup_dir}")
        response = subprocess.run([
            "docker", 
            "cp",
            f"{self.containers['mongo'].id}:{mongo_container_backup_dir}",
            mongo_backup_dir,
            "-q"
        ])
        # check files got copied
        if not os.path.exists(mongo_backup_dir) or not os.listdir(mongo_backup_dir):
            self.logger.error("MongoDB backup directory is empty. Backup failed.")
            return 1
            
        self.logger.info(f"MongoDB backup data copied to: {mongo_backup_dir}")
        return 0
    
    def restore_mongodb(self, backup_path: str) -> int:
        """ Restores MongoDB data from a snapshot by copying the dump back into the container and restoring it."""
        # Copy dump back into container
        container_target = "/tmp/mongodump_restore"
        host_dump_dir = os.path.join(backup_path, "mongo_backup")
        self.logger.info(f"Copying MongoDB dump from {host_dump_dir} to container at {container_target}")
        response = subprocess.run([
            "docker", "cp",
            host_dump_dir,
            f"{self.containers['mongo'].id}:{container_target}"
        ], check=True)
        if response.returncode != 0:
            self.logger.error(f"Failed to copy MongoDB dump from {host_dump_dir} to container.")
            return 1
        self.logger.info(f"Copied Mongo dump into container at {container_target}")

        # Restore to overwrite existing data
        self.logger.info("Restoring MongoDB data from dump...")
        response = self.containers["mongo"].exec_run(
            f"mongorestore --drop {container_target}",
            user="mongodb"  # same user as backup
        )
        if response.exit_code != 0:
            self.logger.error(f"Failed to restore MongoDB data: {response.output.decode()}")
            return 1
        
        self.logger.info("MongoDB data restored successfully.")
        return 0
    
    def backup_redis(self, backup_path: str) -> int:
        """ Backs up Redis data by triggering a SAVE command and copying the dump.rdb file to the host."""
        redis_local_backup_file = os.path.join(backup_path, "dump.rdb")

        # trigger redis backup
        self.logger.info("Triggering Redis SAVE to create a snapshot...")
        response = self.containers["redis"].exec_run("redis-cli SAVE")
        if not response.output.decode().startswith("OK"):
            self.logger.error(f"Failed to trigger Redis SAVE command: {response.output.decode()}")
            return 1
        self.logger.info(f"Redis SAVE command response: {response.output.decode()}")

        # Copy dump.rdb to host
        self.logger.info(f"Copying Redis dump.rdb from container to local file: {redis_local_backup_file}")
        response = subprocess.run([
            "docker", 
            "cp",
            f"{self.containers['redis'].id}:/data/dump.rdb", 
            redis_local_backup_file,
            "-q"
        ])
        if response.returncode != 0:
            self.logger.error(f"Failed to copy Redis dump.rdb from container to {redis_local_backup_file}.")
            return 1
        
        self.logger.info(f"Redis backup file copied to: {redis_local_backup_file}")
        return 0    
    
    def restore_redis(self, backup_path: str) -> int:
        """ Restores Redis data from a snapshot by copying the dump.rdb file back into the container and restarting it."""
        # Stop Redis to avoid racing writes
        self.containers["redis"].stop()
        self.logger.info("Redis container stopped for restore.")

        # Copy dump.rdb back into container
        host_rdb = os.path.join(backup_path, "dump.rdb")
        response = subprocess.run([
            "docker", "cp",
            host_rdb,
            f"{self.containers['redis'].id}:/data/dump.rdb"
        ], check=True)
        if response.returncode != 0:
            self.logger.error(f"Failed to copy Redis dump.rdb from {host_rdb} to container.")
            return 1
        self.logger.info(f"Copied dump.rdb into Redis container.")

        # Restart Redis
        self.containers["redis"].start()
        self.logger.info("Redis container restarted.")
        return 0

    def backup_fileserver(self, backup_path: str) -> int:
        """ Backs up fileserver data by copying the fileserver path to a backup directory."""
        fileserver_volumes = self.compose_dict["services"]["fileserver"]["volumes"]
        fileserver_path = None
        for volume in fileserver_volumes:
            if "/mnt/fileserver" in volume:
                fileserver_path = volume.split(":")[0]
                self.logger.info(f"Fileserver path: {fileserver_path}")
                break

        # Ensure fileserver path exists
        if not fileserver_path:
            self.logger.error("Fileserver path not found in Docker Compose file.")
            return 1
        if not os.path.exists(fileserver_path):
            self.logger.error(f"Fileserver path does not exist: {fileserver_path}")
            return 1
        else:
            self.logger.info(f"Copying fileserver from {fileserver_path} with rsync...")
        # Copy fileserver data 
        response = subprocess.run([
            "rsync", "-av", "--delete", str(fileserver_path), str(backup_path)
        ], check=True)
        if response.returncode != 0:
            self.logger.error(f"Rsync failed: {response}.")
            return 1
        else:
            self.logger.info(f"Rsync successful.")
        # Check files got copied
        fileserver_backup_dir = os.path.join(backup_path, "fileserver")
        if not os.path.exists(fileserver_backup_dir) or not os.listdir(fileserver_backup_dir):
            self.logger.error("Fileserver backup directory is empty. Backup failed.")
            return 1
        else:
            self.logger.info(f"Fileserver data copied to: {fileserver_backup_dir}")
        return 0

    def restore_fileserver(self, backup_path: str) -> int:
        """ Restores fileserver data from a snapshot by rsyncing it back to the live volume."""
        # Read original volume mount from compose
        fileserver_volumes = self.compose_dict["services"]["fileserver"]["volumes"]
        fileserver_path = None
        for volume in fileserver_volumes:
            if "/mnt/fileserver" in volume:
                fileserver_path = os.path.dirname(volume.split(":")[0])
                self.logger.info(f"Fileserver path: {fileserver_path}")
                break
        self.logger.info(f"Restoring fileserver to {fileserver_path}")

        # Rsync backup back into the live volume
        src = os.path.join(backup_path, "fileserver")
        response = subprocess.run([
            "rsync", "-av", "--delete",
            src, fileserver_path
        ], check=True)
        if response.returncode != 0:
            self.logger.error(f"Rsync failed: {response}.")
            return 1
        
        self.logger.info("Fileserver data restored successfully.")
        return 0


@app.command()
def create_snapshot(
    backup_root: str = typer.Option(
        help="Root directory where ClearML backups will be stored."
    ),
    docker_compose_file: str = typer.Option(
        help="Path to the Docker Compose file for ClearML server (typically '/opt/clearml/docker-compose.yml')."
    ),
    retention: int = typer.Option(
        0, 
        help="Number of most recent snapshots to keep. Older snapshots will be deleted. Default is 0 (no clean up).",
    )
):
    """Create a timestamped ClearML snapshot."""
    tic = time.time()
    backup_manager = ClearMLBackupManager(docker_compose_file=docker_compose_file)
    status, backup_path = backup_manager.create_snapshot(backup_root)
    if status == 0 and retention > 0:
            backup_manager.cleanup_old_backups(backup_root, keep_last=retention)
            
    if status != 0:
        typer.secho(f"{datetime.now()} | Backup failed. Check snapshot logs for details: {backup_path}", fg=typer.colors.RED)
    else:
        typer.secho(
            f"{datetime.now()} | Backup completed in {str(timedelta(seconds=int(time.time() - tic)))}. Snapshot located in {backup_path}.", 
            fg=typer.colors.GREEN
            )


@app.command()
def restore_snapshot(
    snapshot_path: str = typer.Option(
        help="Path to the ClearML snapshot directory to restore from."
    )
):
    """Restore a ClearML snapshot."""
    typer.secho(f"WARNING! This will overwrite existing ClearML data. Proceed with caution.", fg=typer.colors.YELLOW)
    typer.secho(f"Before you proceed, make sure that:", fg=typer.colors.YELLOW)
    typer.secho(f"- You have a manual backup of your current ClearML data (in case there are any on the current server instance).", fg=typer.colors.YELLOW)
    typer.secho(f"- The data subfolders are created with correct permissions (see https://clear.ml/docs/latest/docs/deploying_clearml/clearml_server_linux_mac).", fg=typer.colors.YELLOW)
    typer.secho(f"- You are using a docker-compose.yml and config/ copy from {snapshot_path}.", fg=typer.colors.YELLOW)
    typer.secho(f"- The target ClearML server instance is up and running.", fg=typer.colors.YELLOW)
    typer.confirm("Do you want to proceed with the restoration?", abort=True)
    
    if snapshot_path.endswith("/"):
        snapshot_path = snapshot_path[:-1]
        
    backup_manager = ClearMLBackupManager(docker_compose_file=os.path.join(snapshot_path, "docker-compose.yml"))
    status = backup_manager.restore_snapshot(snapshot_path)
    if status != 0:
        typer.secho(f"Snapshot restoration failed. Check logs for details", fg=typer.colors.RED)
    else:
        typer.secho(f"Snapshot restored successfully.", fg=typer.colors.GREEN)
    
    
@app.command()
def clear_schedule():
    """Clear the existing ClearML backup cron job."""
    user = pwd.getpwuid(os.getuid())
    cron = CronTab(user=user.pw_name)
    for job in cron:
        if job.comment == "clearml-backup-tool":
            typer.secho(f"Clearing cron job: {job}", fg=typer.colors.BLUE)
            cron.remove(job)
    cron.write()
    typer.secho("Cleared all existing ClearML backup cron jobs.", fg=typer.colors.GREEN)
    
    
@app.command()
def setup_schedule(
    backup_root: str = typer.Option(
        "./clearml_backup",
        help="Root directory where ClearML backups will be stored. Default is './clearml_backup'.",
        prompt="Enter the backup root directory",
    ),
    docker_compose_file: str = typer.Option(
        "/opt/clearml/docker-compose.yml",
        help="Path to the Docker Compose file for ClearML server (typically '/opt/clearml/docker-compose.yml').",
        prompt="Enter the path to the Docker Compose file"
    ),
    retention: int = typer.Option(
        2,
        help="Number of most recent snapshots to keep. Older snapshots will be deleted. (0 = no cleanup).",
        prompt="Enter the number of most recent snapshots to keep (0 = no cleanup)"
    ),
    backup_period: str = typer.Option(
        "7d",
        help="Backup period for the cron job in the format '{number}{unit}} where unit is one of 'm' (minutes), 'h' (hours), 'd' (days)'.",
        prompt="Enter the backup period for the cron job (format: '{number}{unit}').",
    )
):
    """Set up a cron job to automatically create ClearML snapshots. You can run this without any arguments to go through an interactive setup."""
    assert re.match(r'^\d+[mhd]$', backup_period), "Backup period must be in the format '{number}{unit}' where unit is one of 'm', 'h', 'd'."
    
    user = pwd.getpwuid(os.getuid())
    cron = CronTab(user=user.pw_name)
    abs_backup_root = os.path.abspath(backup_root)
    abs_docker_compose_file = os.path.abspath(docker_compose_file)
    
    for job in cron:
        if job.comment == "clearml-backup-tool":
            typer.secho(f"Clearing cron job: {job}", fg=typer.colors.BLUE)
            cron.remove(job)
    cron.write()
    
    uv_path = subprocess.run(["which", "uv"], capture_output=True, text=True, check=True).stdout.strip()
    command = (f"{uv_path} run {os.path.abspath(__file__)} create-snapshot " 
               f"--backup-root {abs_backup_root} "
               f"--docker-compose-file {abs_docker_compose_file} "
               f"--retention {retention} "
               f"| tail -n 1 >> {abs_backup_root}/autobackup.log 2>&1"
               )
    job = cron.new(command=command, comment="clearml-backup-tool")
    num, unit = int(backup_period[:-1]), backup_period[-1]
    match unit:
        case 'm':
            job.minute.every(num)
        case 'h':
            job.hour.every(num)
        case 'd':
            job.day.every(num)
        case _:
            raise ValueError(f"Invalid backup period unit: {unit}. Must be one of 'm', 'h', 'd'.")
    cron.write()
    
    for job in cron:
        if job.comment == "clearml-backup-tool":
            break
    typer.secho(f"Set up cron job: {job}", fg=typer.colors.BLUE)
    typer.secho(f"Scheduled ClearML backup every {num}{unit}. Job will log to {abs_backup_root}/autobackup.log.", fg=typer.colors.GREEN)


if __name__ == "__main__":
    app()
