# main.py
import os
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Literal

import requests
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

app = FastAPI(title="opportunity-api", version="0.4.1")

# =========================================================
# Auth (Admin/User key separation)
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


def identify_role_by_key(
    x_api_key: Optional[str],
) -> Literal["admin", "user", "none", "invalid"]:
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
# Google Suggest (unofficial)
# =========================================================
def fetch_google_suggest(seed: str, hl: str = "ja", timeout: float = 6.0) -> List[str]:
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
    if isinstance(data, list) and len(data) >= 2 and isinstance(data[1], list):
        return [s for s in data[1] if isinstance(s, str)]
    return []


# =========================================================
# Google Custom Search (existing)
# =========================================================
GOOGLE_CSE_ENDPOINT = "https://customsearch.googleapis.com/customsearch/v1"


class CustomSearchProxyRequest(BaseModel):
    q: str
    cx: Optional[str] = None
    num: int = Field(10, ge=1, le=10)
    start: int = Field(1, ge=1, le=91)
    gl: Optional[str] = None
    hl: Optional[str] = None
    safe: Optional[str] = None
    fields: Optional[str] = None


def _resolve_cx(body_cx: Optional[str]) -> str:
    cx = body_cx or os.getenv("GOOGLE_CSE_DEFAULT_CX")
    if not cx:
        raise HTTPException(status_code=400, detail="cx is required")
    return cx


def _shape_cse_response(
    raw: Dict[str, Any], req: CustomSearchProxyRequest, cx_used: str
) -> Dict[str, Any]:
    info = raw.get("searchInformation") or {}
    try:
        total_results = int(info.get("totalResults", "0"))
    except Exception:
        total_results = 0

    items = raw.get("items") or []
    shaped_items = [
        {
            "title": it.get("title"),
            "link": it.get("link"),
            "snippet": it.get("snippet"),
            "displayLink": it.get("displayLink"),
            "formattedUrl": it.get("formattedUrl"),
        }
        for it in items
    ]

    return {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "query": {
            "q": req.q,
            "cx": cx_used,
            "num": req.num,
            "start": req.start,
        },
        "total_results": total_results,
        "search_time": info.get("searchTime"),
        "items": shaped_items,
    }


# =========================================================
# SerpAPI proxy (NEW)
# =========================================================
SERPAPI_ENDPOINT = "https://serpapi.com/search"


class SerpApiProxyRequest(BaseModel):
    q: str
    num: int = Field(5, ge=1, le=20)


# =========================================================
# Endpoints
# =========================================================
@app.get("/healthz")
def healthz():
    return {"ok": True, "service": "opportunity-api", "version": "0.4.1"}


@app.get("/whoami")
def whoami(x_api_key: Optional[str] = Header(default=None, alias=HEADER_NAME)):
    return {
        "admin_api_key_set": bool(os.getenv("ADMIN_API_KEY")),
        "user_api_key_set": bool(os.getenv("USER_API_KEY")),
        "role": identify_role_by_key(x_api_key),
    }


@app.get("/suggest_fetch")
def suggest_fetch(
    seed: str,
    x_api_key: Optional[str] = Header(default=None, alias=HEADER_NAME),
    hl: str = "ja",
    limit: int = 10,
):
    require_user_key(x_api_key)
    suggestions = fetch_google_suggest(seed=seed, hl=hl)
    return {"seed": seed, "suggestions": suggestions[:limit], "source": "google_suggest"}


@app.post("/customsearch_proxy")
def customsearch_proxy(
    body: CustomSearchProxyRequest,
    x_api_key: Optional[str] = Header(default=None, alias=HEADER_NAME),
):
    require_admin_key(x_api_key)

    google_key = _get_env_required("GOOGLE_CSE_API_KEY")
    cx_used = _resolve_cx(body.cx)

    params = {
        "key": google_key,
        "cx": cx_used,
        "q": body.q,
        "num": body.num,
        "start": body.start,
    }

    r = requests.get(GOOGLE_CSE_ENDPOINT, params=params, timeout=20)
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=r.text)

    return JSONResponse(_shape_cse_response(r.json(), body, cx_used))


@app.post("/customsearch_proxy_serpapi")
def customsearch_proxy_serpapi(
    body: SerpApiProxyRequest,
    x_api_key: Optional[str] = Header(default=None, alias=HEADER_NAME),
):
    require_admin_key(x_api_key)

    serp_key = _get_env_required("SERPAPI_KEY")

    params = {
        "engine": "google",
        "q": body.q,
        "num": body.num,
        "api_key": serp_key,
    }

    r = requests.get(SERPAPI_ENDPOINT, params=params, timeout=20)
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=r.text)

    data = r.json()
    items = [
        {
            "title": it.get("title"),
            "link": it.get("link"),
            "snippet": it.get("snippet"),
        }
        for it in data.get("organic_results", [])
    ]

    return {"items": items, "source": "serpapi"}
