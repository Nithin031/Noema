"""Optional Windows per-user autostart registration."""

from __future__ import annotations

import os
import getpass
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterable, Optional


AUTOSTART_NAME = "NoemaDaemon"
TASK_SCHEDULER_NAME = "NoemaDaemon"
_TASK_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"


def _key():
    if os.name != "nt":
        raise RuntimeError("Windows user autostart is only available on Windows")
    import winreg

    return winreg, winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run"


def build_user_autostart_command(command: str, working_directory: str) -> str:
    """Wrap a daemon command so Windows launches it hidden from its repo root."""

    powershell = str(
        Path(os.environ.get("SystemRoot", r"C:\Windows"))
        / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    )
    directory = str(Path(working_directory).resolve()).replace("'", "''")
    powershell_script = (
        "$ErrorActionPreference='Stop'; "
        "Set-Location -LiteralPath '{}'; & {}".format(
            directory, command.replace('"', "'")
        )
    )
    return (
        '"{}" -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass '
        '-Command "{}"'.format(powershell, powershell_script)
    )


def install_user_autostart(
    command: Optional[str] = None,
    name: str = AUTOSTART_NAME,
    working_directory: Optional[str] = None,
) -> str:
    """Install an explicit current-user startup command and return it."""

    winreg, root, path = _key()
    command = command or '"{}" -m noema'.format(sys.executable)
    if working_directory:
        command = build_user_autostart_command(command, working_directory)
    with winreg.OpenKey(root, path, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, command)
    return command


def uninstall_user_autostart(name: str = AUTOSTART_NAME) -> bool:
    winreg, root, path = _key()
    try:
        with winreg.OpenKey(root, path, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, name)
        return True
    except FileNotFoundError:
        return False


def build_task_scheduler_xml(
    executable: str,
    arguments: Iterable[str],
    working_directory: str,
    user_id: Optional[str] = None,
    delay_seconds: int = 30,
    restart_interval_seconds: int = 60,
    restart_count: int = 999,
) -> str:
    """Build a per-user logon task with restart-on-failure settings."""

    if delay_seconds < 0 or restart_interval_seconds < 1 or restart_count < 1:
        raise ValueError("invalid Task Scheduler timing settings")
    ET.register_namespace("", _TASK_NS)
    root = ET.Element("{{{}}}Task".format(_TASK_NS), {"version": "1.4"})
    registration = ET.SubElement(root, "{{{}}}RegistrationInfo".format(_TASK_NS))
    ET.SubElement(registration, "{{{}}}Author".format(_TASK_NS)).text = "Noema"
    ET.SubElement(registration, "{{{}}}Description".format(_TASK_NS)).text = (
        "Run the Noema daemon supervisor after interactive user logon and restart it on failure."
    )

    triggers = ET.SubElement(root, "{{{}}}Triggers".format(_TASK_NS))
    trigger = ET.SubElement(triggers, "{{{}}}LogonTrigger".format(_TASK_NS))
    ET.SubElement(trigger, "{{{}}}Enabled".format(_TASK_NS)).text = "true"
    if delay_seconds:
        ET.SubElement(trigger, "{{{}}}Delay".format(_TASK_NS)).text = "PT{}S".format(delay_seconds)

    principals = ET.SubElement(root, "{{{}}}Principals".format(_TASK_NS))
    principal = ET.SubElement(principals, "{{{}}}Principal".format(_TASK_NS), {"id": "Author"})
    if user_id is None:
        domain = os.environ.get("USERDOMAIN", "").strip()
        username = os.environ.get("USERNAME", "").strip() or getpass.getuser()
        user_id = "{}\\{}".format(domain, username) if domain else username
    ET.SubElement(principal, "{{{}}}UserId".format(_TASK_NS)).text = user_id
    ET.SubElement(principal, "{{{}}}LogonType".format(_TASK_NS)).text = "InteractiveToken"
    ET.SubElement(principal, "{{{}}}RunLevel".format(_TASK_NS)).text = "LeastPrivilege"

    settings = ET.SubElement(root, "{{{}}}Settings".format(_TASK_NS))
    for name, value in (
        ("MultipleInstancesPolicy", "IgnoreNew"),
        ("DisallowStartIfOnBatteries", "false"),
        ("StopIfGoingOnBatteries", "false"),
        ("AllowHardTerminate", "true"),
        ("StartWhenAvailable", "true"),
        ("RunOnlyIfNetworkAvailable", "false"),
        ("ExecutionTimeLimit", "PT0S"),
    ):
        ET.SubElement(settings, "{{{}}}{}".format(_TASK_NS, name)).text = value
    restart = ET.SubElement(settings, "{{{}}}RestartOnFailure".format(_TASK_NS))
    ET.SubElement(restart, "{{{}}}Interval".format(_TASK_NS)).text = "PT{}S".format(restart_interval_seconds)
    ET.SubElement(restart, "{{{}}}Count".format(_TASK_NS)).text = str(restart_count)

    actions = ET.SubElement(root, "{{{}}}Actions".format(_TASK_NS), {"Context": "Author"})
    action = ET.SubElement(actions, "{{{}}}Exec".format(_TASK_NS))
    ET.SubElement(action, "{{{}}}Command".format(_TASK_NS)).text = str(Path(executable).resolve())
    ET.SubElement(action, "{{{}}}Arguments".format(_TASK_NS)).text = subprocess.list2cmdline(
        [str(argument) for argument in arguments]
    )
    ET.SubElement(action, "{{{}}}WorkingDirectory".format(_TASK_NS)).text = str(
        Path(working_directory).resolve()
    )
    # schtasks.exe expects the XML file's declared encoding to match the
    # Windows-native UTF-16 file written by install_task_scheduler.
    return '<?xml version="1.0" encoding="UTF-16"?>\n' + ET.tostring(root, encoding="unicode")


