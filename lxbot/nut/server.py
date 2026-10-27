"""A read-only Network UPS Tools server for one UPS, enough for Synology DSM ("Synology UPS server") and upsc.

Protocol: https://networkupstools.org/docs/developer-guide.chunked/net-protocol.html
"""

import asyncio
import contextlib
import logging
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# DSM asks for a UPS named "ups" and logs in as monuser/secret; neither can be changed on its side.
UPS_NAME = "ups"
DESCRIPTION = "LuxPower SNA6000 WPV inverter"
DEFAULT_PORT = 3493
MAX_LINE = 1024
# upsmon keeps its connection and polls every few seconds; a silent one is dead.
IDLE_TIMEOUT = 300.0
WRITE_TIMEOUT = 10.0
BIND_RETRY = 60.0
WRITE_COMMANDS = frozenset({"SET", "INSTCMD", "FSD", "PRIMARY", "MASTER"})


@dataclass(frozen=True)
class UpsConfig:
    port: int = DEFAULT_PORT
    low_soc: int = 6
    user: str = "monuser"
    password: str = field(default="secret", repr=False)


Variables = Callable[[], dict[str, str] | None]


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


# NUT quotes with double quotes and escapes with backslashes; an apostrophe is an ordinary character.
def _split(line: str) -> list[str]:
    lexer = shlex.shlex(line, posix=True)
    lexer.whitespace_split = True
    lexer.quotes = '"'
    lexer.escapedquotes = '"'
    lexer.commenters = ""
    return list(lexer)


# upsd compares UPS names and subcommands without regard to case.
def _is_ups(name: str) -> bool:
    return name.lower() == UPS_NAME


class NutServer:
    def __init__(self, config: UpsConfig, variables: Variables, *, host: str = "0.0.0.0"):
        self._config = config
        self._variables = variables
        self._host = host
        self._server: asyncio.Server | None = None
        self._logins = 0
        # upsmon never hangs up, and Server.wait_closed() waits for every client: they are closed by hand.
        self._clients: set[asyncio.StreamWriter] = set()

    @property
    def port(self) -> int:
        return self._server.sockets[0].getsockname()[1]

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._serve, self._host, self._config.port, limit=MAX_LINE)

    # A busy port must not stop the bot (and its grid alerts) the way an ended background task does.
    async def run(self) -> None:
        while True:
            try:
                await self.start()
                break
            except OSError as exc:
                log.error("NUT server cannot listen on port %d: %s", self._config.port, exc)
                await asyncio.sleep(BIND_RETRY)
        log.info("NUT server listening on port %d", self.port)
        # start_server() already serves. serve_forever() is avoided: cancelled, it waits for every client to
        # hang up before this finally could close them.
        try:
            await asyncio.Event().wait()
        finally:
            await self.close()

    async def close(self) -> None:
        if self._server is None:
            return
        self._server.close()
        for writer in list(self._clients):
            writer.close()
        await self._server.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        session = _Session(self)
        self._clients.add(writer)
        try:
            while True:
                line = await asyncio.wait_for(reader.readline(), IDLE_TIMEOUT)
                if not line:
                    break
                reply, done = session.handle(line.decode(errors="replace").strip())
                if reply:
                    writer.write(reply.encode())
                    # A client that stops reading must not hold its handler (and its login) forever.
                    await asyncio.wait_for(writer.drain(), WRITE_TIMEOUT)
                if done:
                    break
        except (TimeoutError, ConnectionError, ValueError):
            pass
        finally:
            self._clients.discard(writer)
            session.end()
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()


