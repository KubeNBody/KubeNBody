# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""
Functions for parsing, validating, and converting input files: the one upload
parser of both APIs (v1 routes/jobs.py, v2 api_v2.py).
Supports CSV and space-separated formats.
"""

import codecs
import csv
import io
from typing import Dict, List, Union

from api_schemas import BodyData
from constants import MAX_BODIES_PER_FILE
from logging_config import setup_logging
from shared.validation import validate_unique_ids


logger = setup_logging('utils.file_parsing')

BODY_COLUMNS = ('n', 'idBody', 'm', 'x', 'y', 'z', 'vx', 'vy', 'vz')


def decode_file_content(file_bytes: bytes, filename: str = "file") -> str:
    """Decode a file: by its BOM (UTF-8, UTF-16 LE/BE), else as UTF-8, else
    UTF-16, else Latin-1 (which never fails). Raises ValueError for an empty
    file. Filename is for the log messages."""
    if not file_bytes:
        raise ValueError("File is empty (0 bytes)")

    if file_bytes.startswith(codecs.BOM_UTF8):
        logger.info(f"Detected UTF-8 BOM in {filename}, stripping it")
        try:
            content = file_bytes.decode('utf-8-sig')  # strips the BOM
            logger.info(f"Successfully decoded {filename} as UTF-8 with BOM")
            return content
        except UnicodeDecodeError as e:
            logger.warning(f"UTF-8 BOM present but decode failed: {e}")

    if file_bytes.startswith(codecs.BOM_UTF16_LE):
        logger.info(f"Detected UTF-16 LE BOM in {filename}")
        try:
            content = file_bytes.decode('utf-16-le')
            logger.info(f"Successfully decoded {filename} as UTF-16 LE")
            return content
        except UnicodeDecodeError:
            pass

    if file_bytes.startswith(codecs.BOM_UTF16_BE):
        logger.info(f"Detected UTF-16 BE BOM in {filename}")
        try:
            content = file_bytes.decode('utf-16-be')
            logger.info(f"Successfully decoded {filename} as UTF-16 BE")
            return content
        except UnicodeDecodeError:
            pass

    try:
        content = file_bytes.decode('utf-8')
        logger.info(f"Successfully decoded {filename} as UTF-8")
        return content
    except UnicodeDecodeError as utf8_error:
        logger.debug(f"UTF-8 decode failed for {filename}: {utf8_error}")

        try:
            content = file_bytes.decode('utf-16')
            logger.info(f"Successfully decoded {filename} as UTF-16 (no BOM)")
            return content
        except UnicodeDecodeError:
            pass

        # Last resort: Latin-1 maps every byte, so it cannot fail (but may
        # produce mojibake for other encodings)
        logger.warning(
            f"Decoded {filename} as Latin-1 (fallback). "
            f"If data looks corrupted, please save file as UTF-8."
        )
        return file_bytes.decode('latin-1')


def is_valid_space_separated_line(line: str) -> bool:
    """True if the line holds exactly 7 whitespace-separated numbers."""
    line = line.strip()
    if not line:
        return False

    try:
        values = line.split()
        if len(values) != 7:
            return False

        # Try to parse all values as floats
        for v in values:
            float(v)

        return True
    except (ValueError, TypeError):
        return False


def detect_file_format(file_content: str) -> str:
    """'csv' for a comma-delimited file (with or without a header), 'space' for
    a space- or tab-separated one (single or multiple delimiters, with or
    without scientific notation). ValueError if the format cannot be
    determined or is ambiguous."""
    lines = file_content.strip().splitlines()
    if not lines:
        raise ValueError("File is empty")

    first_line = None
    for line in lines:
        if line.strip():
            first_line = line.strip()
            break

    if not first_line:
        raise ValueError("File contains only empty lines")

    # STAGE 1: Check for CSV with proper headers
    if ',' in first_line:
        lower_first = first_line.lower()
        # Check if header contains expected column names
        has_csv_columns = any(col in lower_first for col in ['n', 'idbody', 'm', 'x', 'y', 'z', 'vx', 'vy', 'vz'])

        if has_csv_columns:
            logger.debug("Detected CSV format (comma-delimited with headers)")
            return 'csv'

        # STAGE 2: CSV without headers (data only with 9 columns)
        # Check if it looks like CSV data: 9 comma-separated numeric values
        values = first_line.split(',')
        if len(values) == 9:
            try:
                # Try to parse first value as number
                float(values[0].strip())
                float(values[2].strip())  # Also check mass column
                logger.debug("Detected CSV format (comma-delimited data without headers)")
                return 'csv'
            except (ValueError, TypeError):
                pass  # Not numeric data, continue checking

    # STAGE 3: Check for space-separated format (7 columns)
    # This handles: single spaces, multiple spaces, tabs, scientific notation, regular decimals
    if is_valid_space_separated_line(first_line):
        # Validate with multiple lines if available (for robustness)
        lines_to_check = min(3, len(lines))
        valid_count = 0

        for i in range(lines_to_check):
            if lines[i].strip() and is_valid_space_separated_line(lines[i]):
                valid_count += 1

        if valid_count >= min(2, lines_to_check):
            logger.debug(f"Detected space-separated format (validated {valid_count} lines)")
            return 'space'

    # STAGE 4: Unable to detect format - provide helpful error
    characteristics = []
    if ',' in first_line:
        characteristics.append("contains commas")
    if '\t' in first_line:
        characteristics.append("contains tabs")

    values_space = first_line.split()
    values_comma = first_line.split(',')
    characteristics.append(f"{len(values_space)} whitespace-separated values")
    if ',' in first_line:
        characteristics.append(f"{len(values_comma)} comma-separated values")

    raise ValueError(
        f"Unable to detect file format. Expected either:\n"
        f"  - CSV: comma-delimited with header row (n,idBody,m,x,y,z,vx,vy,vz)\n"
        f"  - Space-separated: 7 numeric columns (m x y z vx vy vz)\n"
        f"First line characteristics: {', '.join(characteristics)}"
    )


def parse_space_separated_file(file_content: str) -> List[BodyData]:
    """Parse the space-separated file format.

    Expected format: 7 space-separated columns per line
    Columns: m x y z vx vy vz

    Auto-generates n and idBody sequentially (1, 2, 3, ...) regardless of empty lines.
    """
    lines = file_content.strip().splitlines()
    bodies = []
    body_count = 0  # Track actual body count separately from line number

    for line_num, line in enumerate(lines, 1):
        line = line.strip()
        if not line:  # Skip empty lines
            continue

        try:
            # Split on any whitespace (handles multiple spaces, tabs)
            values = line.split()

            if len(values) != 7:
                raise ValueError(f"Expected 7 values, got {len(values)}")
            if body_count >= MAX_BODIES_PER_FILE:
                raise ValueError(f"File exceeds maximum body count ({MAX_BODIES_PER_FILE:,}). "
                                 f"Please split into smaller files.")

            # Increment body count for this valid body
            body_count += 1

            # Parse values - Python's float() handles scientific notation automatically
            body_data = {
                'n': body_count,  # Sequential numbering
                'idBody': body_count,  # Sequential numbering
                'm': float(values[0]),
                'x': float(values[1]),
                'y': float(values[2]),
                'z': float(values[3]),
                'vx': float(values[4]),
                'vy': float(values[5]),
                'vz': float(values[6])
            }

            # Validate using Pydantic model
            body = BodyData(**body_data)
            bodies.append(body)

        except (ValueError, TypeError) as e:
            raise ValueError(f"Invalid data in line {line_num}: {e}")
        except Exception as e:
            raise ValueError(f"Error processing line {line_num}: {e}")

    if not bodies:
        raise ValueError("File contains no valid body data")

    return bodies


def convert_to_csv_format(bodies: List[Union[BodyData, Dict]]) -> str:
    """Convert bodies (BodyData or dicts) to CSV string format.

    This ensures all internal processing continues to use CSV format.
    """
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(BODY_COLUMNS), extrasaction='ignore')
    writer.writeheader()
    for body in bodies:
        writer.writerow(body.model_dump() if isinstance(body, BodyData) else body)
    return output.getvalue()


def storage_csv(file_content: str, bodies: List[Union[BodyData, Dict]]) -> str:
    """The text to store for an accepted upload: the upload itself when it is
    a CSV with exactly the canonical header, otherwise the bodies re-written
    as canonical CSV. The parser also accepts spaces after the commas, headers
    in any case ("IDBODY") and space-separated files, and stored files are
    then read by the exact column names."""
    first = file_content.lstrip().split('\n', 1)[0]
    if first.rstrip('\r') == ','.join(BODY_COLUMNS):
        return file_content
    return convert_to_csv_format(bodies)


def parse_bodies(file_content: str) -> List[Dict]:
    """validate_csv_data, as plain dicts (n, idBody, m, x, y, z, vx, vy, vz)."""
    return [body.model_dump() for body in validate_csv_data(file_content)]


def validate_csv_data(file_content: str) -> List[BodyData]:
    """Validate and parse file content (CSV or space-separated format).

    Automatically detects format and returns parsed body data.
    Supports both CSV (comma-delimited with headers) and space-separated formats.
    """
    try:
        # Detect file format
        file_format = detect_file_format(file_content)
        logger.info(f"Detected file format: {file_format}")

        if file_format == 'space':
            bodies = parse_space_separated_file(file_content)
            logger.info(f"Parsed {len(bodies)} bodies from space-separated format")
            return bodies
        else:
            # Parse CSV format (original logic)
            csv_reader = csv.DictReader(io.StringIO(file_content), skipinitialspace=True)

            # Strip whitespace from field names (header row) and match them to
            # the expected columns case-insensitively ("idbody", "M" ...)
            canonical = {c.lower(): c for c in BODY_COLUMNS}
            csv_reader.fieldnames = [canonical.get(name.strip().lower(), name.strip()) if name else name
                                     for name in csv_reader.fieldnames]

            bodies = []

            for row_num, row in enumerate(csv_reader, 1):
                try:
                    # Skip empty rows
                    if not row or all(v is None or str(v).strip() == '' for v in row.values()):
                        continue

                    # Check body count limit to prevent OOM
                    if len(bodies) >= MAX_BODIES_PER_FILE:
                        raise ValueError(f"File exceeds maximum body count ({MAX_BODIES_PER_FILE:,}). Please "
                                         "split into smaller files.")

                    # Validate required columns exist BEFORE accessing
                    required_cols = set(BODY_COLUMNS)
                    missing = required_cols - set(row.keys())
                    if missing:
                        raise ValueError(f"Missing required columns: {missing}. Expected columns: {required_cols}")

                    # Convert string values to appropriate types, stripping whitespace
                    body_data = {
                        'n': int(row['n'].strip()),
                        'idBody': int(row['idBody'].strip()),
                        'm': float(row['m'].strip()),
                        'x': float(row['x'].strip()),
                        'y': float(row['y'].strip()),
                        'z': float(row['z'].strip()),
                        'vx': float(row['vx'].strip()),
                        'vy': float(row['vy'].strip()),
                        'vz': float(row['vz'].strip())
                    }

                    # Validate using Pydantic model
                    body = BodyData(**body_data)
                    bodies.append(body)

                except (ValueError, TypeError) as e:
                    raise ValueError(f"Invalid data in row {row_num}: {e}")
                except KeyError as e:
                    raise ValueError(f"Missing required column {e} in row {row_num}. Expected columns: n, "
                                     "idBody, m, x, y, z, vx, vy, vz")
                except Exception as e:
                    raise ValueError(f"Error processing row {row_num}: {e}")

            if not bodies:
                raise ValueError("CSV file contains no valid body data")
            validate_unique_ids(b.idBody for b in bodies)

            logger.info(f"Parsed {len(bodies)} bodies from CSV format")
            return bodies

    except Exception as e:
        raise ValueError(f"Failed to parse file: {e}")
