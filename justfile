venv := ".venv"
python := venv + "/bin/python"

# Generate the dashboard and serve it locally
serve: _venv
    {{python}} generate.py
    {{python}} -m http.server 8080 --directory docs

# Install deps into venv (creates venv if missing)
_venv:
    #!/usr/bin/env bash
    if [ ! -d "{{venv}}" ]; then
        python3 -m venv {{venv}}
    fi
    {{python}} -m pip install -q -r requirements.txt
