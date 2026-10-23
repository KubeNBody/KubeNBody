# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""The vectorised body codec must be byte-for-byte the former struct loop.

binary_to_numpy / numpy_to_binary run on every task (decode the epoch state,
encode the partial). The per-body struct.(un)pack loop they replaced cost about
1 us per body. The reference implementations below are that loop, verbatim.
"""
import math

import numpy as np
import pytest

from v2.binary_format import BODY_SIZE, BODY_STRUCT, binary_to_numpy, numpy_to_binary


def _ref_encode(positions, velocities, masses, ids, start_idx=0):
    buffer = bytearray(len(masses) * BODY_SIZE)
    for i in range(len(masses)):
        BODY_STRUCT.pack_into(buffer, i * BODY_SIZE, start_idx + i, int(ids[i]), float(masses[i]),
                              float(positions[i, 0]), float(positions[i, 1]), float(positions[i, 2]),
                              float(velocities[i, 0]), float(velocities[i, 1]), float(velocities[i, 2]))
    return bytes(buffer)


def _ref_decode(data):
    n = len(data) // BODY_SIZE
    pos, vel = np.zeros((n, 3)), np.zeros((n, 3))
    m, ids = np.zeros(n), np.zeros(n, dtype=np.int32)
    for i in range(n):
        v = BODY_STRUCT.unpack_from(data, i * BODY_SIZE)
        ids[i], m[i] = v[1], v[2]
        pos[i], vel[i] = v[3:6], v[6:9]
        for x in v[2:9]:
            if math.isnan(x) or math.isinf(x):
                raise ValueError("invalid")
    return pos, vel, m, ids


def _random_state(n, seed=0):
    rng = np.random.default_rng(seed)
    return (rng.normal(size=(n, 3)) * 1e3, rng.normal(size=(n, 3)), rng.random(n),
            rng.integers(-2**31, 2**31 - 1, n))


@pytest.mark.parametrize("n", [0, 1, 2, 17, 1001])
@pytest.mark.parametrize("start_idx", [0, 4321])
def test_encode_matches_struct_loop(n, start_idx):
    p, v, m, ids = _random_state(n)
    assert numpy_to_binary(p, v, m, ids, start_idx) == _ref_encode(p, v, m, ids, start_idx)


@pytest.mark.parametrize("n", [0, 1, 2, 17, 1001])
def test_decode_matches_struct_loop(n):
    p, v, m, ids = _random_state(n, seed=3)
    blob = _ref_encode(p, v, m, ids) + b"\x00" * 5          # trailing bytes are ignored
    for got, ref in zip(binary_to_numpy(blob), _ref_decode(blob)):
        assert got.dtype == ref.dtype and got.shape == ref.shape
        assert np.array_equal(got, ref)
        assert got.flags.writeable and got.flags.c_contiguous


@pytest.mark.parametrize("field,where,message", [
    ("m", (1,), "Body 1 has invalid mass: nan"),
    ("p", (2, 1), "Body 2 has invalid y: inf"),
    ("v", (0, 2), "Body 0 has invalid vz: nan"),
])
def test_validation_reports_first_bad_value(field, where, message):
    p, v, m, ids = np.ones((3, 3)), np.ones((3, 3)), np.ones(3), np.arange(3)
    target = {"p": p, "v": v, "m": m}[field]
    target[where] = np.inf if "inf" in message else np.nan
    with pytest.raises(ValueError, match=message.replace("(", r"\(")):
        binary_to_numpy(numpy_to_binary(p, v, m, ids))
    binary_to_numpy(numpy_to_binary(p, v, m, ids), validate=False)   # no raise
