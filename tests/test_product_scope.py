import pytest

from upc_mcp.cli import parser


@pytest.mark.parametrize("arguments", [["transactions"], ["auth", "capture", "--endpoint", "ws://127.0.0.1:62000"]])
def test_retired_bill_commands_are_rejected(arguments):
    with pytest.raises(SystemExit) as error:
        parser().parse_args(arguments)
    assert error.value.code == 2
