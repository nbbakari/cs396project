"""Client for the EPA Clean Air Markets Program Data (CAMPD) API.

Two streaming endpoints are used for one retrieval:

* ``emissions/apportioned/annual`` -- one row per unit-year with operating
  data, emissions, fuels, controls and program codes;
* ``facilities/attributes`` -- the same unit-years with county, latitude,
  longitude, source category, commercial operation date and operating status,
  which the emissions endpoint does not return.

The two are merged on (facilityId, unitId, year) into a single table in the
emissions endpoint's camelCase format, which the validation pipeline already
understands.

The API key is read from the server configuration and sent only in the
``x-api-key`` request header. It is never placed in a URL, stored, logged, or
returned to the browser. The API allows roughly one request per second per key,
so calls are spaced and a 429 response is retried with backoff.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlencode

import pandas as pd
from flask import current_app

ANNUAL_EMISSIONS_PATH = "/streaming-services/emissions/apportioned/annual"
FACILITY_ATTRIBUTES_PATH = "/streaming-services/facilities/attributes"

# Attribute columns merged onto the emissions rows.
ATTRIBUTE_COLUMNS = (
    "county",
    "latitude",
    "longitude",
    "sourceCategory",
    "commercialOperationDate",
    "retirementDate",
)
MERGE_KEYS = ["facilityId", "unitId", "year"]

# Where users request a free CAMPD API key.
API_KEY_SIGNUP_URL = "https://www.epa.gov/power-sector/cam-api-portal#request-api-key"
# A tiny query (one facility, one year) used to check a key before saving it.
KEY_CHECK_YEAR = 2023

MIN_INTERVAL_SECONDS = 1.1
MAX_ATTEMPTS = 4
REQUEST_TIMEOUT_SECONDS = 300

# Choices offered on the retrieval form. Values must match CAMPD's spelling
# exactly, since the API filters on equality.
STATES = [
    ("AL", "Alabama"), ("AK", "Alaska"), ("AZ", "Arizona"), ("AR", "Arkansas"),
    ("CA", "California"), ("CO", "Colorado"), ("CT", "Connecticut"),
    ("DE", "Delaware"), ("DC", "District of Columbia"), ("FL", "Florida"),
    ("GA", "Georgia"), ("HI", "Hawaii"), ("ID", "Idaho"), ("IL", "Illinois"),
    ("IN", "Indiana"), ("IA", "Iowa"), ("KS", "Kansas"), ("KY", "Kentucky"),
    ("LA", "Louisiana"), ("ME", "Maine"), ("MD", "Maryland"),
    ("MA", "Massachusetts"), ("MI", "Michigan"), ("MN", "Minnesota"),
    ("MS", "Mississippi"), ("MO", "Missouri"), ("MT", "Montana"),
    ("NE", "Nebraska"), ("NV", "Nevada"), ("NH", "New Hampshire"),
    ("NJ", "New Jersey"), ("NM", "New Mexico"), ("NY", "New York"),
    ("NC", "North Carolina"), ("ND", "North Dakota"), ("OH", "Ohio"),
    ("OK", "Oklahoma"), ("OR", "Oregon"), ("PA", "Pennsylvania"),
    ("PR", "Puerto Rico"), ("RI", "Rhode Island"), ("SC", "South Carolina"),
    ("SD", "South Dakota"), ("TN", "Tennessee"), ("TX", "Texas"), ("UT", "Utah"),
    ("VT", "Vermont"), ("VA", "Virginia"), ("WA", "Washington"),
    ("WV", "West Virginia"), ("WI", "Wisconsin"), ("WY", "Wyoming"),
]

FUEL_TYPES = [
    "Coal", "Coal Refuse", "Diesel Oil", "Liquified Petroleum Gas",
    "Natural Gas", "Other Gas", "Other Oil", "Other Solid Fuel",
    "Petroleum Coke", "Pipeline Natural Gas", "Process Gas", "Process Sludge",
    "Residual Oil", "Tire Derived Fuel", "Waste", "Wood",
]

UNIT_TYPES = [
    "Arch-fired boiler", "Bubbling fluidized bed boiler", "Cell burner boiler",
    "Circulating fluidized bed boiler", "Combined cycle", "Combustion turbine",
    "Cyclone boiler", "Dry bottom turbo-fired boiler",
    "Dry bottom vertically-fired boiler", "Dry bottom wall-fired boiler",
    "Integrated gasification combined cycle", "Internal combustion engine",
    "Other boiler", "Other turbine", "Pressurized fluidized bed boiler",
    "Process Heater", "Stoker", "Tangentially-fired",
    "Wet bottom turbo-fired boiler", "Wet bottom vertically-fired boiler",
    "Wet bottom wall-fired boiler",
]

CONTROL_TECHNOLOGIES = [
    # SO2
    "Wet Limestone", "Wet Lime FGD", "Dry Lime FGD", "Dry Sorbent Injection",
    "Fluidized Bed Limestone Injection", "Sodium Based", "Magnesium Oxide",
    "Dual Alkali",
    # NOx
    "Selective Catalytic Reduction", "Selective Non-catalytic Reduction",
    "Dry Low NOx Burners", "Low NOx Burner Technology (Dry Bottom only)",
    "Low NOx Burner Technology w/ Overfire Air",
    "Low NOx Burner Technology w/ Closed-coupled OFA",
    "Low NOx Burner Technology w/ Separated OFA",
    "Low NOx Burner Technology w/ Closed-coupled/Separated OFA",
    "Overfire Air", "Water Injection", "Steam Injection", "Ammonia Injection",
    "Combustion Modification/Fuel Reburning",
    # PM
    "Electrostatic Precipitator", "Baghouse", "Wet Scrubber", "Cyclone",
    "Wet ESP",
]


class CampdError(RuntimeError):
    """A retrieval failed; the message is safe to show to users.

    ``status`` is the HTTP status the API answered with, when it answered.
    """

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


@dataclass
class Retrieval:
    frame: pd.DataFrame
    query_params: dict[str, str]
    source_url: str
    emission_rows: int
    attribute_rows: int = 0
    elapsed_seconds: float = 0.0
    warnings: list[str] = field(default_factory=list)


_throttle_lock = threading.Lock()
_last_request_at = 0.0


def _throttle() -> None:
    """Space requests at least MIN_INTERVAL_SECONDS apart, process-wide."""
    global _last_request_at
    with _throttle_lock:
        wait = MIN_INTERVAL_SECONDS - (time.monotonic() - _last_request_at)
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()


def _error_detail(error: urllib.error.HTTPError) -> str:
    try:
        body = json.loads(error.read().decode("utf-8", errors="replace"))
    except ValueError:
        return error.reason or "no detail"
    if isinstance(body, dict):
        inner = body.get("error")
        if isinstance(inner, dict) and inner.get("message"):
            return str(inner["message"])[:300]
        if body.get("message"):
            message = body["message"]
            return (", ".join(message) if isinstance(message, list) else str(message))[:300]
    return error.reason or "no detail"


def endpoint_url(path: str) -> str:
    return current_app.config["CAMPD_API_BASE_URL"].rstrip("/") + path


def _get_json(
    path: str, params: dict[str, str], api_key: Optional[str] = None
) -> list[dict[str, Any]]:
    api_key = api_key or current_app.config.get("CAMPD_API_KEY")
    if not api_key:
        raise CampdError(
            "No CAMPD API key is set. Add your key on the Settings page "
            "(or as CAMPD_API_KEY in .env)."
        )

    url = f"{endpoint_url(path)}?{urlencode(params)}"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        _throttle()
        request = urllib.request.Request(
            url, headers={"x-api-key": api_key, "Accept": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            if error.code == 429 and attempt < MAX_ATTEMPTS:
                time.sleep(2**attempt)
                continue
            raise CampdError(
                f"The CAMPD API returned HTTP {error.code}: {_error_detail(error)}",
                status=error.code,
            ) from None
        except urllib.error.URLError as error:
            raise CampdError(f"Could not reach the CAMPD API: {error.reason}") from None
        except TimeoutError:
            raise CampdError("The CAMPD API did not respond in time.") from None
        except ValueError:
            raise CampdError("The CAMPD API returned a response that is not JSON.") from None

        if not isinstance(payload, list):
            raise CampdError("The CAMPD API returned an unexpected response shape.")
        return payload

    raise CampdError("The CAMPD API rate limit was exceeded; try again in a minute.")


def verify_api_key(api_key: str) -> None:
    """Make one small request with a candidate key; raise CampdError if it fails."""
    _get_json(
        FACILITY_ATTRIBUTES_PATH, {"year": str(KEY_CHECK_YEAR), "facilityId": "3"}, api_key
    )


def build_query(
    year: int,
    *,
    state: Optional[str] = None,
    facility_ids: Optional[list[int]] = None,
    fuel_type: Optional[str] = None,
    unit_type: Optional[str] = None,
    control_technology: Optional[str] = None,
) -> dict[str, str]:
    """Translate retrieval-form filters into CAMPD query parameters."""
    params = {"year": str(year)}
    if state:
        params["stateCode"] = state.upper()
    if facility_ids:
        # CAMPD accepts several values for one parameter separated by "|".
        params["facilityId"] = "|".join(str(i) for i in facility_ids)
    if fuel_type:
        params["unitFuelType"] = fuel_type
    if unit_type:
        params["unitType"] = unit_type
    if control_technology:
        params["controlTechnologies"] = control_technology
    return params


def with_retirement_date(attributes: pd.DataFrame) -> pd.DataFrame:
    """Derive a retirementDate column from CAMPD's operatingStatus text."""
    attributes = attributes.copy()
    if "operatingStatus" in attributes.columns:
        # e.g. "Operating (Retired 06/01/2020)" -> "06/01/2020"
        attributes["retirementDate"] = (
            attributes["operatingStatus"]
            .astype("string")
            .str.extract(r"Retired\s*\(?(\d{1,2}/\d{1,2}/\d{4})", expand=False)
        )
    return attributes


