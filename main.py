import os
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

import requests
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

app = FastAPI(title="opportunity-api", version="0.4.0")

# =========================================================
# Auth (Admin/User key separation)
# =========================================================
# - GPT Actions is good at injecting header keys.
# - We use X-API-KEY header for both admin and user access.
# - Endpoints can require either ADMIN or USER key.
#
# Env:
#   ADMIN_API_KEY  : required for admin-only endpoints (customsearch proxy)
#   USER_API_KEY   : required for user endpoints (suggest_fetch, etc.)
# =========================================================

HEADER_NAME = "X-API-KEY"


def _get_env(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise HTTPException(status_code=500, detail=f"{name} is not set")
    return v


def _is_valid_key(x_api_key: Optional[str], expected: str) -> bool:
    return bool(x_api_key) and x_api_key == expected


def require_admin_key(x_api_key: Optional[str]) -> None:
    expected = _get_env("ADMIN_API_KEY")
    if not _is_valid_key(x_api_key, expected):
        raise HTTPException(status_code=401, detail="Invalid admin API key")


def require_user_key(x_api_key: Optional[str]) -> None:
    expected = _get_env("USER_API_KEY")
    if not _is_valid_key(x_api_key, expected):
        raise HTTPException(status_code=401, detail="Invalid user API key")


def require_any_key(x_api_key: Optional[str]) -> str:
    """
    Accept either admin or user key.
    Returns role: "admin" or "user"
    """
    admin = os.getenv("ADMIN_API_KEY")
    user = os.getenv("USER_API_KEY")
    if not admin:
        raise HTTPException(status_code=500, detail="ADMIN_API_KEY is not set")
    if not user:
        raise HTTPException(status_code=500, detail="USER_API_KEY is not set")

    if _is_valid_key(x_api_key, admin):
        return "admin"
    if _is_valid_key(x_api_key, user):
        return "user"
    raise HTTPException(status_code=401, detail="Invalid API key")


# =========================================================
# Existing feature: Google Suggest (unofficial)
# =========================================================
def fetch_google_suggest(seed: str, hl: str = "ja", timeout: float = 6.0) -> List[str]:
    """
    Google Suggest (unofficial) endpoint.
    Returns a list of suggestion strings.
    """
    url = "https://suggestqueries.google.com/complete/search"
    params = {"client": "firefox", "hl": hl, "q": seed}

    r = requests.get(
        url,
        params=params,
        timeout=timeout,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    r.raise_for_status()

    data = r.json()
    # data is usually: [<query>, [<suggest1>, <suggest2>, ...], ...]
    if isinstance(data, list) and len(data) >= 2 and isinstance(data[1], list):
        return [s for s in data[1] if isinstance(s, str)]
    return []


# =========================================================
# New feature: Google Custom Search proxy (admin-only)
# =========================================================
GOOGLE_CSE_ENDPOINT = "https://customsearch.googleapis.com/customsearch/v1"


class CustomSearchProxyRequest(BaseModel):
    q: str = Field(..., description="Search query (can include operators like site:)")
    cx: Optional[str] = Field(
        None,
        description="Programmable Search Engine ID (optional if GOOGLE_CSE_DEFAULT_CX exists)",
    )
    num: int = Field(10, ge=1, le=10, description="Number of results (1..10)")
    start: int = Field(1, ge=1, le=91, description="Start index (1..91)")
    gl: Optional[str] = Field(None, description="Geolocation (e.g., jp)")
    hl: Optional[str] = Field(None, description="UI language (e.g., ja)")
    safe: Optional[str] = Field(None, description="Safe search (off|active)")
    fields: Optional[str] = Field(None, description="Partial response fields (optional)")


def _require_google_cse_key() -> str:
    key = os.getenv("GOOGLE_CSE_API_KEY")
    if not key:
        raise HTTPException(status_code=500, detail="GOOGLE_CSE_API_KEY is not set")
    return key


def _resolve_cx(body_cx: Optional[str]) -> str:
    cx = body_cx or os.getenv("GOOGLE_CSE_DEFAULT_CX")
    if not cx:
        raise HTTPException(status_code=400, detail="cx is required (or set GOOGLE_CSE_DEFAULT_CX)")
    return cx


def _shape_cse_response(raw: Dict[str, Any], req: CustomSearchProxyRequest, cx_used: str) -> Dict[str, Any]:
    info = raw.get("searchInformation") or {}
    total_results_raw = info.get("totalResults", "0")
    try:
        total_results = int(total_results_raw)
    except Exception:
        total_results = 0

    items = raw.get("items") or []
    shaped_items = []
    for it in items:
        shaped_items.append(
            {
                "title": it.get("title"),
                "link": it.get("link"),
                "snippet": it.get("snippet"),
                "displayLink": it.get("displayLink"),
                "formattedUrl": it.get("formattedUrl"),
            }
        )

    return {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "query": {
            "q": req.q,
            "cx": cx_used,
            "num": req.num,
            "start": req.start,
            "gl": req.gl,
            "hl": req.hl,
            "safe": req.safe,
        },
        "total_results": total_results,
        "search_time": info.get("searchTime"),
        "items": shaped_items,
    }


# =========================================================
# Endpoints
# =========================================================
@app.get("/healthz")
def healthz():
    return {"ok": True, "service": "opportunity-api", "version": "0.4.0"}


@app.get("/whoami")
def whoami(x_api_key: Optional[str] = Header(default=None, alias=HEADER_NAME)):
    """
    Debug endpoint: identifies whether the provided key is admin/user.
    (Safe to keep; remove if you don't want it.)
    """
    role = require_any_key(x_api_key)
    return {"role": role}


@app.get("/suggest_fetch")
def suggest_fetch(
    seed: str,
    x_api_key: Optional[str] = Header(default=None, alias=HEADER_NAME),
    hl: str = "ja",
    limit: int = 10,
):
    """
    User endpoint: Fetch suggestions for a seed keyword.
    Auth: USER_API_KEY via X-API-KEY
    """
    require_user_key(x_api_key)

    try:
        suggestions = fetch_google_suggest(seed=seed, hl=hl)
        if limit and limit > 0:
            suggestions = suggestions[: min(limit, 50)]
        payload = {"seed": seed, "suggestions": suggestions, "source": "google_suggest"}
        return JSONResponse(payload)
    except requests.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Suggest upstream HTTP error: {e}")
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Suggest upstream request error: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {e}")


@app.post("/customsearch_proxy")
def customsearch_proxy(
    body: CustomSearchProxyRequest,
    x_api_key: Optional[str] = Header(default=None, alias=HEADER_NAME),
):
    """
    Admin-only endpoint: Proxy Google Custom Search JSON API.
    Auth: ADMIN_API_KEY via X-API-KEY
    Google key is stored server-side in env GOOGLE_CSE_API_KEY.
    """
    require_admin_key(x_api_key)

    google_key = _require_google_cse_key()
    cx_used = _resolve_cx(body.cx)

    params = {
        "key": google_key,
        "cx": cx_used,
        "q": body.q,
        "num": body.num,
        "start": body.start,
    }
    if body.gl:
        params["gl"] = body.gl
    if body.hl:
        params["hl"] = body.hl
    if body.safe:
        params["safe"] = body.safe
    if body.fields:
        params["fields"] = body.fields

    try:
        r = requests.get(
            GOOGLE_CSE_ENDPOINT,
            params=params,
            timeout=20,
            headers={"User-Agent": "Mozilla/5.0"},
        )
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"CSE upstream request error: {e}")

    if r.status_code != 200:
        try:
            j = r.json()
        except Exception:
            j = {"message": r.text}

        # Keep error readable and actionable for admin debugging
        raise HTTPException(
            status_code=502,
            detail={
                "upstream_status": r.status_code,
                "upstream_error": j,
                "note": "Check: API enabled + billing, key restrictions, cx validity, query params",
            },
        )

    data = r.json()
    return JSONResponse(_shape_cse_response(data, body, cx_used))
