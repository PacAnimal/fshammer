"""The shared-file walk: a broken frame must not hide the blocks after it."""
import binascii

from conftest import fshammer


def frame(writer: int, seq: int, body: bytes) -> bytes:
    return fshammer.HEADER.pack(fshammer.MAGIC, writer, seq, len(body), binascii.crc32(body)) + body


def expected(n: int) -> dict:
    return {fshammer.BlockId(0, i): 100 for i in range(n)}


def test_a_clean_file_has_no_problems(tmp_path):
    path = tmp_path / "shared.bin"
    path.write_bytes(b"".join(frame(0, i, bytes([65 + i]) * 100) for i in range(4)))
    assert fshammer.verify_shared(str(path), expected(4)) == []


def test_the_walk_resumes_after_a_torn_block(tmp_path):
    blocks = [frame(0, i, bytes([65 + i]) * 100) for i in range(4)]
    path = tmp_path / "shared.bin"
    path.write_bytes(blocks[0] + blocks[1][:50] + b"interleaved" + blocks[1][50:] + blocks[2] + blocks[3])

    problems = fshammer.verify_shared(str(path), expected(4))

    assert any("block frame broken" in p for p in problems), problems
    assert not any("seq 2," in p or "seq 3," in p for p in problems), "blocks after the tear were reported missing"


def test_a_lost_block_is_still_reported(tmp_path):
    path = tmp_path / "shared.bin"
    path.write_bytes(frame(0, 0, b"A" * 100) + frame(0, 2, b"C" * 100))

    problems = fshammer.verify_shared(str(path), expected(3))

    assert problems == ["block (writer 0 seq 1, 100 bytes) was acknowledged but is not in the file as a whole block"]
