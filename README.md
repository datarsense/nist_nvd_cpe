# NIST NVD CPE Dictionary

Download CPE Name records from the [NVD CPE API](https://nvd.nist.gov/developers/products). The downloader writes a JSON array to `cpe_dictionary.json` by default.

## Requirements

- Python 3.10 or newer
- Network access to `services.nvd.nist.gov`
- Install dependencies with `python -m pip install -r requirements.txt`

## Usage

Download non-deprecated CPE entries (the default):

```sh
python download_cpe_dictionary.py
```

Include deprecated entries:

```sh
python download_cpe_dictionary.py --include-deprecated
```

Choose a different output file or page size:

```sh
python download_cpe_dictionary.py --output data/cpes.json --page-size 5000
```

An NVD API key is optional. Set `NVD_API_KEY` or pass `--api-key`. Prefer the environment variable so the key is not included in shell history. Requests are sequential, with the NVD-recommended six-second delay between pages. Progress is reported to stderr. If interrupted, the previous destination is left intact.

## Incremental Updates

For an initial population, run the command without date filters. Afterward, export CPE records modified in a UTC time window instead of downloading the entire dictionary again:

```sh
python download_cpe_dictionary.py \
    --last-modified-start-date 2026-10-20T00:00:00Z \\
    --last-modified-end-date 2026-10-22T00:00:00Z \\
    --output cpe_updates.json \\
    --include-deprecated
```

Both timestamps are required together and the end must be later than the start. NVD recommends running automated last-modified updates no more frequently than every two hours. After successfully saving a delta, record its end timestamp as the next update's start timestamp. Apply delta records to the local catalog by `cpeNameId`; use `--include-deprecated` when maintaining deprecation status. A date-filtered run writes only the matching records; it does not merge them with an existing full export.

## Output

The output is a UTF-8 JSON array. Every object contains `vendor`, `product`, `title`, `titles`, `deprecated`, `cpeName`, `part`, and `version`. The English title is used for `title` when available; otherwise the first API title is used. `titles` preserves all localized titles. The downloader also includes the CPE identifier, creation and modification dates, references, and deprecation relationships when the API provides them.

Deprecated records are filtered out by default. Use `--include-deprecated` to retain them. Filtering does not affect API pagination; the output count may therefore be lower than the API's total record count.

The CPE API endpoint returns CPE Names. It is distinct from the NVD CPE Match Criteria API. The downloader starts at `startIndex=0`, follows the API's `resultsPerPage` metadata, and uses the API's default page size unless `--page-size` is specified. Output is written to a temporary file and moved into place only after all pages have been downloaded successfully.

For NVD's full recommendations, see [Start Here](https://nvd.nist.gov/developers/start-here) and [API User Workflows](https://nvd.nist.gov/developers/api-workflows). Applications that share an NVD API key should coordinate requests through one requestor.

## GitHub Actions

To create a downloadable ZIP from GitHub, open the repository's **Actions** tab, select **Download NVD CPE dictionary**, choose whether to include deprecated entries, and run the workflow. After it completes, download `nvd-cpe-dictionary-<run number>` from the run's **Artifacts** section. The artifact contains `cpe_dictionary.zip`, which contains `cpe_dictionary.json`, and is retained for 14 days.

For higher NVD request limits, add an `NVD_API_KEY` repository or environment secret. The workflow also works without the secret, subject to NVD's public rate limit.

## Tests

Run the offline unit tests with:

```sh
python -m unittest discover -s tests -v
```
