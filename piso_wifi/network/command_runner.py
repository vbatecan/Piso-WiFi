"""Command execution subsystem for network operations.

Provides an abstraction over system subprocess calls with support for timeouts,
logging, error handling, and mock runners for deterministic testing.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import logging
import shutil
import subprocess
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class CommandResult:
    """Encapsulates the result of an executed shell command."""
    command: str
    returncode: int
    stdout: str
    stderr: str

    @property
    def success(self) -> bool:
        """True if the command finished with exit code 0."""
        return self.returncode == 0

    def __str__(self) -> str:
        return self.stdout


class CommandRunner(ABC):
    """Abstract interface for running shell commands."""

    @abstractmethod
    def run(
        self,
        command: str,
        ignore_errors: bool = False,
        timeout: Optional[float] = 15.0,
    ) -> CommandResult:
        """Execute command and return structured CommandResult.

        Raises:
            subprocess.CalledProcessError: If returncode != 0 and ignore_errors is False.
            subprocess.TimeoutExpired: If command execution exceeds timeout and ignore_errors is False.
        """
        pass

    def execute(
        self,
        command: str,
        ignore_errors: bool = False,
        timeout: Optional[float] = 15.0,
    ) -> str:
        """Execute command and return stdout as string for compatibility."""
        result = self.run(command, ignore_errors=ignore_errors, timeout=timeout)
        return result.stdout

    def command_exists(self, cmd: str) -> bool:
        """Check if an executable exists on the system PATH."""
        return shutil.which(cmd) is not None


class SystemCommandRunner(CommandRunner):
    """Real system command execution using standard library subprocess."""

    def __init__(self, default_timeout: float = 15.0):
        self.default_timeout = default_timeout

    def run(
        self,
        command: str,
        ignore_errors: bool = False,
        timeout: Optional[float] = None,
    ) -> CommandResult:
        timeout_val = timeout if timeout is not None else self.default_timeout
        logger.debug("Executing system command: %s", command)

        try:
            res = subprocess.run(
                command,
                shell=True,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout_val,
            )
            stdout = res.stdout or ""
            stderr = res.stderr or ""

            if stdout:
                logger.debug("Command output: %s", stdout.strip())
            if stderr:
                logger.debug("Command stderr: %s", stderr.strip())

            if res.returncode != 0 and not ignore_errors:
                logger.error(
                    "Command failed with code %d: %s\nStderr: %s",
                    res.returncode,
                    command,
                    stderr.strip(),
                )
                raise subprocess.CalledProcessError(
                    res.returncode, command, output=stdout, stderr=stderr
                )

            return CommandResult(
                command=command,
                returncode=res.returncode,
                stdout=stdout,
                stderr=stderr,
            )

        except subprocess.TimeoutExpired as exc:
            logger.error("Command timed out after %ss: %s", timeout_val, command)
            if ignore_errors:
                return CommandResult(
                    command=command,
                    returncode=-1,
                    stdout="",
                    stderr=f"TimeoutExpired after {timeout_val} seconds",
                )
            raise


class MockCommandRunner(CommandRunner):
    """Mock command runner for testing network components without root or hardware."""

    def __init__(
        self,
        default_stdout: str = "",
        default_stderr: str = "",
        default_returncode: int = 0,
    ):
        self.executed_commands: List[str] = []
        self.commands: List[str] = self.executed_commands  # backward alias
        self.responses: Dict[str, CommandResult] = {}
        self.default_stdout = default_stdout
        self.default_stderr = default_stderr
        self.default_returncode = default_returncode
        self.handlers: List[Callable[[str], Optional[CommandResult]]] = []
        self.available_commands: set = {
            "hostapd",
            "dnsmasq",
            "iw",
            "ip",
            "iptables",
            "which",
            "systemctl",
            "rfkill",
            "nmcli",
            "arp",
            "tc",
            "killall",
        }

    def register_response(
        self,
        command_pattern: str,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
    ) -> None:
        """Register a canned response for any command containing command_pattern."""
        self.responses[command_pattern] = CommandResult(
            command=command_pattern,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )

    def register_handler(
        self, handler: Callable[[str], Optional[CommandResult]]
    ) -> None:
        """Register a dynamic callback handler that can evaluate arbitrary commands."""
        self.handlers.append(handler)

    def command_exists(self, cmd: str) -> bool:
        """Simulate whether an executable command exists."""
        return cmd in self.available_commands

    def run(
        self,
        command: str,
        ignore_errors: bool = False,
        timeout: Optional[float] = None,
    ) -> CommandResult:
        self.executed_commands.append(command)

        # Check custom handlers
        for handler in self.handlers:
            result = handler(command)
            if result is not None:
                if result.returncode != 0 and not ignore_errors:
                    raise subprocess.CalledProcessError(
                        result.returncode, command, output=result.stdout, stderr=result.stderr
                    )
                return result

        # Check registered response patterns
        for pattern, canned in self.responses.items():
            if pattern in command:
                if canned.returncode != 0 and not ignore_errors:
                    raise subprocess.CalledProcessError(
                        canned.returncode, command, output=canned.stdout, stderr=canned.stderr
                    )
                return CommandResult(
                    command=command,
                    returncode=canned.returncode,
                    stdout=canned.stdout,
                    stderr=canned.stderr,
                )

        # Default configured response
        if self.default_returncode != 0 and not ignore_errors:
            raise subprocess.CalledProcessError(
                self.default_returncode,
                command,
                output=self.default_stdout,
                stderr=self.default_stderr,
            )

        return CommandResult(
            command=command,
            returncode=self.default_returncode,
            stdout=self.default_stdout,
            stderr=self.default_stderr,
        )

    def clear(self) -> None:
        """Reset recorded commands and custom handlers."""
        self.executed_commands.clear()
        self.responses.clear()
        self.handlers.clear()
