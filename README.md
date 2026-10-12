# Employee Attendance & Analytics API

FastAPI service for employee records, IST-aware punch-in/out, audited attendance corrections, and MongoDB aggregation-based analytics. Contract and storage details are in [openapi.yaml](openapi.yaml) and [DATA_MODEL.md](DATA_MODEL.md).

## Setup and run

Requirements: Python 3.11+ and MongoDB 6.0+ reachable from this machine.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="attendance_db"
uvicorn app.main:app --port 8000
```

In VS Code, select `.venv/bin/python` as the workspace interpreter after setup.

The defaults are `mongodb://localhost:27017` and `attendance_db`. A local `.env` may be used; exported environment variables take precedence. The service creates its indexes at startup. Readiness is at `http://localhost:8000/health`; interactive API docs are at `http://localhost:8000/docs`.

To load the supplied sample documents into a dedicated database, set `MONGO_URI` and `MONGO_DB`, then run:

```bash
python sample_seed.py
```

`sample_seed.py` replaces the contents of both collections in the selected database. Do not point it at data you need to keep.

Run the local unit suite with:

```bash
python -m unittest discover -s tests -v
```

## Notes

Analytics read stored derived attendance values. Department trends use a MongoDB-generated calendar series and rolling window. The explain endpoint reports MongoDB `executionStats`. No credentials, `.env`, Dockerfile, or database dump are included.
