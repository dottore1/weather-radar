# Running the tests

```
tests/test_render.py, tests/test_nowcast.py   # framework-free pipeline, both dev/ and
                                                # custom_components/ copies (parametrized)
tests/test_dev_server.py                       # dev/server.py, a real local HTTP server
tests/test_manifest.py                         # manifest.json structural checks
tests/test_config_flow.py, test_coordinator.py,
tests/test_http_views.py, test_image.py, test_init.py   # the HA integration layer
```

The pipeline/dev-server/manifest tests only need `dev/requirements.txt` +
`requirements-test.txt`'s `pytest`. The HA-layer tests additionally need
`pytest-homeassistant-custom-component`, which pulls in the full
`homeassistant` package — and one of its pinned dependencies
(`lru-dict==1.3.0`) has no prebuilt wheel for Python 3.13 on **any**
platform, so installing it needs either a C compiler or the version
override below. **On Windows, run the HA-layer tests via WSL** (a native
Windows compiler toolchain is a much bigger ask than WSL, which most
already have):

```
wsl --install                              # if not already set up
wsl -d Ubuntu -- bash -lc "curl -LsSf https://astral.sh/uv/install.sh | sh"
wsl -d Ubuntu -- bash -lc "cd /mnt/c/Dev/weather-map && \
  ~/.local/bin/uv venv .venv-test-wsl --python 3.13 && \
  ~/.local/bin/uv pip install --python .venv-test-wsl \
    -r dev/requirements.txt -r requirements-test.txt \
    --override tests/uv-overrides.txt"
wsl -d Ubuntu -- bash -lc "cd /mnt/c/Dev/weather-map && .venv-test-wsl/bin/python -m pytest -v"
```

On Linux/Mac, drop the `wsl -d Ubuntu --` prefix and run the same `uv`/
`pytest` commands directly — no override needed if a compiler is present,
but it's harmless either way.
