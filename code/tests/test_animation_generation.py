# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Tests for the streaming animation-file generator.

generate_animation_file_from_path writes one snapshot per epoch (particles
[x, y, z, m], centre of mass, in epoch order), parsing one epoch at a time.
"""
import json
import shutil

import numpy as np
import pytest


pytest.importorskip('pandas')

import file_service


@pytest.fixture
def combined_csv(tmp_path):
    rng = np.random.default_rng(5)
    lines = ['epoch,n,idBody,m,x,y,z,vx,vy,vz']
    expected = {}
    for epoch in (10, 20):
        pos = rng.standard_normal((3, 3))
        mass = np.abs(rng.standard_normal(3)) + 0.5
        vel = rng.standard_normal((3, 3))
        expected[epoch] = (pos, mass)
        for i in range(3):
            lines.append(
                f'{epoch},{i},{i},{mass[i]!r},'
                f'{pos[i,0]!r},{pos[i,1]!r},{pos[i,2]!r},'
                f'{vel[i,0]!r},{vel[i,1]!r},{vel[i,2]!r}'
            )
    path = tmp_path / 'combined.csv'
    path.write_text('\n'.join(lines) + '\n')
    return str(path), expected


class TestStreamingAnimationGeneration:
    def test_generates_valid_animation_json(self, combined_csv, tmp_path, monkeypatch):
        csv_path, expected = combined_csv
        captured = tmp_path / 'uploaded.json'

        class FakeBucket:
            def upload_file(self, src, key):
                shutil.copy(src, captured)

        class FakeS3:
            def Bucket(self, name):
                return FakeBucket()

        monkeypatch.setattr(file_service, 'get_s3', lambda: FakeS3())
        monkeypatch.setattr(file_service, 'verify_file_exists', lambda b, k: True)

        key = file_service.generate_animation_file_from_path(42, csv_path)
        assert key == 'job_id/42/animation_data.json'

        data = json.loads(captured.read_text())
        assert data['job_id'] == 42
        assert data['total_snapshots'] == 2
        assert [s['epoch'] for s in data['snapshots']] == [10, 20]

        for snap in data['snapshots']:
            pos, mass = expected[snap['epoch']]
            particles = np.array(snap['particles'])
            assert particles.shape == (3, 4)
            np.testing.assert_allclose(particles[:, :3], pos, rtol=5e-12)
            np.testing.assert_allclose(particles[:, 3], mass, rtol=5e-12)

            total = mass.sum()
            com_expected = (pos * mass[:, None]).sum(axis=0) / total
            np.testing.assert_allclose(snap['center_of_mass'], com_expected, rtol=1e-9)

    def test_size_limit_enforced(self, combined_csv, monkeypatch):
        csv_path, _ = combined_csv
        monkeypatch.setattr(file_service, 'MAX_ANIMATION_FILE_SIZE_MB', 0)
        with pytest.raises(ValueError, match='Animation too large'):
            file_service.generate_animation_file_from_path(42, csv_path)


def test_a_combined_file_in_s3_is_downloaded_and_streamed(combined_csv, tmp_path, monkeypatch):
    csv_path, _ = combined_csv
    calls = []

    class FakeBucket:
        def download_file(self, key, dest):
            calls.append(("download", key))
            shutil.copy(csv_path, dest)

    monkeypatch.setattr(file_service, 'get_s3', lambda: type("S3", (), {"Bucket": lambda self, n: FakeBucket()})())
    monkeypatch.setattr(file_service, 'generate_animation_file_from_path',
                        lambda job_id, path: calls.append(("generate", job_id, open(path).read() == open(csv_path).read())))
    file_service.generate_animation_file_from_s3(7, 'job_id/7/combined_results.csv')
    assert calls == [("download", 'job_id/7/combined_results.csv'), ("generate", 7, True)]
