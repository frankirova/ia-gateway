"""Removal of ANSI escape sequences from decoded log lines.

Colourised services write ``ESC [ 31 m`` and friends around words that matter.
Those bytes cost prompt budget, pollute the templates sent to the model and make
severity classification depend on how the colour happened to be written.

Only real ESC bytes (``\\x1b``) are removed. Textual lookalikes such as the
literal characters ``\\u001b[31m`` or ``^[[31m`` are ordinary text inside a log
message and are preserved untouched, and non-ASCII UTF-8 content survives.

Stripping is idempotent and never raises: a sequence cut short at the end of a
line is dropped with its incomplete tail instead of leaking a stray ESC or
consuming the next line (each line is stripped on its own).
"""

from __future__ import annotations

import re

__all__ = ["strip_ansi"]

ESC = "\x1b"

#: ``ESC ] ... BEL`` or ``ESC ] ... ESC \``; an unterminated OSC runs to the end
#: of the line, because a line is the largest unit the collector ever strips.
_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\|\Z)")
#: ``ESC [ parameter bytes, intermediate bytes, final byte``.
_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
#: A CSI prefix with no final byte: the sequence was cut short.
_CSI_TRUNCATED = re.compile(r"\x1b\[[0-?]*[ -/]*\Z")
#: Two-character escape: ``ESC`` followed by one printable ASCII byte.
_TWO_CHAR = re.compile(r"\x1b[\x20-\x7e]")
#: Anything left is a truncated or unknown escape; drop it to the end of line.
_RESIDUAL = re.compile(r"\x1b[\s\S]*\Z")


def strip_ansi(text: str) -> str:
    """Return ``text`` without ANSI escape sequences.

    Never raises and is idempotent: after one pass no ``ESC`` byte remains.
    """
    if ESC not in text:
        return text
    value = _OSC.sub("", text)
    value = _CSI.sub("", value)
    value = _CSI_TRUNCATED.sub("", value)
    value = _TWO_CHAR.sub("", value)
    return _RESIDUAL.sub("", value)