def _run_schtasks(arguments: list) -> subprocess.CompletedProcess:
    if os.name != "nt":
        raise RuntimeError("Windows Task Scheduler is only available on Windows")
    try:
        return subprocess.run(
            ["schtasks.exe", *arguments],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        raise RuntimeError("could not execute schtasks.exe") from exc


def install_task_scheduler(
    executable: str,
    arguments: Iterable[str],
    working_directory: str,
    task_name: str = TASK_SCHEDULER_NAME,
) -> str:
    """Install or replace the authoritative per-user daemon task."""

    xml = build_task_scheduler_xml(executable, arguments, working_directory)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-16", suffix=".xml", delete=False) as handle:
            temporary_path = handle.name
            handle.write(xml)
        result = _run_schtasks(["/Create", "/TN", task_name, "/XML", temporary_path, "/F"])
        if result.returncode != 0:
            message = (result.stderr or result.stdout or "Task Scheduler registration failed").strip()
            raise RuntimeError(message)
    finally:
        if temporary_path:
            try:
                Path(temporary_path).unlink()
            except OSError:
                pass
    return task_name


def uninstall_task_scheduler(task_name: str = TASK_SCHEDULER_NAME) -> bool:
    result = _run_schtasks(["/Delete", "/TN", task_name, "/F"])
    if result.returncode == 0:
        return True
    if "does not exist" in (result.stderr or result.stdout or "").casefold():
        return False
    message = (result.stderr or result.stdout or "Task Scheduler removal failed").strip()
    raise RuntimeError(message)


def query_task_scheduler(task_name: str = TASK_SCHEDULER_NAME) -> str:
    result = _run_schtasks(["/Query", "/TN", task_name, "/FO", "LIST", "/V"])
    output = (result.stdout or result.stderr or "").strip()
    if result.returncode != 0:
        raise RuntimeError(output or "Task Scheduler query failed")
    return output