class _Session:
    def __init__(self, server: NutServer):
        self._server = server
        self._user: str | None = None
        self._password: str | None = None
        self._logged_in = False

    def end(self) -> None:
        if self._logged_in:
            self._logged_in = False
            self._server._logins -= 1

    def handle(self, line: str) -> tuple[str, bool]:
        try:
            words = _split(line)
        except ValueError:
            return "ERR INVALID-ARGUMENT\n", False
        if not words:
            return "", False
        command, args = words[0].upper(), words[1:]
        match command:
            case "VER":
                return "Network UPS Tools upsd 2.8.1 (lxbot)\n", False
            case "NETVER" | "PROTVER":
                return "1.3\n", False
            case "HELP":
                return "Commands: HELP VER NETVER GET LIST USERNAME PASSWORD LOGIN LOGOUT STARTTLS\n", False
            case "STARTTLS":
                return "ERR FEATURE-NOT-CONFIGURED\n", False
            case "USERNAME":
                return self._set_credential("_user", args, "USERNAME"), False
            case "PASSWORD":
                return self._set_credential("_password", args, "PASSWORD"), False
            case "LOGIN":
                return self._login(args), False
            case "LOGOUT":
                return "OK Goodbye\n", True
            case "GET":
                return self._get(args), False
            case "LIST":
                return self._list(args), False
            case _ if command in WRITE_COMMANDS:
                return "ERR ACCESS-DENIED\n", False
            case _:
                return "ERR UNKNOWN-COMMAND\n", False

    def _set_credential(self, attribute: str, args: list[str], name: str) -> str:
        if len(args) != 1:
            return "ERR INVALID-ARGUMENT\n"
        if getattr(self, attribute) is not None:
            return f"ERR ALREADY-SET-{name}\n"
        setattr(self, attribute, args[0])
        return "OK\n"

    def _login(self, args: list[str]) -> str:
        config = self._server._config
        if len(args) != 1:
            return "ERR INVALID-ARGUMENT\n"
        if self._logged_in:
            return "ERR ALREADY-LOGGED-IN\n"
        if self._user is None:
            return "ERR USERNAME-REQUIRED\n"
        if self._password is None:
            return "ERR PASSWORD-REQUIRED\n"
        if (self._user, self._password) != (config.user, config.password):
            log.warning("NUT login as %r refused", self._user)
            return "ERR ACCESS-DENIED\n"
        if not _is_ups(args[0]):
            return "ERR UNKNOWN-UPS\n"
        self._logged_in = True
        self._server._logins += 1
        return "OK\n"

    def _get(self, args: list[str]) -> str:
        match [args[0].upper(), *args[1:]] if args else args:
            case ["VAR", ups, name]:
                if not _is_ups(ups):
                    return "ERR UNKNOWN-UPS\n"
                variables = self._server._variables()
                if variables is None:
                    return "ERR DATA-STALE\n"
                if name not in variables:
                    return "ERR VAR-NOT-SUPPORTED\n"
                return f"VAR {UPS_NAME} {name} {_quote(variables[name])}\n"
            case ["UPSDESC", ups]:
                return f"UPSDESC {UPS_NAME} {_quote(DESCRIPTION)}\n" if _is_ups(ups) else "ERR UNKNOWN-UPS\n"
            case ["NUMLOGINS", ups]:
                return f"NUMLOGINS {UPS_NAME} {self._server._logins}\n" if _is_ups(ups) else "ERR UNKNOWN-UPS\n"
            case _:
                return "ERR INVALID-ARGUMENT\n"

    def _list(self, args: list[str]) -> str:
        match [args[0].upper(), *args[1:]] if args else args:
            case ["UPS"]:
                return f"BEGIN LIST UPS\nUPS {UPS_NAME} {_quote(DESCRIPTION)}\nEND LIST UPS\n"
            case ["VAR", ups]:
                if not _is_ups(ups):
                    return "ERR UNKNOWN-UPS\n"
                variables = self._server._variables()
                if variables is None:
                    return "ERR DATA-STALE\n"
                lines = [f"VAR {UPS_NAME} {name} {_quote(value)}\n" for name, value in sorted(variables.items())]
                return f"BEGIN LIST VAR {UPS_NAME}\n{''.join(lines)}END LIST VAR {UPS_NAME}\n"
            case [("RW" | "CMD" | "CLIENT") as kind, ups]:
                if not _is_ups(ups):
                    return "ERR UNKNOWN-UPS\n"
                return f"BEGIN LIST {kind} {UPS_NAME}\nEND LIST {kind} {UPS_NAME}\n"
            case _:
                return "ERR INVALID-ARGUMENT\n"
