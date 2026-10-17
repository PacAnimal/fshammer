"""`--max-name-length` lowers the length limit without dropping any name shape.

The two length cases are derived from the cap: one name exactly at it, which must round-trip, and one a
byte past it, which must be refused rather than truncated.
"""
import os
import subprocess
import sys
import tempfile

from conftest import HAMMER, fshammer

NAME_MAX = 255


def at_cap(cap: int) -> tuple[list[str], list[int]]:
    """The two length-sensitive sets as this cap produces them."""
    fshammer.MAX_NAME_LENGTH = cap
    return fshammer.hostile_names(), fshammer.name_lengths()


def test_the_default_is_name_max() -> None:
    """The default run still tests NAME_MAX and one byte past it."""
    names, lengths = at_cap(NAME_MAX)
    assert "n" * 255 in names, "the default run must still write a name of exactly NAME_MAX"
    assert "o" * 256 in names, "and one byte past it, which a store must refuse rather than trim"
    assert lengths == [64, 128, 200, 255], f"the default long_names ladder changed: {lengths}"


def test_a_cap_keeps_both_shapes_and_only_moves_them() -> None:
    """A cap keeps the same names and both length shapes, only shorter."""
    full, _ = at_cap(NAME_MAX)
    capped, lengths = at_cap(120)

    assert len(capped) == len(full), \
        f"a cap must not DROP a name — {len(full)} at NAME_MAX and {len(capped)} at 120, which is the " \
        f"declared-gap behaviour this switch exists to avoid"
    assert "n" * 120 in capped, "a name exactly at the cap must still be written"
    assert "o" * 121 in capped, "and one byte past the cap, or the never-trimmed claim goes untested"
    assert max(len(n) for n in capped) == 121, \
        f"nothing may exceed cap+1 — the longest is {max(len(n) for n in capped)}, so a length is hardcoded"
    assert 120 in lengths, f"long_names must climb to the cap itself, got {lengths}"
    assert max(lengths) == 120, f"and no further: {lengths}"
    print(f"  at a cap of 120: {len(capped)} names, longest 121, ladder {lengths}")


def test_the_cap_is_always_on_the_ladder() -> None:
    """The cap itself is always a rung of the long_names ladder, even between default rungs."""
    for cap in (16, 65, 121, 128, 199, 254, 255):
        _, lengths = at_cap(cap)
        assert max(lengths) == cap, f"a cap of {cap} produced a ladder topping out at {max(lengths)}"
        assert lengths == sorted(set(lengths)), f"the ladder must be sorted and unique: {lengths}"
        assert all(length <= cap for length in lengths), f"a cap of {cap} asked for {lengths}"


def test_no_length_is_hardcoded_in_the_base_list() -> None:
    """Negative control: a hardcoded long name in HOSTILE_NAMES would ignore the cap."""
    over = [f"{len(n)} bytes: {n[:24]!r}…" for n in fshammer.HOSTILE_NAMES if len(n) > 64]
    assert not over, \
        "these are hardcoded long names in HOSTILE_NAMES, which --max-name-length cannot shorten:\n" \
        + "\n".join(over)


def test_the_cap_is_probed_rather_than_trusted() -> None:
    """The cap is probed, and the probe answers False when it could not write, never True."""
    with tempfile.TemporaryDirectory() as directory:
        assert fshammer.probe_the_cap(directory) is True, \
            "a 255-byte name works on the temp filesystem, so the probe must say so — otherwise a cap can " \
            "never be reported as unnecessary"
        assert not os.listdir(directory), \
            f"the probe must clean up after itself, and left {os.listdir(directory)}"

    assert fshammer.probe_the_cap("/nonexistent-fshammer-probe-target") is False, \
        "NEGATIVE CONTROL: a probe that could not write must not report the cap unnecessary"
    print("  a 255-byte name is probed for real, and an unwritable target answers False")


def test_an_out_of_band_cap_is_refused() -> None:
    """An out-of-range cap is rejected at argument parsing."""
    def argv_error(cap: str, extra: list[str]) -> str:
        """The argparse error for this invocation.

        A second bad switch guarantees parsing fails before anything is written, whatever --dir is.
        """
        proc = subprocess.run(
            [sys.executable, str(HAMMER), "--dir", "/tmp", "--max-name-length", cap, *extra],
            capture_output=True, text=True, check=False)
        assert proc.returncode != 0, f"--max-name-length {cap} {' '.join(extra)} was accepted"
        return proc.stderr

    # The ERROR text, not the switch name: argparse prints the whole usage on any failure and the usage
    # lists --max-name-length, so a substring test for the name matches every message there is.
    for cap in ("400", "8", "0", "-1"):
        message = argv_error(cap, [])
        assert f"--max-name-length {cap}: must be between" in message, \
            f"--max-name-length {cap} failed for some other reason: {message.strip()[-300:]}"
    # control: a legal cap passes that check, so the run dies on the second bad switch instead
    message = argv_error("200", ["--declare-gap", "nosuchphase"])
    assert "no phase called" in message, \
        f"the control did not reach the declare-gap validation, so it proves nothing: {message.strip()[-300:]}"
    assert "must be between" not in message, \
        f"a legal cap was rejected, so the guard above proves nothing: {message.strip()[-300:]}"
    print("  400, 8, 0 and -1 are refused by name; 200 reaches the next check instead")

