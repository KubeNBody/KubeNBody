# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""The upload parser (utils/file_parsing.py), shared by the v1 and v2 APIs."""
from __future__ import annotations

import pytest

from file_service import input_csv_reader
from utils.file_parsing import convert_to_csv_format, parse_bodies, storage_csv, validate_csv_data
from utils.file_parsing import validate_csv_data as parse


HEADER = "n,idBody,m,x,y,z,vx,vy,vz\n"


def test_uploads_refuse_duplicate_ids_and_non_finite_values():
    ok = HEADER + "1,1,1,0,0,0,0,0,0\n2,2,1,1,0,0,0,0,0\n"
    assert len(parse(ok)) == 2
    with pytest.raises(ValueError, match="Duplicate idBody"):
        parse(HEADER + "1,1,1,0,0,0,0,0,0\n2,1,1,1,0,0,0,0,0\n")
    for bad in ("nan", "inf", "-inf"):
        with pytest.raises(ValueError):
            parse(HEADER + f"1,1,1,{bad},0,0,0,0,0\n2,2,1,1,0,0,0,0,0\n")


def test_header_names_match_case_insensitively():
    """The v2 parser this one replaced accepted "IDBODY". v1 required the exact case."""
    bodies = parse_bodies("N, IDBODY, M, X, Y, Z, VX, VY, VZ\n1, 7, 2.5, 0, 1, 0, 0, 0, 0\n")
    assert bodies == [dict(n=1, idBody=7, m=2.5, x=0.0, y=1.0, z=0.0, vx=0.0, vy=0.0, vz=0.0)]


def test_space_separated_files_convert_to_csv_from_dicts_or_models():
    text = "1.0 0 0 0 0 0 0\n2.0 1 0 0 0 0 0\n"
    assert convert_to_csv_format(parse_bodies(text)) == convert_to_csv_format(validate_csv_data(text))
    assert convert_to_csv_format(parse_bodies(text)).startswith("n,idBody,m,x,y,z,vx,vy,vz")


def test_uploads_are_stored_in_the_columns_the_workers_read():
    """The parser accepts "IDBODY" and space-separated files. The workers and
    the comparison service read stored inputs by the exact column names, so
    such uploads are stored as canonical CSV (a canonical one as uploaded)."""
    canonical = HEADER + "1,1,1,0,0,0,0,0,0\n"
    assert storage_csv(canonical, parse_bodies(canonical)) == canonical
    for upload in ("n, idBody, m, x, y, z, vx, vy, vz\n1, 1, 1, 0, 0, 0, 0, 0, 0\n",
                   "N,IDBODY,M,X,Y,Z,VX,VY,VZ\n1,1,1,0,0,0,0,0,0\n", "1.0 0 0 0 0 0 0\n2.0 1 0 0 0 0 0\n"):
        stored = storage_csv(upload, parse_bodies(upload))
        assert stored.startswith("n,idBody,m,x,y,z,vx,vy,vz")
        assert "idBody" in next(input_csv_reader(stored))
    # and a file stored before this (upper-case header) still reads
    assert next(input_csv_reader("N,IDBODY,M,X,Y,Z,VX,VY,VZ\n1,1,1,0,0,0,0,0,0\n"))["idBody"] == "1"
