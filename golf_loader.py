import argparse
import os
import sys
import time
import json
import re
import uuid
import hashlib
import datetime as dt
from typing import Any, Dict, List, Optional, Tuple, Iterable

import requests
from tenacity import retry, wait_exponential, stop_after_attempt
from google.cloud import firestore
from google.cloud.firestore_v1 import DELETE_FIELD

US_STATES = [
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA",
    "HI","ID","IL","IN","IA","KS","KY","LA","ME","MD",
    "MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ",
    "NM","NY","NC","ND","OH","OK","OR","PA","RI","SC",
    "SD","TN","TX","UT","VT","VA","WA","WV","WI","WY",
]

DEFAULT_OVERPASS_URL = os.environ.get("OVERPASS_API_URL", "https://overpass-api.de/api/interpreter")
DEFAULT_PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "buoyant-ability-465005-d7")
DEFAULT_DATABASE = os.environ.get("GOOGLE_CLOUD_FIRESTORE_DATABASE", "golf-course-db")


def ensure_gcp_credentials() -> None:
    if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        return
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
    except NameError:
        script_dir = os.getcwd()
    candidate = os.path.join(script_dir, "credentials", "serviceAccountKey.json")
    if os.path.exists(candidate):
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = candidate


def slugify(*parts: str) -> str:
    text = "-".join([p for p in parts if p])
    text = text.lower()
    text = re.sub(r"[^a-z0-9\-\s]", "", text)
    text = re.sub(r"[\s_]+", "-", text).strip("-")
    return text[:200]


def to_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def parse_holes(tags: Dict[str, Any]) -> Optional[int]:
    holes = tags.get("golf:holes") or tags.get("holes")
    if isinstance(holes, str):
        m = re.search(r"(9|18|27|36|45|54)", holes)
        if m:
            return int(m.group(1))
    if isinstance(holes, (int, float)):
        try:
            iv = int(holes)
            if 0 < iv <= 54:
                return iv
        except Exception:
            pass
    return None


