# main.py
import os
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Literal

import requests
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

app = FastAPI(title="opportunity-api", version="0.4.0")

# =========================================================
# Auth (Admin/User key separation)
# =========================================================
# We use X-API-KEY header for both admin and user access.
#
# Env:
#   ADMIN_API_KEY  : required for admin-only endpoints (customsearch proxy)
#   USER_API_KEY   : required for user endpoints (suggest_fetch, etc.)
#   GOOGLE_CSE_API_KEY     : required for customsearch proxy
#   GOOGLE_CSE_DEFAULT_CX  : optional default cx
# =========================================================

HEADER_NAME = "X-API-KEY"


def _get_env_optional(name: str) -> Optional[str]:
    v = os.getenv(name)
    if not v:
        return None
    return v


def _get_env_required(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise HTTPException(status_code=500, detail=f"{name} is not set")
    return v


def _is_valid_key(x_api_key: Optional[str], expected: Optional[str]) -> bool:
    return bool(x_api_key) and bool(expected) and x_api_key == expected


def require_admin_key(x_api_key: Optional[str]) -> None:
    expected = _get_env_required("ADMIN_API_KEY")
    if not _is_valid_key(x_api_key, expected):
        raise HTTPException(status_code=401, detail="Invalid admin API key")


def require_user_key(x_api_key: Optional[str]) -> None:
    expected = _get_env_required("USER_API_KEY")
    if not _is_valid_key(x_api_key, expected):
        raise HTTPException(status_code=401, detail="Invalid user API key")


def identify_role_by_key(x_api_key: Optional[str]) -> Literal["admin", "user", "none", "invalid"]:
    """
    Identify role WITHOUT throwing 500 when env is missing.
    - admin/user: key matches env
    - none: no key provided
    - invalid: key provided but doesn't match (or env not set)
    """
    if not x_api_key:
        return "none"

    admin = _get_env_optional("ADMIN_API_KEY")
    user = _get_env_optional("USER_API_KEY")

    if _is_valid_key(x_api_key, admin):
        return "admin"
    if _is_valid_key(x_api_key, user):
        return "user"
    return "invalid"


# =========================================================
# Existing feature: Google Suggest (unofficial)
# =========================================================
def fetch_google_suggest(seed: str, hl: str = "ja", timeout: float = 6.0) -> List[str]:
    """
    Google Suggest (unofficial).
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
    key = _get_env_required("GOOGLE_CSE_API_KEY")
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
    shaped_items: List[Dict[str, Any]] = []
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
    # No auth. Must always be callable for probes.
    return {"ok": True, "service": "opportunity-api", "version": "0.4.0"}


@app.get("/whoami")
def whoami(x_api_key: Optional[str] = Header(default=None, alias=HEADER_NAME)):
    """
    Debug endpoint:
    - NEVER 500 just because env is missing (so you can verify deployment state)
    - Shows whether env keys are set, and whether provided key matches admin/user.
    """
    admin_set = bool(os.getenv("ADMIN_API_KEY"))
    user_set = bool(os.getenv("USER_API_KEY"))
    role = identify_role_by_key(x_api_key)

    return {
        "admin_api_key_set": admin_set,
        "user_api_key_set": user_set,
        "role": role,  # admin | user | none | invalid
    }


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

    params: Dict[str, Any] = {
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
