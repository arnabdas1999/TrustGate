"""CP0 STUB so `docker compose up` starts. Naman replaces this file.

The real gate holds the only Flagsmith organisation key, runs allowlisted reads, and runs a
write only with a valid, single-use, signed approval (see the plan's "Where the agent stops
to ask"). Until then every Flagsmith endpoint answers 501.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, status

app = FastAPI(title="TrustGate gate (stub)", version="0.0.1")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "mode": "stub"}


@app.post("/gate/read")
def read() -> None:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "gate not implemented yet (Naman)")


@app.post("/gate/write")
def write() -> None:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "gate not implemented yet (Naman)")


@app.get("/gate/fingerprint")
def fingerprint() -> None:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "gate not implemented yet (Naman)")
