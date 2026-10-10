# Native extraction and search

Thoth requires the `native/thoth_html` Rust/PyO3 extension for HTML text, title
and image extraction, object-search ranking, and institutional SQL planning.
Python provides API and database orchestration. The superseded Python HTML
extractors and ranker have been removed; `THOTH_HTML_BACKEND` and
`THOTH_SEARCH_RANK_BACKEND` no longer select implementations.

## Build and install

Install Rust/Cargo through Rustup and Maturin in Thoth's virtual environment:

```powershell
.\.venv\Scripts\python.exe -m pip install "maturin>=1.8,<2"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\build-native-html.ps1 -Install
```

The script builds an optimized ABI3 wheel in `dist/native`, installs it into
`.venv`, and checks the import. Without `-Install`, it only builds the wheel.
Build, install and import failures stop the script; it does not install an old
wheel after a failed build.

On Linux, with the virtual environment active:

```sh
python -m pip install 'maturin>=1.8,<2'
python -m maturin build --release --manifest-path native/thoth_html/Cargo.toml --out dist/native
python -m pip install --force-reinstall --no-deps dist/native/thoth_html_native-0.1.0-cp311-abi3-*.whl
python -c 'import _thoth_html; print(_thoth_html.IMPLEMENTATION)'
```

Build before launching a new installation. For an existing desktop-managed
runtime, inspect status first and use `scripts/restart-thoth-runtime.ps1` after
installation to reload the module. If Windows rejects installation because a
loaded module is locked, stop the runtime through its supervised controls,
install, and restart through the same controls. See [runtime ownership](runtime_architecture.md).

Missing or incompatible native modules raise an installation error when
extraction or ranking is called; health reports unavailable native backends.

## Verify and benchmark

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_text_extraction.py tests\test_html_native.py tests\test_search_native.py -q
.\.venv\Scripts\python.exe scripts\benchmark-html-extractor.py
.\.venv\Scripts\python.exe scripts\benchmark-search-ranker.py
```

Benchmarks report the current native implementation. Historical Python/native
comparisons are retained as performance evidence, rather than executable
fallback implementations. They measure local kernels, not complete ingestion
or agent-workflow speedups.
