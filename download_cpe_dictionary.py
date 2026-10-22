"""Download and normalize the NVD CPE dictionary."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from http.client import IncompleteRead
from pathlib import Path
from typing import Any, TextIO
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

load_dotenv()

API_URL = "https://services.nvd.nist.gov/rest/json/cpes/2.0"
DEFAULT_PAGE_SIZE = 10_000
MAX_PAGE_SIZE = 10_000
DEFAULT_OUTPUT = "cpe_dictionary.json"
REQUEST_DELAY = 6.0
MAX_RETRIES = 5
TIMEOUT_SECONDS = 60


def split_cpe_name(cpe_name: str) -> list[str]:
    """Split a CPE 2.3 formatted name without splitting escaped colons."""
    components: list[str] = []
    current: list[str] = []
    escaped = False

    for character in cpe_name:
        if character == "\\":
            current.append(character)
            if escaped:
                escaped = False
            else:
                escaped = True
        elif character == ":" and not escaped:
            components.append("".join(current))
            current = []
        else:
            current.append(character)
            escaped = False

    components.append("".join(current))
    if len(components) < 6 or components[:2] != ["cpe", "2.3"]:
        raise ValueError(f"Unsupported CPE name: {cpe_name!r}")
    return components


def _unescape_component(component: str) -> str:
    """Decode backslash escapes in a CPE formatted-string component."""
    return re.sub(r"\\(.)", r"\1", component)


def normalize_cpe(cpe_record: dict[str, Any]) -> dict[str, Any]:
    """Return a flat, JSON-ready dictionary record from an API CPE object."""
    cpe_name = cpe_record.get("cpeName")
    if not isinstance(cpe_name, str):
        raise TypeError("CPE API product is missing a string cpeName")

    components = split_cpe_name(cpe_name)
    titles = cpe_record.get("titles", [])
    if not isinstance(titles, list):
        titles = []

    valid_titles = [
        title
        for title in titles
        if isinstance(title, dict) and isinstance(title.get("title"), str)
    ]
    title = next(
        (entry["title"] for entry in valid_titles if entry.get("lang") == "en"),
        valid_titles[0]["title"] if valid_titles else "",
    )

    normalized: dict[str, Any] = {
        "vendor": _unescape_component(components[3]),
        "product": _unescape_component(components[4]),
        "title": title,
        "titles": titles,
        "deprecated": bool(cpe_record.get("deprecated", False)),
        "cpeName": cpe_name,
        "part": _unescape_component(components[2]),
        "version": _unescape_component(components[5]),
    }

    for field in ("cpeNameId", "created", "lastModified", "refs", "deprecatedBy", "deprecates"):
        if field in cpe_record:
            normalized[field] = cpe_record[field]

    return normalized


def _normalize_modified_window(
    start_date: str | None, end_date: str | None
) -> tuple[str | None, str | None]:
    """Validate and normalize an optional UTC last-modified window."""
    if start_date is None and end_date is None:
        return None, None
    if start_date is None or end_date is None:
        raise ValueError("both last-modified start and end dates are required")

    def parse_utc(value: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
        except ValueError as error:
            raise ValueError("last-modified dates must use ISO-8601 UTC format") from error
        if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
            raise ValueError("last-modified dates must use UTC (Z or +00:00)")
        return parsed.astimezone(timezone.utc)

    start = parse_utc(start_date)
    end = parse_utc(end_date)
    if end <= start:
        raise ValueError("last-modified end date must be after start date")

    def format_utc(value: datetime) -> str:
        return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    return format_utc(start), format_utc(end)


def _retry_http_error(error: HTTPError, attempt: int, max_retries: int) -> None:
    """Wait before retrying a throttled or temporarily unavailable API."""
    retryable = error.code in (403, 429) or 500 <= error.code < 600
    if not retryable or attempt == max_retries:
        raise error

    retry_after = error.headers.get("Retry-After") if error.headers else None
    try:
        delay = float(retry_after) if retry_after else 2**attempt
    except ValueError:
        delay = 2**attempt
    time.sleep(min(max(delay, 0.0), 60.0))


def _read_response(request: Request, timeout: float, max_retries: int) -> bytes:
    """Read a response body with bounded retries for transient failures."""
    for attempt in range(max_retries + 1):
        try:
            with urlopen(request, timeout=timeout) as response:
                return response.read()
        except HTTPError as error:
            _retry_http_error(error, attempt, max_retries)
        except IncompleteRead as error:
            if attempt == max_retries:
                raise RuntimeError(
                    "NVD CPE API response body was truncated after retries"
                ) from error
            time.sleep(min(2**attempt, 60))
        except (URLError, TimeoutError):
            if attempt == max_retries:
                raise
            time.sleep(min(2**attempt, 60))
    raise RuntimeError("NVD CPE API request failed after retries")


def fetch_page(
    start_index: int,
    results_per_page: int,
    api_key: str | None = None,
    timeout: float = TIMEOUT_SECONDS,
    max_retries: int = MAX_RETRIES,
    last_modified_start_date: str | None = None,
    last_modified_end_date: str | None = None,
) -> dict[str, Any]:
    """Fetch one CPE API page, retrying throttling and transient failures."""
    last_modified_start_date, last_modified_end_date = _normalize_modified_window(
        last_modified_start_date, last_modified_end_date
    )
    parameters = {"startIndex": start_index, "resultsPerPage": results_per_page}
    if last_modified_start_date and last_modified_end_date:
        parameters["lastModStartDate"] = last_modified_start_date
        parameters["lastModEndDate"] = last_modified_end_date
    query = urlencode(parameters)
    headers = {"User-Agent": "nist-nvd-cpe-dictionary-downloader/1.0"}
    if api_key:
        headers["apiKey"] = api_key
    request = Request(f"{API_URL}?{query}", headers=headers)
    payload = json.loads(_read_response(request, timeout, max_retries).decode("utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("NVD CPE API response must be a JSON object")
    return payload


def _write_record(output: TextIO, record: dict[str, Any], first_record: bool) -> None:
    """Append one normalized record to the open JSON array."""
    if not first_record:
        output.write(",")
    json.dump(record, output, ensure_ascii=False, separators=(",", ":"))


def _validate_page(
    page: dict[str, Any], start_index: int, expected_total: int | None
) -> tuple[list[Any], int, int]:
    """Validate pagination metadata and return products, total, and page size."""
    page_total = page.get("totalResults")
    products = page.get("products")
    returned_start = page.get("startIndex", start_index)
    returned_page_size = page.get("resultsPerPage", len(products) if isinstance(products, list) else 0)
    if not isinstance(page_total, int) or page_total < 0:
        raise ValueError("NVD CPE API response has invalid totalResults")
    if not isinstance(products, list):
        raise TypeError("NVD CPE API response has invalid products")
    if not isinstance(returned_page_size, int) or returned_page_size < 1:
        if page_total > 0:
            raise ValueError("NVD CPE API response has invalid resultsPerPage")
        returned_page_size = 1
    if returned_start != start_index:
        raise ValueError("NVD CPE API returned an unexpected startIndex")
    if expected_total is not None and page_total != expected_total:
        raise RuntimeError("NVD CPE dictionary changed during download; rerun to get a consistent snapshot")
    if not products and start_index < page_total:
        raise RuntimeError("NVD CPE API returned an empty page before all results were retrieved")
    return products, page_total, returned_page_size


def _write_products(
    output: TextIO,
    products: list[Any],
    first_record: bool,
    include_deprecated: bool,
) -> tuple[bool, int]:
    """Normalize and append records from one API page."""
    written_count = 0
    for product_entry in products:
        if not isinstance(product_entry, dict) or not isinstance(product_entry.get("cpe"), dict):
            raise TypeError("NVD CPE API response contains an invalid product entry")
        cpe_record = product_entry["cpe"]
        if not include_deprecated and cpe_record.get("deprecated", False):
            continue
        _write_record(output, normalize_cpe(cpe_record), first_record)
        first_record = False
        written_count += 1
    return first_record, written_count


def _download_pages(
    output: TextIO,
    *,
    api_key: str | None,
    page_size: int,
    include_deprecated: bool,
    request_delay: float,
    last_modified_start_date: str | None,
    last_modified_end_date: str | None,
) -> tuple[int, int]:
    """Fetch all pages and stream the selected records into an open file."""
    output.write("[")
    first_record = True
    start_index = 0
    total_results: int | None = None
    written_count = 0
    fetched_count = 0

    while total_results is None or start_index < total_results:
        page = fetch_page(
            start_index,
            page_size,
            api_key=api_key,
            last_modified_start_date=last_modified_start_date,
            last_modified_end_date=last_modified_end_date,
        )
        products, page_total, returned_page_size = _validate_page(
            page, start_index, total_results
        )
        total_results = page_total
        first_record, page_written = _write_products(
            output, products, first_record, include_deprecated
        )
        written_count += page_written
        fetched_count += len(products)
        start_index += returned_page_size

        print(
            f"Fetched {fetched_count:,}/{total_results:,} CPE records; wrote {written_count:,}.",
            file=sys.stderr,
            flush=True,
        )
        if start_index < total_results and request_delay:
            time.sleep(request_delay)

    output.write("]\n")
    return written_count, total_results or 0


def download_dictionary(
    output_path: str | Path = DEFAULT_OUTPUT,
    *,
    api_key: str | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    include_deprecated: bool = False,
    request_delay: float | None = None,
    last_modified_start_date: str | None = None,
    last_modified_end_date: str | None = None,
) -> tuple[int, int]:
    """Download all CPE pages and atomically write the selected records.

    Returns the number of records written and the API's total result count.
    """
    if not 1 <= page_size <= MAX_PAGE_SIZE:
        raise ValueError(f"page_size must be between 1 and {MAX_PAGE_SIZE}")

    delay = request_delay
    if delay is None:
        delay = REQUEST_DELAY
    if delay < 0:
        raise ValueError("request_delay must not be negative")
    modified_start, modified_end = _normalize_modified_window(
        last_modified_start_date, last_modified_end_date
    )

    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as output:
            temporary_path = Path(output.name)
            written_count, total_results = _download_pages(
                output,
                api_key=api_key,
                page_size=page_size,
                include_deprecated=include_deprecated,
                request_delay=delay,
                last_modified_start_date=modified_start,
                last_modified_end_date=modified_end,
            )
            output.flush()
            os.fsync(output.fileno())

        os.replace(temporary_path, destination)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass

    return written_count, total_results


def build_argument_parser() -> argparse.ArgumentParser:
    """Create the command-line argument parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-o",
        "--output",
        default=DEFAULT_OUTPUT,
        help=f"output JSON file (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=DEFAULT_PAGE_SIZE,
        help=f"records per API request, from 1 to {MAX_PAGE_SIZE}",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("NVD_API_KEY"),
        help="NVD API key (or set NVD_API_KEY)",
    )
    parser.add_argument(
        "--include-deprecated",
        action="store_true",
        help="include deprecated CPE entries (excluded by default)",
    )
    parser.add_argument(
        "--last-modified-start-date",
        help="export records modified on or after this UTC ISO-8601 timestamp",
    )
    parser.add_argument(
        "--last-modified-end-date",
        help="export records modified on or before this UTC ISO-8601 timestamp",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the downloader command-line interface."""
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    try:
        written, total = download_dictionary(
            args.output,
            api_key=args.api_key,
            page_size=args.page_size,
            include_deprecated=args.include_deprecated,
            last_modified_start_date=args.last_modified_start_date,
            last_modified_end_date=args.last_modified_end_date,
        )
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        print(f"Download failed: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted; the destination was not replaced.", file=sys.stderr)
        return 130

    print(f"Wrote {written:,} CPE records from {total:,} API records to {args.output}.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())