def merge_attributes(emissions: pd.DataFrame, attributes: pd.DataFrame) -> pd.DataFrame:
    """Left-join facility attributes onto emissions rows by unit-year."""
    attributes = with_retirement_date(attributes)
    keep = MERGE_KEYS + [c for c in ATTRIBUTE_COLUMNS if c in attributes.columns]
    attributes = attributes[keep].drop_duplicates(subset=MERGE_KEYS)
    emissions = emissions.drop(
        columns=[c for c in ATTRIBUTE_COLUMNS if c in emissions.columns]
    )
    return emissions.merge(attributes, on=MERGE_KEYS, how="left")


def fetch_attributes(query: dict[str, str]) -> pd.DataFrame:
    return with_retirement_date(pd.DataFrame(_get_json(FACILITY_ATTRIBUTES_PATH, query)))


def retrieve_annual(query: dict[str, str], *, include_attributes: bool = True) -> Retrieval:
    """Fetch annual emissions (and optionally facility attributes) for a query."""
    started = time.monotonic()
    frame = pd.DataFrame(_get_json(ANNUAL_EMISSIONS_PATH, query))
    retrieval = Retrieval(
        frame=frame,
        query_params=dict(query),
        source_url=endpoint_url(ANNUAL_EMISSIONS_PATH),
        emission_rows=len(frame),
    )
    if frame.empty:
        retrieval.elapsed_seconds = time.monotonic() - started
        return retrieval

    # CAMPD's own internal unit key; renamed so it cannot be mistaken for the
    # EPA unit ID ("unit_id" and "unitId" normalise to the same header).
    frame = frame.rename(columns={"unit_id": "campdUnitRecordId"})

    if include_attributes:
        try:
            attributes = fetch_attributes(query)
        except CampdError as error:
            retrieval.warnings.append(
                f"Facility attributes could not be retrieved, so county, location "
                f"and dates are left blank: {error}"
            )
        else:
            retrieval.attribute_rows = len(attributes)
            if not attributes.empty:
                frame = merge_attributes(frame, attributes)

    retrieval.frame = frame
    retrieval.elapsed_seconds = time.monotonic() - started
    return retrieval
