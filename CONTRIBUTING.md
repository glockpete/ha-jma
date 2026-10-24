# Contributing

Thanks for helping. Bug reports and pull requests are welcome.

## Reporting a problem

Open an issue and fill in the form. Attach the diagnostics file (Settings, Devices & services, JMA Weather
(Unofficial), three dots, **Download diagnostics**); it hides your location. Please do not post coordinates.

## Running the tests

The tests use [pytest-homeassistant-custom-component](https://github.com/MatthewFlamm/pytest-homeassistant-custom-component)
and the real files of each service saved in `tests/fixtures`, so they need no network.

```bash
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements_test.txt ruff
pytest tests -q
ruff check .
```

The Home Assistant version that `pytest-homeassistant-custom-component` pulls in decides which Python you need;
`.github/workflows/tests.yml` shows the one that is used in CI.

## The tsunami coast table

`custom_components/jma/tsunami_coast_data.py` is generated: JMA's list of the coastal municipalities of each tsunami
forecast area (津波予報区), by class20 code. Do not edit it by hand. When JMA publishes a new list, or the area list
gets a new class20 code:

```bash
pip install pdfplumber pyshp numpy
python scripts/build_tsunami_coast.py --fetch-rows tests/fixtures/tsunami_shichouson_rows.json   # saves the table of the PDF
python scripts/build_tsunami_coast.py --write                                                  # makes the module (fetches area.json)
python scripts/check_tsunami_coast_gis.py --area tests/fixtures/area.json                      # compares with JMA's GIS data
```

The script stops when a name of the PDF fits no class20 area, or fits several: add nothing to the table by hand,
change the rule in `resolve_name` or say in the pull request what the PDF changed. `tests/test_tsunami_coast.py`
makes the table again from the saved rows and `tests/fixtures/area.json` and fails if the module differs.

## Pull requests

- Keep a change small and say what it fixes in the description.
- Add or change a test. A fix for a feed that changed shape needs a sample of the new file in `tests/fixtures`
  and a line in `tests/fixtures/README.md` saying where it came from.
- Parsers (`parse_*.py`) import nothing from Home Assistant; keep it that way.
- Keep the service credits (`attribution`) and the README table of data sources correct when a feed is added.
- Update `CHANGELOG.md`.
