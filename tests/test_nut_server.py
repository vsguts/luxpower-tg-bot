import asyncio

import pytest

from lxbot.nut.server import NutServer, UpsConfig

VARIABLES = {"ups.status": "OL", "battery.charge": "80", "device.model": 'SNA "6000"'}


class Source:
    def __init__(self):
        self.variables: dict[str, str] | None = dict(VARIABLES)

    def __call__(self):
        return self.variables


class Client:
    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer

    async def ask(self, line):
        self.writer.write(f"{line}\n".encode())
        await self.writer.drain()
        return (await asyncio.wait_for(self.reader.readline(), 2)).decode().rstrip("\n")

    async def ask_list(self, line):
        lines = [await self.ask(line)]
        while lines[0].startswith("BEGIN") and not lines[-1].startswith("END"):
            lines.append((await asyncio.wait_for(self.reader.readline(), 2)).decode().rstrip("\n"))
        return lines

    async def close(self):
        self.writer.close()
        await self.writer.wait_closed()


@pytest.fixture
async def source():
    return Source()


@pytest.fixture
async def server(source):
    config = UpsConfig(port=0)
    nut = NutServer(config, source, host="127.0.0.1")
    await nut.start()
    yield nut
    await nut.close()


@pytest.fixture
async def client(server):
    connection = Client(*await asyncio.open_connection("127.0.0.1", server.port))
    yield connection
    await connection.close()


async def test_get_a_variable(client):
    assert await client.ask("GET VAR ups ups.status") == 'VAR ups ups.status "OL"'


async def test_quotes_in_values_are_escaped(client):
    assert await client.ask("GET VAR ups device.model") == 'VAR ups device.model "SNA \\"6000\\""'


async def test_an_unknown_variable_or_ups(client):
    assert await client.ask("GET VAR ups battery.voltage") == "ERR VAR-NOT-SUPPORTED"
    assert await client.ask("GET VAR other ups.status") == "ERR UNKNOWN-UPS"


async def test_list_the_variables(client):
    assert await client.ask_list("LIST VAR ups") == [
        "BEGIN LIST VAR ups",
        'VAR ups battery.charge "80"',
        'VAR ups device.model "SNA \\"6000\\""',
        'VAR ups ups.status "OL"',
        "END LIST VAR ups",
    ]


async def test_list_the_ups(client):
    assert await client.ask_list("LIST UPS") == [
        "BEGIN LIST UPS",
        'UPS ups "LuxPower SNA6000 WPV inverter"',
        "END LIST UPS",
    ]
    assert await client.ask("GET UPSDESC ups") == 'UPSDESC ups "LuxPower SNA6000 WPV inverter"'


async def test_nothing_is_writable_or_commandable(client):
    assert await client.ask_list("LIST RW ups") == ["BEGIN LIST RW ups", "END LIST RW ups"]
    assert await client.ask_list("LIST CMD ups") == ["BEGIN LIST CMD ups", "END LIST CMD ups"]
    for line in ("SET VAR ups ups.status OB", "INSTCMD ups shutdown.return", "FSD ups", "PRIMARY ups"):
        assert await client.ask(line) == "ERR ACCESS-DENIED"


async def test_data_is_stale_without_a_reading(client, source):
    source.variables = None
    assert await client.ask("GET VAR ups ups.status") == "ERR DATA-STALE"
    assert await client.ask("LIST VAR ups") == "ERR DATA-STALE"


async def test_values_follow_the_source(client, source):
    source.variables = {**VARIABLES, "ups.status": "OB LB"}
    assert await client.ask("GET VAR ups ups.status") == 'VAR ups ups.status "OB LB"'


async def test_the_dsm_login(client):
    assert await client.ask("USERNAME monuser") == "OK"
    assert await client.ask("PASSWORD secret") == "OK"
    assert await client.ask("LOGIN ups") == "OK"
    assert await client.ask("GET NUMLOGINS ups") == "NUMLOGINS ups 1"
    assert await client.ask("LOGIN ups") == "ERR ALREADY-LOGGED-IN"
    assert await client.ask("LOGOUT") == "OK Goodbye"
    assert await asyncio.wait_for(client.reader.read(), 2) == b""


async def test_a_wrong_password_is_denied(client):
    assert await client.ask("LOGIN ups") == "ERR USERNAME-REQUIRED"
    await client.ask("USERNAME monuser")
    assert await client.ask("LOGIN ups") == "ERR PASSWORD-REQUIRED"
    await client.ask("PASSWORD guess")
    assert await client.ask("LOGIN ups") == "ERR ACCESS-DENIED"
    assert await client.ask("USERNAME other") == "ERR ALREADY-SET-USERNAME"


async def test_tls_is_declined(client):
    assert await client.ask("STARTTLS") == "ERR FEATURE-NOT-CONFIGURED"


async def test_version_and_unknown_commands(client):
    assert (await client.ask("VER")).startswith("Network UPS Tools upsd")
    assert await client.ask("NETVER") == "1.3"
    assert await client.ask("REBOOT now") == "ERR UNKNOWN-COMMAND"
    assert await client.ask('GET VAR "ups') == "ERR INVALID-ARGUMENT"


async def test_a_quoted_argument_is_one_word(client):
    assert await client.ask('GET VAR "ups" "ups.status"') == 'VAR ups ups.status "OL"'


async def test_an_overlong_line_ends_the_connection(client):
    client.writer.write(b"GET " + b"x" * 5000 + b"\n")
    await client.writer.drain()
    assert await asyncio.wait_for(client.reader.read(), 2) == b""


async def test_the_config_hides_the_password():
    config = UpsConfig(password="hidden")
    assert "hidden" not in repr(config)
    assert (config.user, config.port, config.low_soc) == ("monuser", 3493, 6)


async def test_a_busy_port_is_retried_instead_of_ending_the_task(source, monkeypatch):
    monkeypatch.setattr("lxbot.nut.server.BIND_RETRY", 0.01)
    holder = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = holder.sockets[0].getsockname()[1]
    nut = NutServer(UpsConfig(port=port), source, host="127.0.0.1")
    task = asyncio.create_task(nut.run())
    await asyncio.sleep(0.05)
    assert not task.done()
    holder.close()
    await holder.wait_closed()
    for _ in range(100):
        await asyncio.sleep(0.01)
        if nut._server is not None:
            break
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"GET VAR ups ups.status\n")
    assert await asyncio.wait_for(reader.readline(), 2) == b'VAR ups ups.status "OL"\n'
    writer.close()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_names_and_subcommands_ignore_case(client):
    assert await client.ask("get var UPS ups.status") == 'VAR ups ups.status "OL"'
    assert await client.ask("list ups") == "BEGIN LIST UPS"


async def test_an_apostrophe_is_an_ordinary_character(client):
    assert await client.ask("GET VAR ups it's") == "ERR VAR-NOT-SUPPORTED"


async def test_stopping_closes_a_client_that_never_hangs_up(source):
    nut = NutServer(UpsConfig(port=0), source, host="127.0.0.1")
    task = asyncio.create_task(nut.run())
    for _ in range(100):
        await asyncio.sleep(0.01)
        if nut._server is not None:
            break
    reader, writer = await asyncio.open_connection("127.0.0.1", nut.port)
    writer.write(b"GET VAR ups ups.status\n")
    await reader.readline()
    task.cancel()
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
    assert await asyncio.wait_for(reader.read(), 2) == b""
    writer.close()
