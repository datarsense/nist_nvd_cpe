"""Offline tests for the NVD CPE dictionary downloader."""

from __future__ import annotations

import json
import io
import tempfile
import unittest
from http.client import IncompleteRead
from io import BytesIO
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

import download_cpe_dictionary as downloader


def make_cpe(name: str, *, deprecated: bool = False, titles: list[dict[str, str]] | None = None) -> dict[str, Any]:
    """Build a minimal API CPE record for tests."""
    return {
        "cpeName": name,
        "cpeNameId": f"id-{name.split(':')[4]}",
        "deprecated": deprecated,
        "titles": titles or [{"title": "Example", "lang": "en"}],
    }


def make_page(
    start_index: int,
    total: int,
    *cpe_records: dict[str, Any],
    results_per_page: int | None = None,
) -> dict[str, Any]:
    """Build a paged CPE API response."""
    return {
        "startIndex": start_index,
        "totalResults": total,
        "resultsPerPage": results_per_page or max(len(cpe_records), 1),
        "products": [{"cpe": record} for record in cpe_records],
    }


class SplitCpeNameTests(unittest.TestCase):
    def test_split_and_unescape_components(self) -> None:
        cpe_name = r"cpe:2.3:a:acme\:labs:pro\+duct:1.0:*:*:*:*:*:*:*"
        parts = downloader.split_cpe_name(cpe_name)

        self.assertEqual(parts[3], r"acme\:labs")
        normalized = downloader.normalize_cpe(make_cpe(cpe_name))
        self.assertEqual(normalized["vendor"], "acme:labs")
        self.assertEqual(normalized["product"], "pro+duct")
        self.assertEqual(normalized["version"], "1.0")

    def test_english_title_is_preferred_and_all_titles_are_preserved(self) -> None:
        record = make_cpe(
            "cpe:2.3:a:acme:widget:1:*:*:*:*:*:*:*",
            titles=[{"title": "Titre", "lang": "fr"}, {"title": "Widget", "lang": "en"}],
        )

        normalized = downloader.normalize_cpe(record)

        self.assertEqual(normalized["title"], "Widget")
        self.assertEqual(normalized["titles"], record["titles"])

    def test_first_title_is_fallback_when_english_is_missing(self) -> None:
        normalized = downloader.normalize_cpe(
            make_cpe(
                "cpe:2.3:a:acme:widget:1:*:*:*:*:*:*:*",
                titles=[{"title": "Titre", "lang": "fr"}],
            )
        )

        self.assertEqual(normalized["title"], "Titre")


class DownloadDictionaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.first_record = make_cpe("cpe:2.3:a:acme:active:1:*:*:*:*:*:*:*")
        self.deprecated_record = make_cpe(
            "cpe:2.3:a:acme:old:1:*:*:*:*:*:*:*", deprecated=True
        )
        self.last_record = make_cpe("cpe:2.3:a:acme:last:1:*:*:*:*:*:*:*")
        self.pages = [
            make_page(0, 3, self.first_record, self.deprecated_record, results_per_page=2),
            make_page(2, 3, self.last_record, results_per_page=2),
        ]

    def test_deprecated_records_are_excluded_by_default_without_skipping_pages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "dictionary.json"
            with patch.object(downloader, "fetch_page", side_effect=self.pages) as fetch_page:
                written, total = downloader.download_dictionary(
                    output_path, page_size=2, request_delay=0
                )

            records = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual([item.args[0] for item in fetch_page.call_args_list], [0, 2])
        self.assertEqual((written, total), (2, 3))
        self.assertEqual([record["product"] for record in records], ["active", "last"])
        self.assertTrue(all(not record["deprecated"] for record in records))

    def test_include_deprecated_flag_keeps_deprecated_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "dictionary.json"
            with patch.object(downloader, "fetch_page", side_effect=self.pages):
                written, total = downloader.download_dictionary(
                    output_path,
                    page_size=2,
                    include_deprecated=True,
                    request_delay=0,
                )

            records = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual((written, total), (3, 3))
        self.assertEqual([record["product"] for record in records], ["active", "old", "last"])
        self.assertTrue(records[1]["deprecated"])

    def test_empty_dictionary_writes_an_empty_array(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "dictionary.json"
            with patch.object(downloader, "fetch_page", return_value=make_page(0, 0)):
                written, total = downloader.download_dictionary(output_path, request_delay=0)

            self.assertEqual(output_path.read_text(encoding="utf-8"), "[]\n")
        self.assertEqual((written, total), (0, 0))

    def test_failed_download_does_not_replace_existing_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "dictionary.json"
            output_path.write_text("existing data", encoding="utf-8")
            with patch.object(
                downloader,
                "fetch_page",
                side_effect=[self.pages[0], RuntimeError("network unavailable")],
            ):
                with self.assertRaisesRegex(RuntimeError, "network unavailable"):
                    downloader.download_dictionary(output_path, page_size=2, request_delay=0)

            self.assertEqual(output_path.read_text(encoding="utf-8"), "existing data")

    def test_cli_include_deprecated_option_is_applied(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "dictionary.json"
            with patch.object(downloader, "fetch_page", side_effect=self.pages):
                with patch.object(downloader.sys, "stderr", new_callable=io.StringIO):
                    with patch.object(downloader.time, "sleep"):
                        exit_code = downloader.main(
                            [
                                "--output",
                                str(output_path),
                                "--page-size",
                                "2",
                                "--include-deprecated",
                            ]
                        )

            records = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual([record["product"] for record in records], ["active", "old", "last"])


class FetchPageTests(unittest.TestCase):
    def test_retries_a_truncated_response_body(self) -> None:
        truncated_response = MagicMock()
        truncated_response.__enter__.return_value.read.side_effect = IncompleteRead(
            b"partial", 5
        )
        complete_response = BytesIO(b'{"startIndex":0,"totalResults":0,"products":[]}')

        with patch.object(
            downloader, "urlopen", side_effect=[truncated_response, complete_response]
        ) as urlopen:
            with patch.object(downloader.time, "sleep") as sleep:
                page = downloader.fetch_page(0, 100, max_retries=1)

        self.assertEqual(page["totalResults"], 0)
        self.assertEqual(urlopen.call_count, 2)
        self.assertEqual(sleep.call_args_list, [call(1)])

    def test_truncated_response_error_is_clear_after_retries_exhaust(self) -> None:
        truncated_response = MagicMock()
        truncated_response.__enter__.return_value.read.side_effect = IncompleteRead(
            b"partial", 5
        )

        with patch.object(downloader, "urlopen", return_value=truncated_response):
            with patch.object(downloader.time, "sleep"):
                with self.assertRaisesRegex(RuntimeError, "body was truncated"):
                    downloader.fetch_page(0, 100, max_retries=0)

    def test_retries_rate_limit_using_retry_after_header(self) -> None:
        throttled = HTTPError(
            downloader.API_URL,
            429,
            "Too Many Requests",
            {"Retry-After": "2"},
            BytesIO(b""),
        )
        response = BytesIO(b'{"startIndex":0,"totalResults":0,"products":[]}')

        with patch.object(downloader, "urlopen", side_effect=[throttled, response]) as urlopen:
            with patch.object(downloader.time, "sleep") as sleep:
                page = downloader.fetch_page(0, 100, api_key="test-key")

        self.assertEqual(page["totalResults"], 0)
        self.assertEqual(urlopen.call_count, 2)
        self.assertEqual(sleep.call_args_list, [call(2.0)])
        request = urlopen.call_args_list[0].args[0]
        self.assertEqual(request.get_header("Apikey"), "test-key")

    def test_modified_date_window_is_sent_in_utc_query_parameters(self) -> None:
        response = BytesIO(b'{"startIndex":0,"totalResults":0,"products":[]}')
        with patch.object(downloader, "urlopen", return_value=response) as urlopen:
            downloader.fetch_page(
                0,
                100,
                last_modified_start_date="2026-10-20T00:00:00Z",
                last_modified_end_date="2026-10-22T00:00:00+00:00",
            )

        query = parse_qs(urlparse(urlopen.call_args.args[0].full_url).query)
        self.assertEqual(query["lastModStartDate"], ["2026-10-20T00:00:00.000Z"])
        self.assertEqual(query["lastModEndDate"], ["2026-10-22T00:00:00.000Z"])

    def test_modified_date_window_requires_both_utc_dates_in_order(self) -> None:
        with self.assertRaisesRegex(ValueError, "both last-modified"):
            downloader.download_dictionary(
                "unused.json",
                last_modified_start_date="2026-10-20T00:00:00Z",
                request_delay=0,
            )

        with self.assertRaisesRegex(ValueError, "must be after"):
            downloader.download_dictionary(
                "unused.json",
                last_modified_start_date="2026-10-22T00:00:00Z",
                last_modified_end_date="2026-10-20T00:00:00Z",
                request_delay=0,
            )


if __name__ == "__main__":
    unittest.main()