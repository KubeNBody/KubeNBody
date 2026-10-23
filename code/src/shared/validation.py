# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""
Shared validation utilities for input file processing.

Provides validation functions used by both V1 and V2 APIs.
"""

import re


def validate_unique_ids(ids) -> None:
    """Every body needs its own idBody: the comparisons, the final-state
    checks and the V2 partitioning match bodies by it. Raises ValueError naming the first duplicates."""
    seen, dup = set(), []
    for i in ids:
        if i in seen and i not in dup:
            dup.append(i)
        seen.add(i)
    if dup:
        shown = ", ".join(str(d) for d in dup[:5]) + (" ..." if len(dup) > 5 else "")
        raise ValueError(f"Duplicate idBody values ({len(dup)}): {shown}")


def sanitize_file_name(name: str, max_length: int = 255) -> str:
    """The name for storage: alphanumerics, dashes, underscores and dots kept,
    spaces as underscores, cut to max_length ('unnamed_file' if nothing is
    left)."""
    # Remove or replace invalid characters (keep word chars, spaces, dash, underscore, dot)
    sanitized = re.sub(r'[^\w\s\-_.]', '', name)
    # Replace spaces with underscores
    sanitized = sanitized.replace(' ', '_')
    sanitized = re.sub(r'_+', '_', sanitized)
    # Trim underscores from ends and limit length
    sanitized = sanitized.strip('_').strip()[:max_length]
    return sanitized or 'unnamed_file'