def extract_city_state_country(tags: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    city = tags.get("addr:city") or tags.get("is_in:city")
    state = tags.get("addr:state") or tags.get("is_in:state_code") or tags.get("is_in:state")
    country = tags.get("addr:country") or tags.get("is_in:country_code") or tags.get("is_in:country")
    if isinstance(country, str):
        if len(country) == 2:
            country = country.upper()
        elif country.lower() in ("usa", "united states", "us"):
            country = "US"
    if isinstance(state, str) and len(state) == 2:
        state = state.upper()
    return (city, state, country)


def build_aliases(tags: Dict[str, Any]) -> List[str]:
    aliases: List[str] = []
    alt_name = tags.get("alt_name")
    short_name = tags.get("short_name")
    official_name = tags.get("official_name")
    name_en = tags.get("name:en")
    for v in (alt_name, short_name, official_name, name_en):
        if isinstance(v, str):
            aliases.extend([s.strip() for s in re.split(r"[;,]", v) if s.strip()])
    uniq: List[str] = []
    seen = set()
    for a in aliases:
        al = a.lower()
        if al not in seen:
            seen.add(al)
            uniq.append(a)
    return uniq[:10]


def compute_osm_fingerprint(doc: Dict[str, Any]) -> str:
    payload = {
        "name_lower": (doc.get("name_lower") or ""),
        "aliases": sorted([(a or "").lower() for a in (doc.get("aliases") or [])]),
        "city": (doc.get("city") or ""),
        "state": (doc.get("state") or ""),
        "lat": doc.get("lat"),
        "lng": doc.get("lng"),
        "holes": doc.get("holes"),
        "website": (doc.get("website") or ""),
        "country": (doc.get("country") or ""),
    }
    s = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def normalize_course(element: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    tags = element.get("tags") or {}
    if tags.get("leisure") != "golf_course":
        return None

    name = tags.get("name")
    if not isinstance(name, str) or not name.strip():
        return None

    lat = element.get("lat")
    lon = element.get("lon")
    if lat is None or lon is None:
        center = element.get("center") or {}
        lat = center.get("lat")
        lon = center.get("lon")

    lat_f = to_float(lat)
    lon_f = to_float(lon)
    if lat_f is None or lon_f is None:
        return None

    city, state, country = extract_city_state_country(tags)
    if not country:
        country = "US"
    holes = parse_holes(tags)

    website = None
    for key in ("website", "contact:website", "url"):
        v = tags.get(key)
        if isinstance(v, str) and v.startswith("http"):
            website = v
            break

    aliases = build_aliases(tags)

    osm_type = element.get("type")
    osm_raw_id = element.get("id")
    osm_id = f"{osm_type}:{osm_raw_id}" if osm_type and osm_raw_id is not None else None

    doc = {
        "name": name,
        "name_lower": name.lower(),
        "aliases": aliases,
        "city": city,
        "state": state,
        "country": country,
        "lat": lat_f,
        "lng": lon_f,
        "holes": holes,
        "website": website,
        "source": f"osm:{time.strftime('%Y-%m')}",
        "osm_id": osm_id,
        # fingerprint/osm_updated_at set later
    }
    return doc


def overpass_query_for_state(state_code: str) -> str:
    iso = f"US-{state_code}"
    return f"""
    [out:json][timeout:90];
    area["ISO3166-2"="{iso}"]->.searchArea;
    (
      node["leisure"="golf_course"](area.searchArea);
      way["leisure"="golf_course"](area.searchArea);
      relation["leisure"="golf_course"](area.searchArea);
    );
    out center tags;
    """.strip()


@retry(wait=wait_exponential(multiplier=1, min=1, max=60), stop=stop_after_attempt(5))
def call_overpass(query: str, url: str = DEFAULT_OVERPASS_URL) -> Dict[str, Any]:
    resp = requests.post(url, data={"data": query}, timeout=120)
    resp.raise_for_status()
    return resp.json()


def fetch_courses_by_state(state_code: str) -> List[Dict[str, Any]]:
    query = overpass_query_for_state(state_code)
    data = call_overpass(query)
    elements = data.get("elements", [])
    courses: List[Dict[str, Any]] = []
    for el in elements:
        doc = normalize_course(el)
        if doc and doc.get("country") == "US":
            if not doc.get("state"):
                doc["state"] = state_code
            courses.append(doc)
    return courses


def get_firestore_client(project: str, database: str) -> firestore.Client:
    return firestore.Client(project=project, database=database)


def _batched(iterable: Iterable[Any], size: int) -> Iterable[List[Any]]:
    batch: List[Any] = []
    for item in iterable:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def upsert_courses(
    courses: List[Dict[str, Any]],
    project: str = DEFAULT_PROJECT,
    database: str = DEFAULT_DATABASE,
    collection: str = "courses",
    skip_unchanged: bool = False,
    run_id: Optional[str] = None,
) -> Tuple[int, int]:
    if not courses:
        return (0, 0)
    client = get_firestore_client(project, database)

    # Prepare docs and compute ids + fingerprints
    prepared: Dict[str, Dict[str, Any]] = {}
    for course in courses:
        doc_id = slugify(course.get("name", ""), course.get("city", ""), course.get("state", ""))
        course["osm_fingerprint"] = compute_osm_fingerprint(course)
        prepared[doc_id] = course

    # Fetch existing docs to compare
    existing: Dict[str, Dict[str, Any]] = {}
    doc_refs = [client.collection(collection).document(doc_id) for doc_id in prepared.keys()]
    for chunk in _batched(doc_refs, 300):
        for snap in client.get_all(chunk):
            if snap.exists:
                existing[snap.id] = snap.to_dict()

    batch = client.batch()
    write_count = 0
    skip_count = 0
    for doc_id, course in prepared.items():
        prev = existing.get(doc_id)
        if skip_unchanged and prev and prev.get("osm_fingerprint") == course.get("osm_fingerprint"):
            skip_count += 1
            continue
        doc_ref = client.collection(collection).document(doc_id)
        payload = {**course}
        payload["updated_at"] = firestore.SERVER_TIMESTAMP
        payload["osm_updated_at"] = firestore.SERVER_TIMESTAMP
        payload["last_seen_run_id"] = run_id
        payload["stale"] = False
        payload["stale_at"] = DELETE_FIELD
        batch.set(doc_ref, payload, merge=True)
        write_count += 1
        if write_count % 400 == 0:
            batch.commit()
            batch = client.batch()
    if write_count % 400 != 0:
        batch.commit()
    return (write_count, skip_count)


def mark_stale_for_states(
    states: List[str],
    project: str,
    database: str,
    collection: str,
    run_id: str,
) -> int:
    client = get_firestore_client(project, database)
    marked = 0
    for st in states:
        q = client.collection(collection).where("country", "==", "US").where("state", "==", st)
        batch = client.batch()
        for i, snap in enumerate(q.stream(), start=1):
            data = snap.to_dict() or {}
            if data.get("last_seen_run_id") != run_id:
                batch.set(snap.reference, {"stale": True, "stale_at": firestore.SERVER_TIMESTAMP}, merge=True)
                marked += 1
            if i % 400 == 0:
                batch.commit()
                batch = client.batch()
        batch.commit()
    return marked


def purge_stale(
    states: Optional[List[str]],
    project: str,
    database: str,
    collection: str,
    older_than_days: int,
) -> int:
    if older_than_days <= 0:
        return 0
    client = get_firestore_client(project, database)
    cutoff = dt.datetime.utcnow() - dt.timedelta(days=older_than_days)
    deleted = 0
    target_states = states or US_STATES
    for st in target_states:
        q = (
            client.collection(collection)
            .where("stale", "==", True)
            .where("state", "==", st)
        )
        batch = client.batch()
        i = 0
        for snap in q.stream():
            data = snap.to_dict() or {}
            stale_at = data.get("stale_at")
            if isinstance(stale_at, dt.datetime) and stale_at <= cutoff:
                batch.delete(snap.reference)
                deleted += 1
                i += 1
                if i % 400 == 0:
                    batch.commit()
                    batch = client.batch()
        batch.commit()
    return deleted


def run(
    states: List[str],
    all_states: bool,
    dry_run: bool,
    project: str,
    database: str,
    skip_unchanged: bool,
    mark_stale: bool,
    purge_stale_days: int,
    run_id: Optional[str],
) -> None:
    target_states = US_STATES if all_states else states
    if not target_states:
        print("No states provided. Use --state or --all.", file=sys.stderr)
        sys.exit(1)

    run_id = run_id or f"run-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"

    all_courses: List[Dict[str, Any]] = []
    for st in target_states:
        st = st.upper()
        if st not in US_STATES:
            print(f"Skip invalid state: {st}", file=sys.stderr)
            continue
        print(f"Fetching state {st}...")
        try:
            courses = fetch_courses_by_state(st)
        except Exception as e:
            print(f"Error fetching {st}: {e}", file=sys.stderr)
            continue
        print(f"  Found {len(courses)} courses")
        all_courses.extend(courses)

    if dry_run:
        print(json.dumps(all_courses[:10], ensure_ascii=False, indent=2))
        print(f"Total courses (sampled 10 shown): {len(all_courses)}")
        return

    written, skipped = upsert_courses(
        all_courses,
        project=project,
        database=database,
        skip_unchanged=skip_unchanged,
        run_id=run_id,
    )
    print(f"Upserted {written} courses, skipped {skipped} unchanged into Firestore project {project}, database {database}")

    if mark_stale:
        marked = mark_stale_for_states([s.upper() for s in target_states], project, database, "courses", run_id)
        print(f"Marked {marked} courses as stale")

    if purge_stale_days and purge_stale_days > 0:
        purged = purge_stale([s.upper() for s in target_states], project, database, "courses", purge_stale_days)
        print(f"Purged {purged} stale courses older than {purge_stale_days} days")


def main():
    ensure_gcp_credentials()
    parser = argparse.ArgumentParser(description="OSM → Firestore golf course loader")
    parser.add_argument("--state", action="append", default=[], help="US state code, can repeat")
    parser.add_argument("--all", action="store_true", help="Process all 50 states")
    parser.add_argument("--dry-run", action="store_true", help="Do not write to Firestore; print sample")
    parser.add_argument("--project", default=DEFAULT_PROJECT, help="GCP project id")
    parser.add_argument("--database", default=DEFAULT_DATABASE, help="Firestore database id (default '(default)')")
    parser.add_argument("--skip-unchanged", action="store_true", help="Skip writes when osm_fingerprint unchanged")
    parser.add_argument("--mark-stale", action="store_true", help="Mark not-seen docs in processed states as stale")
    parser.add_argument("--purge-stale-days", type=int, default=0, help="Delete stale docs older than N days (0=disabled)")
    parser.add_argument("--run-id", default=None, help="Optional run id; auto-generated if omitted")
    args = parser.parse_args()

    # Default to all states if neither --state nor --all is provided
    if not args.state and not args.all:
        args.all = True

    run(
        states=args.state,
        all_states=args.all,
        dry_run=args.dry_run,
        project=args.project,
        database=args.database,
        skip_unchanged=args.skip_unchanged,
        mark_stale=args.mark_stale,
        purge_stale_days=args.purge_stale_days,
        run_id=args.run_id,
    )


if __name__ == "__main__":
    main()
