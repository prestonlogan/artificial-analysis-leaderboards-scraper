# Extraction flow

`src/main.py` loads `config.yaml`, fetches the source data, parses it into rows, and writes a CSV. `--install-browser` installs Chromium and exits without scraping.

## Model comparison pages

For `/models`, `src/components/scraper.py` downloads the page directly and reads the data-manifest paths and keys included in its HTML. It downloads each candidate manifest, decrypts it with AES-GCM, decompresses it with gzip, and embeds the model records in a JSON script for the parser. Browser rendering is not used for this path. URL selection filters are omitted when fetching the complete dataset.

`src/components/parser.py` flattens nested model records into columns. Common fields receive readable headings and appear first. Remaining fields are included in source order. Lists become JSON strings, booleans become lowercase text, and missing values become empty cells.

## Provider leaderboard pages

The scraper launches headless Chromium, checks the navigation response, and waits for relevant page content. It clicks available header buttons before extracting the HTML. The parser reads the rendered table and derives provider names from image attributes where available.

Fetch failures retry with exponential backoff. A missing browser stops the retry loop and reports the installation command.

## Configuration and output

`src/components/config.py` loads YAML settings and applies environment overrides. Configuration, CSV paths, and log paths are relative to the working directory, so run from the project folder.

`src/components/formatter.py` writes CSV rows and optionally formats decimal values using Babel. Timestamped filenames are enabled in the checked-in configuration. The separate `format_data_as_csv` helper performs Pandera validation; the main export uses `write_to_csv` directly.

`src/components/logger.py` writes console messages and `logs/scraper.log`. Check the completion message and resulting CSV to confirm success: the entry point does not currently return a nonzero exit status for every failure.

## Browser storage

The scraper sets `PLAYWRIGHT_BROWSERS_PATH` to the project-root `.browsers/` directory unless an explicit override is present. The installation command inherits the same setting. The GitHub Actions workflow sets that path in its checkout before installing and running Chromium.

## Existing checks

The test suite covers configuration overrides, table parsing, model-record flattening, manifest embedding, retries, browser status messages, CSV formatting, and the main orchestration flow. It uses mocked network and browser calls; passing tests do not establish compatibility with a changed live website.
