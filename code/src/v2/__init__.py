# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""
V2 Distributed Worker - Redis-Centric Architecture

This module provides a simplified, high-performance distributed worker
for N-body simulations using Redis as the primary data store and
synchronization layer.

Key differences from v1:
- Redis for task queue (Streams), body state, and synchronization
- No separate Merger service (last worker handles merge inline)
- Binary data format (64 bytes/body) for performance
- S3 for periodic snapshots and final results only
- Coordination state lives in Redis. PostgreSQL holds the job record (start,
  progress at snapshot epochs, completion or failure) and is not on the
  per-task path
"""
