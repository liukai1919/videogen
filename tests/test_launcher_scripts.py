"""Encoding rules for the launcher scripts.

Both of these bit us on a Chinese Windows and neither shows up as an encoding
error — they surface as nonsense syntax errors in a file that reads fine in any
editor, so they are worth a test rather than a comment.
"""

from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
UTF8_BOM = b"\xef\xbb\xbf"


@pytest.mark.parametrize("script", sorted(ROOT.glob("*.ps1")), ids=lambda p: p.name)
def test_powershell_scripts_carry_a_utf8_bom(script: Path) -> None:
    # Windows PowerShell 5.1 reads a BOM-less .ps1 in the ANSI code page (GBK
    # here), and the mis-decoded comments swallow quotes and braces: the parser
    # then reports "字符串缺少终止符" on a line that is perfectly fine.
    assert script.read_bytes().startswith(UTF8_BOM), (
        f"{script.name} 需要 UTF-8 BOM，否则 PowerShell 5.1 会按 GBK 读它"
    )


@pytest.mark.parametrize("script", sorted(ROOT.glob("*.cmd")), ids=lambda p: p.name)
def test_batch_scripts_are_pure_ascii(script: Path) -> None:
    # cmd.exe reads a .cmd in the console's OEM code page, and a UTF-8 Chinese
    # comment re-read as GBK can produce a 0x26 "&" — which ends the rem and
    # runs the rest of the line as a command.
    data = script.read_bytes()
    offenders = [byte for byte in data if byte > 127]
    assert not offenders, f"{script.name} 只能用 ASCII，发现 {len(offenders)} 个高位字节"


def test_the_shell_script_has_no_carriage_returns() -> None:
    # .gitattributes pins this to LF; a CRLF copy fails inside bash with
    # "$'\r': command not found".
    assert b"\r" not in (ROOT / "start.sh").read_bytes()
