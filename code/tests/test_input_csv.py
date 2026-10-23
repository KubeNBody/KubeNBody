# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""The workers read input files the way the API validates them: a header with
spaces after the commas ("n, idBody, m, ...") passed validation but gave the
monolithic worker keys like ' idBody', so the job failed on every retry."""
from __future__ import annotations

import pytest

import worker_monolithic_base as W
from utils.file_parsing import validate_csv_data


def test_worker_reads_a_csv_the_api_accepts(monkeypatch):
    text = "n, idBody, m, x, y, z, vx, vy, vz\n0, 1, 1.0, 0, 0, 0, 0, 0, 0\n1, 2, 2.0, 1, 0, 0, 0, 1, 0\n"
    assert len(validate_csv_data(text)) == 2                     # the API's validation
    monkeypatch.setattr(W, "download_file", lambda bucket, key: text)
    bodies = W.load_bodies_from_csv("b", "k")
    assert [b["idBody"] for b in bodies] == [1, 2] and bodies[1]["vy"] == 1.0


@pytest.mark.parametrize("bad_id", ["4294967297", "-2147483649"])
def test_an_id_outside_int32_is_refused(bad_id):
    """The workers store ids in int32: 4294967297 wrapped to 1, the id of
    another body, and the pair's force was skipped as a self-interaction."""
    text = f"n,idBody,m,x,y,z,vx,vy,vz\n0,1,1.0,0,0,0,0,0,0\n1,{bad_id},1.0,1,0,0,0,0,0\n"
    with pytest.raises(ValueError, match="(?s)row 2.*idBody"):           # the API answers it 400
        validate_csv_data(text)
