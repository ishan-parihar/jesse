"""Platform-agnostic daemon abstraction layer for managing background services.

Supports systemd (Linux), launchd (macOS), and nohup (fallback).
Self-contained — no external dependencies beyond stdlib.
"""

import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


class DaemonManager:
    """Manage background services across init systems.

    Detects the platform automatically and delegates to the appropriate
    backend: systemd, launchd, or nohup-based PID file management.
    """

    PLATFORM_SYSTEMD = "systemd"
    PLATFORM_LAUNCHD = "launchd"
    PLATFORM_NOHUP = "nohup"

    SYSTEMD_SYSTEM_DIR = Path("/etc/systemd/system")
    SYSTEMD_USER_DIR = Path.home() / ".config" / "systemd" / "user"
    LAUNCHD_DIR = Path.home() / "Library" / "LaunchAgents"
    NOHUP_DAEMON_DIR = Path.home() / ".mt5-mcp" / "daemon"

    def __init__(self) -> None:
        self._platform = self._detect_platform()

    # ------------------------------------------------------------------ #
    # Platform detection
    # ------------------------------------------------------------------ #

    def _detect_platform(self) -> str:
        """Return the init system to use based on the current OS."""
        if sys.platform == "linux":
            return self.PLATFORM_SYSTEMD
        elif sys.platform == "darwin":
            return self.PLATFORM_LAUNCHD
        else:
            return self.PLATFORM_NOHUP

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _run(
        cmd: list[str], *, timeout: int = 10
    ) -> subprocess.CompletedProcess[str] | None:
        """Run a subprocess and return the result, or None on error."""
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except Exception:
            return None

    def _systemd_unit_path(self, service_name: str) -> Path | None:
        """Return the path to a systemd unit file if it exists."""
        system_path = self.SYSTEMD_SYSTEM_DIR / service_name
        if system_path.is_file():
            return system_path
        user_path = self.SYSTEMD_USER_DIR / service_name
        if user_path.is_file():
            return user_path
        return None

    def _launchd_plist_path(self, service_name: str) -> Path | None:
        """Return the path to a launchd plist file if it exists."""
        plist_name = service_name
        if not plist_name.endswith(".plist"):
            plist_name = plist_name.replace(".service", "") + ".plist"
        path = self.LAUNCHD_DIR / plist_name
        if path.is_file():
            return path
        return None

    def _nohup_pid_file(self, service_name: str) -> Path:
        """Return the PID file path for a nohup-managed service."""
        self.NOHUP_DAEMON_DIR.mkdir(parents=True, exist_ok=True)
        safe_name = service_name.replace(".", "_").replace("/", "_")
        return self.NOHUP_DAEMON_DIR / f"{safe_name}.pid"

    def _nohup_meta_file(self, service_name: str) -> Path:
        """Return the metadata JSON file path for a nohup service."""
        self.NOHUP_DAEMON_DIR.mkdir(parents=True, exist_ok=True)
        safe_name = service_name.replace(".", "_").replace("/", "_")
        return self.NOHUP_DAEMON_DIR / f"{safe_name}.json"

    def _parse_unit_file(self, path: Path) -> dict[str, str]:
        """Parse a systemd unit file into a flat dict of key=value pairs."""
        result: dict[str, str] = {}
        try:
            for line in path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or line.startswith("["):
                    continue
                if "=" in line:
                    key, _, value = line.partition("=")
                    result[key.strip()] = value.strip()
        except Exception:
            pass
        return result

    def _parse_plist(self, path: Path) -> dict[str, Any]:
        """Parse a launchd plist (XML) into a dict.

        Uses a minimal XML parser to avoid external dependencies.
        Falls back to reading Program/ProgramArguments from simple key-value
        pairs in the plist.
        """
        result: dict[str, Any] = {}
        try:
            content = path.read_text()
            current_key: str | None = None
            for line in content.splitlines():
                line = line.strip()
                if line.startswith("<key>") and line.endswith("</key>"):
                    current_key = line[5:-6]
                elif (
                    current_key
                    and line.startswith("<string>")
                    and line.endswith("</string>")
                ):
                    result[current_key] = line[8:-9]
                    current_key = None
                elif current_key and line.startswith("<true/>"):
                    result[current_key] = True
                    current_key = None
                elif current_key and line.startswith("<false/>"):
                    result[current_key] = False
                    current_key = None
                elif (
                    current_key
                    and line.startswith("<integer>")
                    and line.endswith("</integer>")
                ):
                    try:
                        result[current_key] = int(line[9:-10])
                    except ValueError:
                        result[current_key] = line[9:-10]
                    current_key = None
        except Exception:
            pass
        return result

    # ------------------------------------------------------------------ #
    # Public API — systemd backend
    # ------------------------------------------------------------------ #

    def _systemd_is_installed(self, service_name: str) -> bool:
        unit_path = self._systemd_unit_path(service_name)
        if unit_path:
            return True
        result = self._run(["systemctl", "list-unit-files", service_name])
        if result and result.returncode == 0:
            return service_name in result.stdout
        return False

    def _systemd_is_loaded(self, service_name: str) -> bool:
        result = self._run(["systemctl", "list-units", service_name, "--no-pager"])
        if result and result.returncode == 0:
            return service_name in result.stdout
        return False

    def _systemd_is_running(self, service_name: str) -> bool:
        for cmd in (
            ["systemctl", "--user", "is-active", service_name],
            ["systemctl", "is-active", service_name],
        ):
            result = self._run(cmd)
            if result and result.stdout.strip() == "active":
                return True
        return False

    def _systemd_install(
        self,
        service_name: str,
        exec_start: str,
        working_directory: str = "",
        environment: dict[str, str] | None = None,
        description: str = "",
    ) -> bool:
        unit_content = self._build_systemd_unit(
            service_name, exec_start, working_directory, environment, description
        )

        unit_path = self.SYSTEMD_USER_DIR / service_name
        try:
            self.SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)
            unit_path.write_text(unit_content)
        except PermissionError:
            unit_path = self.SYSTEMD_SYSTEM_DIR / service_name
            try:
                unit_content_tmp = Path(tempfile.mkstemp(suffix=".service")[1])
                unit_content_tmp.write_text(unit_content)
                result = self._run(
                    ["sudo", "cp", str(unit_content_tmp), str(unit_path)]
                )
                if not result or result.returncode != 0:
                    return False
                unit_content_tmp.unlink(missing_ok=True)
            except Exception:
                return False
        except Exception:
            return False

        self._run(["systemctl", "--user", "daemon-reload"])
        self._run(["systemctl", "daemon-reload"])
        self._run(["systemctl", "--user", "enable", service_name])
        self._run(["systemctl", "enable", service_name])
        return True

    def _build_systemd_unit(
        self,
        service_name: str,
        exec_start: str,
        working_directory: str,
        environment: dict[str, str] | None,
        description: str,
    ) -> str:
        lines = [
            "[Unit]",
            f"Description={description or service_name}",
            "After=network.target",
            "",
            "[Service]",
            "Type=simple",
            f"ExecStart={exec_start}",
        ]
        if working_directory:
            lines.append(f"WorkingDirectory={working_directory}")
        if environment:
            for key, value in environment.items():
                lines.append(f'Environment="{key}={value}"')
        lines += [
            "Restart=on-failure",
            "RestartSec=5",
            "StandardOutput=journal",
            "StandardError=journal",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
        return "\n".join(lines)

    def _systemd_repair(self, service_name: str, expected_exec: str) -> bool:
        unit_path = self._systemd_unit_path(service_name)
        if not unit_path:
            return False
        try:
            config = self._parse_unit_file(unit_path)
            config["ExecStart"] = expected_exec
            new_unit = self._build_systemd_unit(
                service_name,
                expected_exec,
                config.get("WorkingDirectory", ""),
                self._extract_env_from_unit(config),
                config.get("Description", ""),
            )
            unit_path.write_text(new_unit)
            self._run(["systemctl", "--user", "daemon-reload"])
            self._run(["systemctl", "daemon-reload"])
            return True
        except Exception:
            return False

    def _extract_env_from_unit(self, config: dict[str, str]) -> dict[str, str]:
        """Extract Environment entries from a parsed unit file."""
        env: dict[str, str] = {}
        for key, value in config.items():
            if key == "Environment":
                cleaned = value.strip('"').strip("'")
                if "=" in cleaned:
                    k, _, v = cleaned.partition("=")
                    env[k.strip()] = v.strip()
        return env

    def _systemd_uninstall(self, service_name: str) -> bool:
        try:
            self._run(["systemctl", "--user", "stop", service_name])
            self._run(["systemctl", "stop", service_name])
            self._run(["systemctl", "--user", "disable", service_name])
            self._run(["systemctl", "disable", service_name])
        except Exception:
            pass

        for base in [self.SYSTEMD_USER_DIR, self.SYSTEMD_SYSTEM_DIR]:
            unit_path = base / service_name
            if unit_path.is_file():
                try:
                    unit_path.unlink()
                except PermissionError:
                    result = self._run(["sudo", "rm", "-f", str(unit_path)])
                    if not result or result.returncode != 0:
                        return False
                except Exception:
                    return False

        self._run(["systemctl", "--user", "daemon-reload"])
        self._run(["systemctl", "daemon-reload"])
        return True

    def _systemd_restart(self, service_name: str) -> bool:
        result = self._run(["systemctl", "--user", "restart", service_name])
        if result and result.returncode == 0:
            return True
        result = self._run(["systemctl", "restart", service_name])
        return result is not None and result.returncode == 0

    def _systemd_read_command(self, service_name: str) -> dict[str, Any] | None:
        unit_path = self._systemd_unit_path(service_name)
        if not unit_path:
            return None
        config = self._parse_unit_file(unit_path)
        env = self._extract_env_from_unit(config)
        return {
            "exec_start": config.get("ExecStart"),
            "working_directory": config.get("WorkingDirectory", ""),
            "environment": env if env else None,
        }

    def _systemd_detect_drift(
        self, service_name: str, expected: dict[str, Any]
    ) -> list[str]:
        current = self._systemd_read_command(service_name)
        if current is None:
            return ["unit file not found"]
        issues: list[str] = []
        if (
            expected.get("exec_start")
            and current.get("exec_start") != expected["exec_start"]
        ):
            issues.append(
                f"ExecStart drifted: expected '{expected['exec_start']}', "
                f"got '{current.get('exec_start')}'"
            )
        if (
            expected.get("working_directory")
            and current.get("working_directory") != expected["working_directory"]
        ):
            issues.append(
                f"WorkingDirectory drifted: expected '{expected['working_directory']}', "
                f"got '{current.get('working_directory')}'"
            )
        expected_env = expected.get("environment") or {}
        current_env = current.get("environment") or {}
        for key, value in expected_env.items():
            if current_env.get(key) != value:
                issues.append(
                    f"Environment drifted for '{key}': expected '{value}', "
                    f"got '{current_env.get(key)}'"
                )
        return issues

    # ------------------------------------------------------------------ #
    # Public API — launchd backend
    # ------------------------------------------------------------------ #

    def _launchd_is_installed(self, service_name: str) -> bool:
        plist_path = self._launchd_plist_path(service_name)
        return plist_path is not None

    def _launchd_is_loaded(self, service_name: str) -> bool:
        label = service_name.replace(".service", "")
        result = self._run(["launchctl", "list", label])
        if result and result.returncode == 0:
            return label in result.stdout
        return False

    def _launchd_is_running(self, service_name: str) -> bool:
        label = service_name.replace(".service", "")
        result = self._run(["launchctl", "list", label])
        if result and result.returncode == 0 and label in result.stdout:
            lines = result.stdout.strip().splitlines()
            for line in lines:
                if label in line:
                    parts = line.split()
                    if len(parts) >= 1:
                        try:
                            pid = int(parts[0])
                            return pid > 0
                        except ValueError:
                            return True
        return False

    def _launchd_install(
        self,
        service_name: str,
        exec_start: str,
        working_directory: str = "",
        environment: dict[str, str] | None = None,
        description: str = "",
    ) -> bool:
        label = service_name.replace(".service", "")
        plist_name = label + ".plist"
        plist_path = self.LAUNCHD_DIR / plist_name

        try:
            self.LAUNCHD_DIR.mkdir(parents=True, exist_ok=True)
        except Exception:
            return False

        plist = self._build_launchd_plist(
            label, exec_start, working_directory, environment, description
        )
        try:
            plist_path.write_text(plist)
        except Exception:
            return False

        result = self._run(["launchctl", "load", str(plist_path)])
        return result is not None and result.returncode == 0

    def _build_launchd_plist(
        self,
        label: str,
        exec_start: str,
        working_directory: str,
        environment: dict[str, str] | None,
        description: str,
    ) -> str:
        try:
            parts = shlex.split(exec_start)
        except ValueError:
            parts = exec_start.split()

        program = parts[0] if parts else exec_start
        args = parts if len(parts) > 1 else []

        lines = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">',
            '<plist version="1.0">',
            "<dict>",
            f"    <key>Label</key>",
            f"    <string>{label}</string>",
        ]

        if args:
            lines.append(f"    <key>ProgramArguments</key>")
            lines.append("    <array>")
            for arg in parts:
                lines.append(f"        <string>{arg}</string>")
            lines.append("    </array>")
        else:
            lines.append(f"    <key>Program</key>")
            lines.append(f"    <string>{program}</string>")

        if working_directory:
            lines.append(f"    <key>WorkingDirectory</key>")
            lines.append(f"    <string>{working_directory}</string>")

        if environment:
            lines.append(f"    <key>EnvironmentVariables</key>")
            lines.append("    <dict>")
            for key, value in environment.items():
                lines.append(f"        <key>{key}</key>")
                lines.append(f"        <string>{value}</string>")
            lines.append("    </dict>")

        lines += [
            f"    <key>RunAtLoad</key>",
            f"    <true/>",
            f"    <key>KeepAlive</key>",
            f"    <dict>",
            f"        <key>SuccessfulExit</key>",
            f"        <false/>",
            f"    </dict>",
            f"    <key>StandardOutPath</key>",
            f"    <string>/tmp/{label}.out</string>",
            f"    <key>StandardErrorPath</key>",
            f"    <string>/tmp/{label}.err</string>",
            "</dict>",
            "</plist>",
            "",
        ]
        return "\n".join(lines)

    def _launchd_repair(self, service_name: str, expected_exec: str) -> bool:
        plist_path = self._launchd_plist_path(service_name)
        if not plist_path:
            return False

        label = service_name.replace(".service", "")
        self._run(["launchctl", "unload", str(plist_path)])

        current = self._parse_plist(plist_path)
        working_dir = current.get("WorkingDirectory", "")
        env: dict[str, str] | None = None

        plist = self._build_launchd_plist(label, expected_exec, working_dir, env, "")
        try:
            plist_path.write_text(plist)
        except Exception:
            return False

        result = self._run(["launchctl", "load", str(plist_path)])
        return result is not None and result.returncode == 0

    def _launchd_uninstall(self, service_name: str) -> bool:
        label = service_name.replace(".service", "")
        plist_path = self._launchd_plist_path(service_name)

        if plist_path:
            self._run(["launchctl", "unload", str(plist_path)])

        self._run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"])

        if plist_path and plist_path.is_file():
            try:
                plist_path.unlink()
            except Exception:
                return False

        return True

    def _launchd_restart(self, service_name: str) -> bool:
        plist_path = self._launchd_plist_path(service_name)
        if not plist_path:
            return False
        self._run(["launchctl", "unload", str(plist_path)])
        result = self._run(["launchctl", "load", str(plist_path)])
        return result is not None and result.returncode == 0

    def _launchd_read_command(self, service_name: str) -> dict[str, Any] | None:
        plist_path = self._launchd_plist_path(service_name)
        if not plist_path:
            return None
        config = self._parse_plist(plist_path)

        exec_start = ""
        if "ProgramArguments" in str(config):
            parts: list[str] = []
            try:
                in_args = False
                for line in plist_path.read_text().splitlines():
                    line = line.strip()
                    if "<key>ProgramArguments</key>" in line:
                        in_args = True
                        continue
                    if in_args and line == "</array>":
                        break
                    if (
                        in_args
                        and line.startswith("<string>")
                        and line.endswith("</string>")
                    ):
                        parts.append(line[8:-9])
            except Exception:
                pass
            exec_start = " ".join(parts) if parts else config.get("Program", "")
        else:
            exec_start = config.get("Program", "")

        return {
            "exec_start": exec_start if exec_start else None,
            "working_directory": config.get("WorkingDirectory", ""),
            "environment": None,
        }

    def _launchd_detect_drift(
        self, service_name: str, expected: dict[str, Any]
    ) -> list[str]:
        current = self._launchd_read_command(service_name)
        if current is None:
            return ["plist file not found"]
        issues: list[str] = []
        if (
            expected.get("exec_start")
            and current.get("exec_start") != expected["exec_start"]
        ):
            issues.append(
                f"ProgramArguments drifted: expected '{expected['exec_start']}', "
                f"got '{current.get('exec_start')}'"
            )
        if (
            expected.get("working_directory")
            and current.get("working_directory") != expected["working_directory"]
        ):
            issues.append(
                f"WorkingDirectory drifted: expected '{expected['working_directory']}', "
                f"got '{current.get('working_directory')}'"
            )
        return issues

    # ------------------------------------------------------------------ #
    # Public API — nohup backend (fallback)
    # ------------------------------------------------------------------ #

    def _nohup_is_installed(self, service_name: str) -> bool:
        pid_file = self._nohup_pid_file(service_name)
        return pid_file.is_file()

    def _nohup_is_loaded(self, service_name: str) -> bool:
        return self._nohup_is_installed(service_name)

    def _nohup_is_running(self, service_name: str) -> bool:
        pid_file = self._nohup_pid_file(service_name)
        if not pid_file.is_file():
            return False
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
            return True
        except (ValueError, ProcessLookupError, PermissionError, OSError):
            return False

    def _nohup_install(
        self,
        service_name: str,
        exec_start: str,
        working_directory: str = "",
        environment: dict[str, str] | None = None,
        description: str = "",
    ) -> bool:
        safe_name = service_name.replace(".", "_").replace("/", "_")
        log_file = self.NOHUP_DAEMON_DIR / f"{safe_name}.log"

        try:
            self.NOHUP_DAEMON_DIR.mkdir(parents=True, exist_ok=True)
        except Exception:
            return False

        env_prefix = ""
        if environment:
            env_prefix = " ".join(f'{k}="{v}"' for k, v in environment.items()) + " "

        cd_prefix = f"cd {working_directory} && " if working_directory else ""
        full_cmd = f"{env_prefix}{cd_prefix}{exec_start}"

        try:
            proc = subprocess.Popen(
                ["nohup", "sh", "-c", full_cmd],
                stdout=open(str(log_file), "a"),
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            pid = proc.pid
        except Exception:
            return False

        try:
            self._nohup_pid_file(service_name).write_text(str(pid))
        except Exception:
            return False

        meta = {
            "exec_start": exec_start,
            "working_directory": working_directory,
            "environment": environment,
            "description": description,
        }
        try:
            self._nohup_meta_file(service_name).write_text(json.dumps(meta))
        except Exception:
            pass

        return True

    def _nohup_repair(self, service_name: str, expected_exec: str) -> bool:
        self._nohup_stop(service_name)

        meta_path = self._nohup_meta_file(service_name)
        if not meta_path.is_file():
            return False
        try:
            meta = json.loads(meta_path.read_text())
        except Exception:
            return False

        meta["exec_start"] = expected_exec
        meta_path.write_text(json.dumps(meta))

        return self._nohup_install(
            service_name,
            expected_exec,
            meta.get("working_directory", ""),
            meta.get("environment"),
            meta.get("description", ""),
        )

    def _nohup_stop(self, service_name: str) -> bool:
        pid_file = self._nohup_pid_file(service_name)
        if not pid_file.is_file():
            return True
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 15)

            for _ in range(10):
                try:
                    os.kill(pid, 0)
                    time.sleep(0.5)
                except ProcessLookupError:
                    break
            else:
                try:
                    os.kill(pid, 9)
                except ProcessLookupError:
                    pass
        except (ValueError, ProcessLookupError, PermissionError, OSError):
            pass

        try:
            pid_file.unlink(missing_ok=True)
        except Exception:
            pass
        return True

    def _nohup_uninstall(self, service_name: str) -> bool:
        self._nohup_stop(service_name)
        safe_name = service_name.replace(".", "_").replace("/", "_")
        for suffix in [".pid", ".json", ".log"]:
            path = self.NOHUP_DAEMON_DIR / f"{safe_name}{suffix}"
            try:
                path.unlink(missing_ok=True)
            except Exception:
                return False
        return True

    def _nohup_restart(self, service_name: str) -> bool:
        meta_path = self._nohup_meta_file(service_name)
        if not meta_path.is_file():
            return False
        try:
            meta = json.loads(meta_path.read_text())
        except Exception:
            return False

        self._nohup_stop(service_name)
        return self._nohup_install(
            service_name,
            meta["exec_start"],
            meta.get("working_directory", ""),
            meta.get("environment"),
            meta.get("description", ""),
        )

    def _nohup_read_command(self, service_name: str) -> dict[str, Any] | None:
        meta_path = self._nohup_meta_file(service_name)
        if not meta_path.is_file():
            return None
        try:
            meta = json.loads(meta_path.read_text())
            return {
                "exec_start": meta.get("exec_start"),
                "working_directory": meta.get("working_directory", ""),
                "environment": meta.get("environment"),
            }
        except Exception:
            return None

    def _nohup_detect_drift(
        self, service_name: str, expected: dict[str, Any]
    ) -> list[str]:
        current = self._nohup_read_command(service_name)
        if current is None:
            return ["metadata file not found"]
        issues: list[str] = []
        if (
            expected.get("exec_start")
            and current.get("exec_start") != expected["exec_start"]
        ):
            issues.append(
                f"ExecStart drifted: expected '{expected['exec_start']}', "
                f"got '{current.get('exec_start')}'"
            )
        if (
            expected.get("working_directory")
            and current.get("working_directory") != expected["working_directory"]
        ):
            issues.append(
                f"WorkingDirectory drifted: expected '{expected['working_directory']}', "
                f"got '{current.get('working_directory')}'"
            )
        expected_env = expected.get("environment") or {}
        current_env = current.get("environment") or {}
        for key, value in expected_env.items():
            if current_env.get(key) != value:
                issues.append(
                    f"Environment drifted for '{key}': expected '{value}', "
                    f"got '{current_env.get(key)}'"
                )
        return issues

    # ------------------------------------------------------------------ #
    # Public API — platform-agnostic dispatch
    # ------------------------------------------------------------------ #

    def is_installed(self, service_name: str) -> bool:
        """Check if a service is installed on the system."""
        if self._platform == self.PLATFORM_SYSTEMD:
            return self._systemd_is_installed(service_name)
        elif self._platform == self.PLATFORM_LAUNCHD:
            return self._launchd_is_installed(service_name)
        else:
            return self._nohup_is_installed(service_name)

    def is_loaded(self, service_name: str) -> bool:
        """Check if a service is loaded in the init system."""
        if self._platform == self.PLATFORM_SYSTEMD:
            return self._systemd_is_loaded(service_name)
        elif self._platform == self.PLATFORM_LAUNCHD:
            return self._launchd_is_loaded(service_name)
        else:
            return self._nohup_is_loaded(service_name)

    def is_running(self, service_name: str) -> bool:
        """Check if a service is currently running."""
        if self._platform == self.PLATFORM_SYSTEMD:
            return self._systemd_is_running(service_name)
        elif self._platform == self.PLATFORM_LAUNCHD:
            return self._launchd_is_running(service_name)
        else:
            return self._nohup_is_running(service_name)

    def install(
        self,
        service_name: str,
        exec_start: str,
        working_directory: str = "",
        environment: dict[str, str] | None = None,
        description: str = "",
    ) -> bool:
        """Install a service on the system."""
        if self._platform == self.PLATFORM_SYSTEMD:
            return self._systemd_install(
                service_name, exec_start, working_directory, environment, description
            )
        elif self._platform == self.PLATFORM_LAUNCHD:
            return self._launchd_install(
                service_name, exec_start, working_directory, environment, description
            )
        else:
            return self._nohup_install(
                service_name, exec_start, working_directory, environment, description
            )

    def repair(self, service_name: str, expected_exec: str) -> bool:
        """Repair a drifted service by updating its ExecStart."""
        if self._platform == self.PLATFORM_SYSTEMD:
            return self._systemd_repair(service_name, expected_exec)
        elif self._platform == self.PLATFORM_LAUNCHD:
            return self._launchd_repair(service_name, expected_exec)
        else:
            return self._nohup_repair(service_name, expected_exec)

    def uninstall(self, service_name: str) -> bool:
        """Stop, disable, and remove a service."""
        if self._platform == self.PLATFORM_SYSTEMD:
            return self._systemd_uninstall(service_name)
        elif self._platform == self.PLATFORM_LAUNCHD:
            return self._launchd_uninstall(service_name)
        else:
            return self._nohup_uninstall(service_name)

    def restart(self, service_name: str) -> bool:
        """Restart a service."""
        if self._platform == self.PLATFORM_SYSTEMD:
            return self._systemd_restart(service_name)
        elif self._platform == self.PLATFORM_LAUNCHD:
            return self._launchd_restart(service_name)
        else:
            return self._nohup_restart(service_name)

    def read_command(self, service_name: str) -> dict[str, Any] | None:
        """Read the current ExecStart, working directory, and environment from the service config."""
        if self._platform == self.PLATFORM_SYSTEMD:
            return self._systemd_read_command(service_name)
        elif self._platform == self.PLATFORM_LAUNCHD:
            return self._launchd_read_command(service_name)
        else:
            return self._nohup_read_command(service_name)

    def detect_drift(self, service_name: str, expected: dict[str, Any]) -> list[str]:
        """Compare current service config against expected config, returning drift issues."""
        if self._platform == self.PLATFORM_SYSTEMD:
            return self._systemd_detect_drift(service_name, expected)
        elif self._platform == self.PLATFORM_LAUNCHD:
            return self._launchd_detect_drift(service_name, expected)
        else:
            return self._nohup_detect_drift(service_name, expected)
