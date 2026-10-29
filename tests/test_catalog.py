from pathlib import Path

import pytest

from spylead.catalog import FORMS, SPY_NAME, CatalogEntry, load_catalog
from spylead.config import ConfigError

ROOT = Path(__file__).resolve().parents[1]

# SPEC.md §4.3, in order: name, FRED ID, form, default lag, English title.
SPEC_TABLE = [
    ("M2", "M2SL", "LOG", 55, "M2 money stock"),
    ("Spread_CCC", "BAMLH0A3HYC", "LEVEL", 1, "ICE BofA CCC & lower high-yield option-adjusted spread"),
    ("PMI_Philly", "GACDFSA066MSFRBPHI", "LEVEL", 18, "Philadelphia Fed manufacturing survey, current general activity"),
    (
        "PMI_Philly_Future", "GAFDFSA066MSFRBPHI", "LEVEL", 18,
        "Philadelphia Fed manufacturing survey, future general activity (six months ahead)",
    ),
    ("Spread_10Y2Y", "T10Y2Y", "LEVEL", 1, "10-year minus 2-year Treasury"),
    ("Spread_10Y3M", "T10Y3M", "LEVEL", 1, "10-year minus 3-month Treasury"),
    ("Spread_BAA10Y", "BAA10Y", "LEVEL", 1, "Moody's Baa corporate minus 10-year Treasury"),
    ("Spread_HY", "BAMLH0A0HYM2", "LEVEL", 1, "ICE BofA US high-yield option-adjusted spread"),
    ("Claims", "ICSA", "LOG", 5, "Initial jobless claims"),
    ("Permits", "PERMIT", "LOG", 48, "New private housing units authorized by building permits"),
    ("Hours_Mfg", "AWHMAN", "LOG", 35, "Average weekly hours, manufacturing"),
    ("Orders_Capex", "NEWORDER", "LOG", 56, "New orders, nondefense capital goods excluding aircraft"),
    ("Sentiment", "UMCSENT", "LOG", 60, "University of Michigan consumer sentiment"),
    ("NFCI", "NFCI", "LEVEL", 5, "Chicago Fed National Financial Conditions Index"),
    ("STLFSI", "STLFSI4", "LEVEL", 6, "St. Louis Fed Financial Stress Index"),
    ("VIX", "VIXCLS", "LOG", 1, "CBOE volatility index"),
    ("FedFunds", "DFF", "LEVEL", 1, "Effective federal funds rate"),
    ("OECD_CLI", "USALOLITOAASTSAM", "LOG", 44, "OECD composite leading indicator, amplitude adjusted"),
]

ALPHA = """\
[[series]]
name = "Alpha"
fred_id = "ALPHA7"
form = "LOG"
default_lag_days = 55
title_en = "Alpha title"
title_zh = "甲標題"
"""
BETA = """\
[[series]]
name = "Beta"
fred_id = "BETA7"
form = "LEVEL"
default_lag_days = 48
title_en = "Beta title"
title_zh = "乙標題"
"""


def write(tmp_path, text):
    path = tmp_path / "catalog.toml"
    path.write_text(text, encoding="utf-8")
    return path


def with_beta(old, new):
    assert old in BETA
    return f"{ALPHA}\n{BETA.replace(old, new)}"


def test_committed_catalog_matches_the_spec_table():
    catalog = load_catalog(ROOT / "catalog.toml")
    assert [(e.name, e.fred_id, e.form, e.default_lag_days, e.title_en) for e in catalog] == SPEC_TABLE


def test_committed_catalog_has_chinese_titles():
    for entry in load_catalog(ROOT / "catalog.toml"):
        assert any("一" <= char <= "鿿" for char in entry.title_zh), entry.name


def test_entries_keep_the_file_order(tmp_path):
    assert load_catalog(write(tmp_path, f"{BETA}\n{ALPHA}")) == (
        CatalogEntry("Beta", "BETA7", "LEVEL", 48, "Beta title", "乙標題"),
        CatalogEntry("Alpha", "ALPHA7", "LOG", 55, "Alpha title", "甲標題"),
    )


def test_missing_file(tmp_path):
    path = tmp_path / "catalog.toml"
    with pytest.raises(ConfigError) as caught:
        load_catalog(path)
    assert str(caught.value) == f"{path}: not found"


NAME_RULE = "name must match [A-Za-z0-9_]{1,40}"
ERRORS = [
    pytest.param(with_beta('"Beta"', '"Alpha"'), "entry 2: name duplicates entry 1", id="duplicate-name"),
    pytest.param(with_beta('"Beta"', '"Beta-2"'), f"entry 2: {NAME_RULE}", id="bad-name"),
    pytest.param(with_beta('"Beta"', f'"{"B" * 41}"'), f"entry 2: {NAME_RULE}", id="long-name"),
    pytest.param(with_beta('"Beta"', '""'), f"entry 2: {NAME_RULE}", id="empty-name"),
    pytest.param(with_beta('"Beta"', '"SPY"'), "entry 2: name SPY is reserved", id="reserved-name"),
    pytest.param(with_beta('"BETA7"', '"BETA7&file_type=xml"'), "entry 2: fred_id must match [A-Z0-9_]{1,40}", id="bad-fred-id"),
    pytest.param(with_beta('"LEVEL"', '"LOGARITHM"'), "entry 2: form must be LOG or LEVEL", id="bad-form"),
    pytest.param(with_beta('"LEVEL"', '"level"'), "entry 2: form must be LOG or LEVEL", id="lowercase-form"),
    pytest.param(with_beta('form = "LEVEL"\n', ""), "entry 2: missing key form", id="missing-key"),
    pytest.param(with_beta('form = "LEVEL"', 'form = "LEVEL"\ncolour = "Teal"'), "entry 2: unknown key colour", id="unknown-key"),
    pytest.param(with_beta("= 48", '= "Fifty"'), "entry 2: default_lag_days must be an integer", id="string-lag"),
    pytest.param(with_beta("= 48", "= true"), "entry 2: default_lag_days must be an integer", id="boolean-lag"),
    pytest.param(with_beta("= 48", "= -48"), "entry 2: default_lag_days must be 0 or more", id="negative-lag"),
    pytest.param(with_beta('"乙標題"', "7"), "entry 2: title_zh must be a string", id="number-title"),
    pytest.param('series = ["Teal"]\n', "entry 1: must be a table", id="entry-not-a-table"),
    pytest.param('series = "Teal"\n', "series must be an array", id="series-not-an-array"),
    pytest.param("", "missing key series", id="no-series"),
    pytest.param(f'colour = "Teal"\n{ALPHA}', "unknown key colour", id="unknown-top-level-key"),
]


@pytest.mark.parametrize(("text", "problem"), ERRORS)
def test_errors_name_the_entry_and_echo_no_value(tmp_path, echoed, text, problem):
    path = write(tmp_path, text)
    with pytest.raises(ConfigError) as caught:
        load_catalog(path)
    detail = str(caught.value).removeprefix(f"{path}: ")
    assert detail == problem
    assert echoed(detail, text, rule_words=(*FORMS, SPY_NAME)) == []
