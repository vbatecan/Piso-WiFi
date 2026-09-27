"""Backup service providing snapshots, listing, restoration, and pruning for Piso-WiFi."""

from datetime import datetime
import logging
import os
import re
import shutil
import sqlite3
import tempfile
from typing import Any, Dict, List, Optional
import zipfile

logger = logging.getLogger(__name__)


class BackupService:
    """Manages full system configuration and SQLite database backup snapshots.

    Creates sanitized timestamped archives, lists historical backups, safely restores
    snapshots preserving active credentials, and prunes older archives.
    """

    SENSITIVE_KEY_PATTERNS = [
        re.compile(r"password", re.IGNORECASE),
        re.compile(r"secret", re.IGNORECASE),
        re.compile(r"token", re.IGNORECASE),
        re.compile(r"api_key", re.IGNORECASE),
        re.compile(r"private", re.IGNORECASE),
        re.compile(r"credential", re.IGNORECASE),
    ]

    def __init__(
        self,
        db_path: Optional[str] = None,
        env_path: Optional[str] = None,
        backup_dir: Optional[str] = None,
    ):
        """Initialize BackupService with database, environment, and backup directories.

        Args:
            db_path: Path to the active SQLite database file. Defaults to DB_PATH or config/piso_wifi.db.
            env_path: Path to the .env configuration file. Defaults to ENV_PATH or .env.
            backup_dir: Directory where backup archives are stored. Defaults to BACKUP_DIR or backups.
        """
        self.db_path = os.path.abspath(db_path or os.getenv("DB_PATH", "config/piso_wifi.db"))
        self.env_path = os.path.abspath(env_path or os.getenv("ENV_PATH", ".env"))
        self.backup_dir = os.path.abspath(backup_dir or os.getenv("BACKUP_DIR", "backups"))

    @classmethod
    def is_sensitive_key(cls, key: str) -> bool:
        """Check whether an environment variable key is considered sensitive.

        Args:
            key: Name of the environment variable.

        Returns:
            bool: True if key matches sensitive patterns.
        """
        clean_key = (key or "").strip()
        for pattern in cls.SENSITIVE_KEY_PATTERNS:
            if pattern.search(clean_key):
                return True
        return False

    @classmethod
    def sanitize_env(cls, env_content: str) -> str:
        """Sanitize an environment file string, redacting sensitive passwords and secrets.

        Args:
            env_content: Raw contents of the .env file.

        Returns:
            str: Redacted environment file content.
        """
        sanitized_lines = []
        for line in env_content.splitlines(keepends=True):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                sanitized_lines.append(line)
                continue

            if "=" in line:
                key, _, val = line.partition("=")
                key_clean = key.strip()
                if cls.is_sensitive_key(key_clean):
                    sanitized_lines.append(f"{key_clean}=********\n")
                else:
                    sanitized_lines.append(line)
            else:
                sanitized_lines.append(line)

        return "".join(sanitized_lines)

    def _snapshot_sqlite(self, src_db_path: str, dst_path: str) -> None:
        """Create a consistent snapshot of the SQLite database using SQLite's online backup API."""
        if os.path.exists(src_db_path) and os.path.getsize(src_db_path) > 0:
            src_conn = None
            dst_conn = None
            try:
                src_conn = sqlite3.connect(src_db_path)
                dst_conn = sqlite3.connect(dst_path)
                with dst_conn:
                    src_conn.backup(dst_conn)
            except Exception as e:
                logger.warning(
                    "SQLite online backup failed (%s), falling back to file copy: %s",
                    e,
                    src_db_path,
                )
                shutil.copy2(src_db_path, dst_path)
            finally:
                if dst_conn:
                    try:
                        dst_conn.close()
                    except Exception:
                        pass
                if src_conn:
                    try:
                        src_conn.close()
                    except Exception:
                        pass
        elif os.path.exists(src_db_path):
            shutil.copy2(src_db_path, dst_path)
        else:
            # Create an empty file if db does not exist yet
            with open(dst_path, "w", encoding="utf-8") as f:
                f.write("")

    def create_backup(self, dest_dir: Optional[str] = None) -> Dict[str, Any]:
        """Create a timestamped zip archive containing piso_wifi.db and sanitized .env.

        Args:
            dest_dir: Destination directory for the backup archive. Defaults to self.backup_dir.

        Returns:
            Dict[str, Any]: Backup metadata including filename, filepath, size, and timestamp.
        """
        target_dir = os.path.abspath(dest_dir or self.backup_dir)
        os.makedirs(target_dir, exist_ok=True)

        now = datetime.now()
        timestamp = now.strftime("%Y%m%d_%H%M%S")
        filename = f"piso_wifi_backup_{timestamp}.zip"
        zip_path = os.path.join(target_dir, filename)

        if os.path.exists(zip_path):
            timestamp = now.strftime("%Y%m%d_%H%M%S_%f")
            filename = f"piso_wifi_backup_{timestamp}.zip"
            zip_path = os.path.join(target_dir, filename)

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_db = os.path.join(temp_dir, "piso_wifi.db")
            temp_env = os.path.join(temp_dir, ".env")

            # 1. Snapshot database
            self._snapshot_sqlite(self.db_path, temp_db)

            # 2. Snapshot & sanitize .env
            if os.path.exists(self.env_path):
                try:
                    with open(self.env_path, "r", encoding="utf-8") as f:
                        raw_env = f.read()
                    sanitized_env = self.sanitize_env(raw_env)
                except Exception as e:
                    logger.error("Failed to read env file for backup: %s", e)
                    sanitized_env = "# Error reading original .env\n"
            else:
                sanitized_env = "# Piso-WiFi Auto-Generated Environment\n"

            with open(temp_env, "w", encoding="utf-8") as f:
                f.write(sanitized_env)

            # 3. Create compressed zip archive
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zipf:
                zipf.write(temp_db, arcname="piso_wifi.db")
                zipf.write(temp_env, arcname=".env")

        file_size = os.path.getsize(zip_path)
        logger.info("Backup successfully created at %s (%d bytes)", zip_path, file_size)

        return {
            "filename": filename,
            "filepath": zip_path,
            "size_bytes": file_size,
            "timestamp": timestamp,
            "created_at": now.isoformat(),
            "contains_db": True,
            "contains_env": True,
            "status": "success",
        }

    def list_backups(self, dest_dir: Optional[str] = None) -> List[Dict[str, Any]]:
        """Enumerate existing backup archives sorted from newest to oldest.

        Args:
            dest_dir: Directory to scan for backups. Defaults to self.backup_dir.

        Returns:
            List[Dict[str, Any]]: List of metadata dictionaries for each valid backup.
        """
        target_dir = os.path.abspath(dest_dir or self.backup_dir)
        if not os.path.exists(target_dir):
            return []

        backups: List[Dict[str, Any]] = []

        try:
            entries = os.listdir(target_dir)
        except OSError as e:
            logger.error("Failed to list backup directory '%s': %s", target_dir, e)
            return []

        for name in entries:
            if not name.endswith(".zip"):
                continue

            full_path = os.path.join(target_dir, name)
            if not os.path.isfile(full_path):
                continue

            try:
                stat = os.stat(full_path)
                with zipfile.ZipFile(full_path, "r") as zf:
                    namelist = zf.namelist()

                has_db = "piso_wifi.db" in namelist or any(n.endswith(".db") for n in namelist)
                has_env = ".env" in namelist or any(n.endswith(".env") for n in namelist)

                backups.append(
                    {
                        "filename": name,
                        "filepath": full_path,
                        "size_bytes": stat.st_size,
                        "created_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                        "mtime": stat.st_mtime,
                        "contains_db": has_db,
                        "contains_env": has_env,
                        "files": namelist,
                    }
                )
            except Exception as e:
                logger.warning("Skipping corrupted or unreadable zip '%s': %s", full_path, e)

        # Sort descending by modification time (most recent first)
        backups.sort(key=lambda b: b.get("mtime", 0.0), reverse=True)

        # Clean internal mtime sorting helper from public response
        for b in backups:
            b.pop("mtime", None)

        return backups

    def restore_backup(self, backup_zip_path: str) -> bool:
        """Restore SQLite database and configuration from a backup archive.

        Preserves existing live secrets if the backup archive contains sanitized asterisks.

        Args:
            backup_zip_path: Filepath to the backup zip archive.

        Returns:
            bool: True if restore succeeded, False otherwise.
        """
        full_path = os.path.abspath(backup_zip_path)
        if not os.path.exists(full_path) or not zipfile.is_zipfile(full_path):
            logger.error("Cannot restore backup: file '%s' does not exist or is not a valid zip", full_path)
            return False

        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                with zipfile.ZipFile(full_path, "r") as zipf:
                    zipf.extractall(temp_dir)

                extracted_files = os.listdir(temp_dir)

                # 1. Restore Database
                db_name = next(
                    (n for n in ["piso_wifi.db"] if n in extracted_files),
                    next((n for n in extracted_files if n.endswith(".db")), None),
                )
                if db_name:
                    extracted_db = os.path.join(temp_dir, db_name)
                    os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
                    # Verify DB integrity if sqlite3 database
                    conn = None
                    try:
                        conn = sqlite3.connect(extracted_db)
                        cursor = conn.cursor()
                        cursor.execute("PRAGMA integrity_check")
                        res = cursor.fetchone()
                        if res and res[0] != "ok":
                            logger.error("Backup DB failed SQLite integrity check: %s", res[0])
                            return False
                    except Exception as e:
                        logger.warning("Could not run SQLite integrity check on backup db: %s", e)
                    finally:
                        if conn:
                            try:
                                conn.close()
                            except Exception:
                                pass

                    shutil.copy2(extracted_db, self.db_path)
                    logger.info("Restored database to %s", self.db_path)

                # 2. Restore .env with credential preservation
                env_name = next(
                    (n for n in [".env"] if n in extracted_files),
                    next((n for n in extracted_files if n.endswith(".env")), None),
                )
                if env_name:
                    extracted_env = os.path.join(temp_dir, env_name)
                    with open(extracted_env, "r", encoding="utf-8") as f:
                        backup_lines = f.readlines()

                    # Read active .env to preserve unmasked passwords if backup has '********'
                    existing_secrets: Dict[str, str] = {}
                    if os.path.exists(self.env_path):
                        with open(self.env_path, "r", encoding="utf-8") as f:
                            for line in f:
                                stripped = line.strip()
                                if stripped and not stripped.startswith("#") and "=" in stripped:
                                    k, _, v = stripped.partition("=")
                                    existing_secrets[k.strip()] = v.strip()

                    final_lines = []
                    for line in backup_lines:
                        stripped = line.strip()
                        if stripped and not stripped.startswith("#") and "=" in stripped:
                            k, _, v = stripped.partition("=")
                            k_clean = k.strip()
                            v_clean = v.strip()

                            # If value is masked and we have the active secret, preserve it
                            if ("*" in v_clean or "[REDACTED]" in v_clean) and k_clean in existing_secrets:
                                final_lines.append(f"{k_clean}={existing_secrets[k_clean]}\n")
                            else:
                                final_lines.append(line)
                        else:
                            final_lines.append(line)

                    os.makedirs(os.path.dirname(self.env_path), exist_ok=True)
                    with open(self.env_path, "w", encoding="utf-8") as f:
                        f.writelines(final_lines)
                    logger.info("Restored configuration to %s", self.env_path)

            return True
        except Exception as e:
            logger.error("Error restoring backup from '%s': %s", full_path, e, exc_info=True)
            return False

    def prune_old_backups(self, max_keep: int = 7, dest_dir: Optional[str] = None) -> int:
        """Prune excess backup files, retaining only the most recent max_keep archives.

        Args:
            max_keep: Number of newest backups to retain (default: 7).
            dest_dir: Target directory containing backups. Defaults to self.backup_dir.

        Returns:
            int: Number of deleted backup archives.
        """
        if max_keep < 0:
            max_keep = 0

        backups = self.list_backups(dest_dir)
        if len(backups) <= max_keep:
            return 0

        to_delete = backups[max_keep:]
        deleted_count = 0

        for item in to_delete:
            path = item.get("filepath")
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                    deleted_count += 1
                    logger.info("Pruned old backup archive: %s", path)
                except OSError as e:
                    logger.error("Failed to delete old backup '%s': %s", path, e)

        return deleted_count

    def create_automatic_backup(
        self, dest_dir: Optional[str] = None, max_keep: int = 7
    ) -> Dict[str, Any]:
        """Create a scheduled or automatic snapshot and prune obsolete archives.

        Args:
            dest_dir: Target directory for backup. Defaults to self.backup_dir.
            max_keep: Number of archives to retain (default: 7).

        Returns:
            Dict[str, Any]: Metadata dictionary for the created backup.
        """
        result = self.create_backup(dest_dir=dest_dir)
        pruned = self.prune_old_backups(max_keep=max_keep, dest_dir=dest_dir)
        result["pruned_count"] = pruned
        return result
