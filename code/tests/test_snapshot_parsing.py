# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Tests for the pandas-based combined-CSV snapshot parser: the epochs and
arrays it yields, across chunk boundaries (an epoch spilling from one read
chunk into the next) and with an epoch filter.

Float tolerance: the parser uses float_precision='high' (C-speed), which
matches Python's float() to ~1e-12 relative — far below anything visible
in conservation metrics, but not bit-exact, hence RTOL below.
"""

import numpy as np
import pytest

from utils.snapshot_loader import _parse_csv_to_numpy, iter_epochs_pandas, read_body_frame


pd = pytest.importorskip('pandas')

RTOL = 5e-12


@pytest.fixture
def combined_csv(tmp_path):
    """Combined CSV with 3 epochs x 4 bodies of deterministic values."""
    rng = np.random.default_rng(11)
    lines = ['epoch,n,idBody,m,x,y,z,vx,vy,vz']
    expected = {}
    for epoch in (100, 200, 300):
        pos = rng.standard_normal((4, 3))
        vel = rng.standard_normal((4, 3))
        mass = np.abs(rng.standard_normal(4)) + 0.1
        expected[epoch] = (pos, vel, mass)
        for i in range(4):
            lines.append(
                f'{epoch},{i},{i},{mass[i]!r},'
                f'{pos[i,0]!r},{pos[i,1]!r},{pos[i,2]!r},'
                f'{vel[i,0]!r},{vel[i,1]!r},{vel[i,2]!r}'
            )
    path = tmp_path / 'combined.csv'
    path.write_text('\n'.join(lines) + '\n')
    return str(path), expected


class TestPandasEpochIterator:
    def test_matches_expected_values(self, combined_csv):
        path, expected = combined_csv
        parsed = dict(iter_epochs_pandas(path))

        assert sorted(parsed) == sorted(expected)
        for epoch, (pos, vel, mass) in expected.items():
            p, v, m = parsed[epoch]
            np.testing.assert_allclose(p, pos, rtol=RTOL)
            np.testing.assert_allclose(v, vel, rtol=RTOL)
            np.testing.assert_allclose(m, mass, rtol=RTOL)

    def test_epoch_spanning_chunk_boundary(self, combined_csv):
        path, expected = combined_csv
        # chunksize=3 < 4 bodies/epoch forces every epoch to span chunks
        parsed = dict(iter_epochs_pandas(path, chunksize=3))

        assert sorted(parsed) == sorted(expected)
        for epoch, (pos, vel, mass) in expected.items():
            p, v, m = parsed[epoch]
            np.testing.assert_allclose(p, pos, rtol=RTOL)
            np.testing.assert_allclose(m, mass, rtol=RTOL)

    def test_only_the_requested_epochs(self, combined_csv):
        path, _ = combined_csv
        assert sorted(dict(iter_epochs_pandas(path, epochs={100, 300}))) == [100, 300]
        assert sorted(dict(iter_epochs_pandas(path, chunksize=3, epochs={200, 300}))) == [200, 300]


@pytest.mark.parametrize("header", ["n, idBody, m, x, y, z, vx, vy, vz", "N,IDBODY,M,X,Y,Z,VX,VY,VZ"])
def test_an_input_stored_with_a_spaced_or_upper_case_header_parses_like_a_canonical_one(header):
    """Inputs the API accepted were once stored as uploaded: the comparison's
    initial state and body ids read them with the canonical columns."""
    rows = ["1,7,2.0,0.1,0.2,0.3,1e-3,2e-3,3e-3", "2,3,1.0,-0.1,0.0,0.5,0.0,0.0,-1e-3"]
    canonical = "n,idBody,m,x,y,z,vx,vy,vz\n" + "\n".join(rows) + "\n"
    other = header + "\n" + "\n".join(r.replace(",", ", ") if " " in header else r for r in rows) + "\n"
    for a, b in zip(_parse_csv_to_numpy(canonical), _parse_csv_to_numpy(other)):
        np.testing.assert_array_equal(a, b)
    assert list(read_body_frame(other)["idBody"]) == [7, 3]
