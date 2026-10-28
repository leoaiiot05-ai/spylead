import pytest

from spylead.config import Config, ConfigError, load_config

TOKEN = "Sentinel-token-0123456789"
FRED_KEY = "sentinel-fred-key"
TIINGO_KEY = "sentinel-tiingo-key"
WEB = f'[web]\nport = 8090\ntoken = "{TOKEN}"\n'
VALID = f"""\
{WEB}
[fred]
api_key = "{FRED_KEY}"

[tiingo]
api_key = "{TIINGO_KEY}"
"""


def write(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def edit(old, new):
    assert old in VALID
    return VALID.replace(old, new)


def test_valid_file(tmp_path):
    config = load_config(write(tmp_path, VALID))
    assert config == Config(port=8090, token=TOKEN, fred_api_key=FRED_KEY, tiingo_api_key=TIINGO_KEY)
    assert config.empty_keys == ()


def test_repr_hides_the_secrets(tmp_path):
    text = repr(load_config(write(tmp_path, VALID)))
    assert "8090" in text
    for secret in (TOKEN, FRED_KEY, TIINGO_KEY):
        assert secret not in text


def test_byte_order_mark_is_accepted(tmp_path):
    path = tmp_path / "config.toml"
    path.write_bytes(b"\xef\xbb\xbf" + VALID.encode())
    assert load_config(path).port == 8090


def test_token_of_16_characters_is_accepted(tmp_path):
    token = "T" * 16
    assert load_config(write(tmp_path, edit(TOKEN, token))).token == token


def test_empty_api_keys_are_accepted_and_listed(tmp_path):
    config = load_config(write(tmp_path, VALID.replace(FRED_KEY, "").replace(TIINGO_KEY, "")))
    assert (config.fred_api_key, config.tiingo_api_key) == ("", "")
    assert config.empty_keys == ("fred.api_key", "tiingo.api_key")


def test_missing_file(tmp_path):
    path = tmp_path / "config.toml"
    with pytest.raises(ConfigError) as caught:
        load_config(path)
    assert str(caught.value) == f"{path}: not found"


def test_unreadable_path(tmp_path):
    with pytest.raises(ConfigError) as caught:
        load_config(tmp_path)
    assert str(caught.value).startswith(f"{tmp_path}: cannot be read (")


def test_invalid_toml_gives_the_position_only(tmp_path):
    # tomllib itself would quote the illegal character.
    path = write(tmp_path, edit(f'"{TOKEN}"', '"Teal\x01"'))
    with pytest.raises(ConfigError) as caught:
        load_config(path)
    assert str(caught.value) == f"{path}: not valid TOML (at line 3, column 14)"


def test_invalid_utf8(tmp_path):
    path = tmp_path / "config.toml"
    path.write_bytes(edit(TOKEN, "Teal\xe9-0123456789abcdef").encode("latin-1"))
    with pytest.raises(ConfigError) as caught:
        load_config(path)
    assert str(caught.value) == f"{path}: not valid UTF-8"


ERRORS = [
    pytest.param('colour = "Teal"\n' + VALID, "unknown key colour", id="unknown-top-level-key"),
    pytest.param(VALID + '\n[proxy]\nurl = "Teal"\n', "unknown key proxy", id="unknown-table"),
    pytest.param(edit("port = 8090", 'port = 8090\nhost = "Teal"'), "unknown key web.host", id="unknown-key"),
    pytest.param(edit(f'\n[tiingo]\napi_key = "{TIINGO_KEY}"\n', ""), "missing key tiingo", id="missing-table"),
    pytest.param(edit("port = 8090\n", ""), "missing key web.port", id="missing-key"),
    pytest.param(edit(WEB, 'web = "Teal"\n'), "web must be a table", id="string-section"),
    pytest.param(edit("port = 8090", 'port = "Teal"'), "web.port must be an integer", id="string-port"),
    pytest.param(edit("port = 8090", "port = true"), "web.port must be an integer", id="boolean-port"),
    pytest.param(edit("port = 8090", "port = 8090.5"), "web.port must be an integer", id="float-port"),
    pytest.param(edit(f'"{TOKEN}"', "1234567890123456789"), "web.token must be a string", id="integer-token"),
    pytest.param(edit(f'"{FRED_KEY}"', '["Teal"]'), "fred.api_key must be a string", id="array-key"),
    pytest.param(edit("port = 8090", "port = 70000"), "web.port must be between 1 and 65535", id="port-out-of-range"),
    pytest.param(edit("port = 8090", "port = 7497"), "web.port must not be an IB Gateway or TWS API port", id="ib-port"),
    pytest.param(edit(TOKEN, "change-me"), "web.token must be changed from the placeholder", id="placeholder-token"),
    pytest.param(edit(TOKEN, "Teal-fifteen-ch"), "web.token must have at least 16 characters", id="short-token"),
]


@pytest.mark.parametrize(("text", "problem"), ERRORS)
def test_errors_name_the_key_and_echo_no_value(tmp_path, echoed, text, problem):
    path = write(tmp_path, text)
    with pytest.raises(ConfigError) as caught:
        load_config(path)
    detail = str(caught.value).removeprefix(f"{path}: ")
    assert detail == problem
    assert echoed(detail, text) == []
