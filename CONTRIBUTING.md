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

## Pull requests

- Keep a change small and say what it fixes in the description.
- Add or change a test. A fix for a feed that changed shape needs a sample of the new file in `tests/fixtures`
  and a line in `tests/fixtures/README.md` saying where it came from.
- Parsers (`parse_*.py`) import nothing from Home Assistant; keep it that way.
- Keep the service credits (`attribution`) and the README table of data sources correct when a feed is added.
- Update `CHANGELOG.md`.
