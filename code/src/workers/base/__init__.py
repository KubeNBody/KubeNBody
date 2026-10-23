# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Base abstractions for worker architecture."""

from workers.base.compute_backend import ComputeBackend
from workers.base.message_backend import MessageBackend


__all__ = ['ComputeBackend', 'MessageBackend']
