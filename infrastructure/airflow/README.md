# Internal framework install requirements

The Airflow image builds in two stages:

  1. The framework source tree is `COPY`-ed to `/opt/framework` in
     the `Dockerfile` and installed with `pip install -r
     infrastructure/airflow/requirements.txt`.
  2. `requirements.txt` lists only the *runtime* extras — the
     framework itself is installed separately because the
     Docker build context is the project root.
