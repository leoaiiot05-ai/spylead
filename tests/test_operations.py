"""Operations (SPEC.md §15, §16.3, §19): the autostart script, run.bat and the README, read as text. No PowerShell runs."""
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
AUTOSTART = ROOT / "scripts" / "install-autostart.ps1"
RUN = ROOT / "run.bat"
README = ROOT / "README.md"
# A path in a command the README gives, by its extension.
COMMAND_PATH = re.compile(r"[\w.\\/-]+\.(?:toml|ps1|bat|md|py)\b")


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def launcher() -> str:
    """The launcher that install-autostart.ps1 writes: its single-quoted here-string."""
    return re.search(r"@'\n(.*?)\n'@", text(AUTOSTART), re.S).group(1)


def section(markdown: str, heading: str) -> str:
    """The text under a ## heading, up to the next one."""
    return re.search(rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", markdown, re.S | re.M).group(1)


def commands() -> list[str]:
    """The lines of the README's code blocks."""
    return [line.strip() for block in re.findall(r"```\n(.*?)```", text(README), re.S) for line in block.splitlines()]


# install-autostart.ps1


@pytest.mark.parametrize("path", [AUTOSTART, RUN], ids=lambda path: path.name)
def test_the_windows_scripts_are_ascii(path):
    # Windows PowerShell 5.1 reads a script without a byte-order mark in the ANSI code page, and cmd in the OEM one.
    assert path.read_bytes().isascii()


def test_the_autostart_script_runs_in_windows_powershell_5_1():
    script = text(AUTOSTART)
    assert script.startswith("#Requires -Version 5.1\n")
    code = "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("#"))
    # PowerShell 7's ??, ?., && and || operators, its ternary, ForEach-Object -Parallel and Join-Path's extra parts.
    assert re.findall(r"\?\?|\?\.|&&|\|\||\s\?\s|-Parallel\b|-AdditionalChildPath", code) == []


def test_the_autostart_registers_a_per_user_logon_task_named_spylead():
    script = text(AUTOSTART)
    assert "$TaskName = 'spylead'" in script
    assert "New-ScheduledTaskTrigger -AtLogOn -User $user" in script
    assert "New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited" in script
    assert "Register-ScheduledTask @task" in script


def test_the_launcher_is_written_under_localappdata():
    script = text(AUTOSTART)
    assert "$LauncherFolder = Join-Path $env:LOCALAPPDATA 'spylead'" in script
    assert "$Launcher = Join-Path $LauncherFolder 'launch.ps1'" in script
    assert "Set-Content -LiteralPath $Launcher -Value (Get-LauncherText)" in script
    assert '-ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Launcher`"' in script


def test_the_launcher_waits_up_to_10_minutes_then_starts_pythonw_in_the_project():
    written = launcher()
    assert "$Project = '__PROJECT__'" in written
    assert "$Deadline = (Get-Date).AddMinutes(10)" in written
    assert "$Python = Join-Path $Project '.venv\\Scripts\\pythonw.exe'" in written
    assert "$Arguments = @('-m', 'spylead', '--config', 'config.toml')" in written
    assert "-WorkingDirectory $Project -WindowStyle Hidden" in written
    # The project folder is the script's parent, and a quote in it is doubled inside the single-quoted string.
    assert "$Project = Split-Path -Parent $PSScriptRoot" in text(AUTOSTART)
    assert """$text.Replace('__PROJECT__', $Project.Replace("'", "''"))""" in text(AUTOSTART)


def test_a_refused_task_falls_back_to_a_startup_shortcut_and_says_so():
    script = text(AUTOSTART)
    assert "$Shortcut = Join-Path ([Environment]::GetFolderPath('Startup')) 'spylead.lnk'" in script
    assert re.search(r"try \{\s*Register-SpyleadTask\s*\} catch \{\s*New-StartupShortcut\s*Write-Host", script)


def test_remove_deletes_the_task_the_shortcut_and_the_launcher():
    script = text(AUTOSTART)
    assert "[switch]$Remove" in script
    assert re.search(r"if \(\$Remove\) \{\s*Remove-Autostart\s*\} else \{\s*Install-Autostart\s*\}", script)
    removal = script[script.index("function Remove-Autostart") :]
    for step in ("Unregister-ScheduledTask -TaskName $TaskName", "Remove-Item -LiteralPath $Shortcut", "Remove-Item -LiteralPath $LauncherFolder -Recurse"):
        assert step in removal


# run.bat


def test_run_bat_starts_the_venv_python_with_config_toml_from_its_own_folder():
    lines = text(RUN).splitlines()
    assert lines[0] == "@echo off"
    assert lines.index('cd /d "%~dp0"') < lines.index('".venv\\Scripts\\python.exe" -m spylead --config config.toml')
    assert "if errorlevel 1 pause" in lines


# README.md


@pytest.mark.parametrize(
    "words",
    [
        "## What it does, and what it does not",
        "fredaccount.stlouisfed.org",
        "tiingo.com",
        ".venv/Scripts/python -m pip install -r pylock.toml",
        "Copy-Item config.example.toml config.toml",
        "## The first Fetch Data",
        "**already up to date**",
        "must stay under 10 minutes on this PC",
        "## Reading the results",
        "Estimated points",
        "Out-of-sample",
        "**The one-sided curve**",
        "**The reference fit**",
        "**Install this site as an app**",
        "**Add to Home Screen**",
        "**Never use Tailscale Funnel, and never open or forward a port to the internet.**",
        "## Autostart",
        "Change Drive Letter and Paths",
        "## Backups",
        ".venv/Scripts/python -m spylead.bench",
        "SPEC.md §16.3",
        "## Limitations",
    ],
)
def test_the_readme_covers_each_topic_of_prompt_10(words):
    assert words in text(README)


def test_the_readme_names_every_status_as_the_ui_shows_it():
    labels = [label for key, label in json.loads(text(ROOT / "spylead" / "static" / "locales" / "en.json")).items() if key.startswith("status.")]
    assert [label for label in labels if f"**{label}**" not in text(README)] == []


def test_the_readme_lists_every_limitation_of_spec_17():
    def items(markdown: str, heading: str) -> int:
        return len(re.findall(r"^\d+\. ", section(markdown, heading), re.M))

    assert items(text(README), "Limitations") == items(text(ROOT / "SPEC.md"), "17. Accepted limitations") == 11


def test_the_readme_serves_the_configured_port_through_tailscale_on_8443():
    port = re.search(r"^port = (\d+)", text(ROOT / "config.example.toml"), re.M).group(1)
    assert f"tailscale serve --bg --https=8443 http://127.0.0.1:{port}" in commands()
    assert f"`tailscale serve --bg --https=8443 http://127.0.0.1:{port}`" in text(ROOT / "SPEC.md")


def test_every_file_the_readmes_commands_name_exists():
    named = {path for line in commands() for path in COMMAND_PATH.findall(line)}
    assert named >= {"pylock.toml", "config.example.toml", "scripts/install-autostart.ps1"}
    # config.toml is the one the owner creates.
    assert sorted(path for path in named - {"config.toml"} if not (ROOT / path.replace("\\", "/").removeprefix("./")).is_file()) == []
