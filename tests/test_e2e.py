import pytest

from canav import e2e


@pytest.mark.parametrize(
    "data, expected",
    [
        # AUTOSAR SWS CRC Library, CRC8 SAE J1850 check values
        ("00000000", 0x59),
        ("F20183", 0x37),
        ("0FAA0055", 0x79),
        ("00FF5511", 0xB8),
        ("332255AABBCCDDEEFF", 0xCB),
        ("926B55", 0x8C),
        ("FFFFFFFF", 0x74),
        (b"123456789".hex(), 0x4B),  # CRC-8/SAE-J1850 catalogue check value
    ],
)
def test_crc8_known_vectors(data, expected):
    assert e2e.crc8_j1850(bytes.fromhex(data)) == expected


def test_layout():
    assert e2e.layout(8) == (6, 7)
    assert e2e.layout(4) == (2, 3)
    assert e2e.data_bits(8) == 48 and e2e.data_bits(4) == 16
    with pytest.raises(ValueError):
        e2e.layout(6)


@pytest.mark.parametrize("dlc", [4, 8])
def test_protect_check_roundtrip(dlc):
    payload = bytes(range(0x10, 0x10 + dlc))
    prev = None
    for k in range(40):
        counter = k % 16
        frame = e2e.protect(0x123, payload, counter)
        # signal bytes and the counter byte's high nibble untouched
        assert frame[: dlc - 2] == payload[: dlc - 2]
        assert frame[dlc - 2] >> 4 == payload[dlc - 2] >> 4
        res = e2e.check(0x123, frame, prev)
        assert res.ok and res.counter == counter
        prev = res.counter


def test_check_detects_corruption_and_counter_errors():
    frame = e2e.protect(0x200, bytes(8), 5)
    bad = bytearray(frame)
    bad[0] ^= 0x01
    assert not e2e.check(0x200, bytes(bad)).crc_ok
    # DataID is part of the CRC: same payload under another ID fails
    assert not e2e.check(0x201, frame).crc_ok
    assert not e2e.check(0x200, frame, last_counter=5).counter_ok  # repeated
    assert not e2e.check(0x200, frame, last_counter=3).counter_ok  # one lost
    assert e2e.check(0x200, frame, last_counter=3, max_delta=2).counter_ok
    assert e2e.check(0x200, e2e.protect(0x200, bytes(8), 0), last_counter=15).ok  # wrap
    assert e2e.next_counter(15) == 0
