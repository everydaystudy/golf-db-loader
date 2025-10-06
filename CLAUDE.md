# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Python-based data loader that fetches US golf course data from OpenStreetMap (via Overpass API) and stores it in Google Cloud Firestore. The script processes data state-by-state, handles deduplication via fingerprinting, and manages stale records.

**Target Infrastructure:**
- GCP Project: `buoyant-ability-465005-d7`
- Firestore Database: `golf-course-db`
- Collection: `courses`
- Deployment: Cloud Run Jobs (containerized)

## Development Commands

### Environment Setup
```bash
# Create and activate virtual environment
python3 -m venv .venv && source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### Authentication
The script auto-detects credentials at `credentials/serviceAccountKey.json` if `GOOGLE_APPLICATION_CREDENTIALS` is not set (see `ensure_gcp_credentials()` in golf_loader.py:31).

### Running Locally
```bash
# Default: processes all 50 states (behavior changed by default when no flags provided)
python golf_loader.py --project buoyant-ability-465005-d7 --database golf-course-db

# Specific state(s)
python golf_loader.py --state CA --state AZ --project buoyant-ability-465005-d7 --database golf-course-db

# Dry-run (preview without writing)
python golf_loader.py --state CA --dry-run

# Production-safe flags
python golf_loader.py --all --skip-unchanged --mark-stale \
  --project buoyant-ability-465005-d7 --database golf-course-db

# Purge stale records older than N days
python golf_loader.py --all --purge-stale-days 14 \
  --project buoyant-ability-465005-d7 --database golf-course-db
```

### Docker
```bash
# Build
docker build -t golf-loader:latest .

# Run with local credentials mounted
docker run --rm \
  -v $(pwd)/credentials:/app/credentials:ro \
  -e GOOGLE_CLOUD_PROJECT=buoyant-ability-465005-d7 \
  golf-loader:latest \
  --state CA --project buoyant-ability-465005-d7 --database golf-course-db
```

### Cloud Run Jobs
```bash
# Build and push to Artifact Registry
PROJECT=buoyant-ability-465005-d7
REGION=us-central1
REPO=golf-db-loader
IMAGE=$REGION-docker.pkg.dev/$PROJECT/$REPO/golf-loader:latest
gcloud builds submit --project $PROJECT --tag $IMAGE

# Create job (uses default CMD: --skip-unchanged --mark-stale)
gcloud run jobs create golf-loader-all \
  --image $IMAGE \
  --region $REGION --project $PROJECT \
  --service-account golf-loader-sa@$PROJECT.iam.gserviceaccount.com

# Execute
gcloud run jobs execute golf-loader-all --region $REGION --project $PROJECT
```

## Architecture

### Core Data Flow
1. **Fetch** (golf_loader.py:241): Queries Overpass API per state using ISO3166-2 area codes
2. **Normalize** (golf_loader.py:159): Transforms OSM tags into standardized schema; generates search fields (tokens, n-grams, normalized text)
3. **Fingerprint** (golf_loader.py:143): Computes SHA256 hash of key fields for deduplication
4. **Upsert** (golf_loader.py:270): Batch-writes to Firestore (400 docs/batch), skips unchanged docs if `--skip-unchanged`
5. **Stale Management** (golf_loader.py:322): Marks unseen docs as stale via `last_seen_run_id` tracking; purges after N days

### Document Schema
```json
{
  "name": "Pebble Beach Golf Links",
  "name_lower": "pebble beach golf links",
  "name_lower_normalized": "pebble beach golf links",
  "name_tokens": ["pebble", "beach", "golf", "links"],
  "name_ngrams": ["peb", "ebb", "bbl", "ble", "..."],
  "name_ngrams_normalized": ["peb", "ebb", "bbl", "ble", "..."],
  "aliases": ["Pebble Beach", "PBGL"],
  "city": "Pebble Beach",
  "state": "CA",
  "country": "US",
  "lat": 36.567,
  "lng": -121.948,
  "holes": 18,
  "website": "https://www.pebblebeach.com",
  "source": "osm:2025-08",
  "updated_at": "<Firestore Timestamp>",
  "osm_id": "way:123456789",
  "osm_fingerprint": "<sha256>",
  "stale": false,
  "stale_at": null,
  "last_seen_run_id": "run-20250806120000-abc123"
}
```

### Document ID Generation
Uses `slugify()` (golf_loader.py:42): concatenates `name-city-state`, lowercased, non-alphanumeric stripped, max 200 chars. Example: `pebble-beach-golf-links-pebble-beach-ca`

### Key Functions
- `normalize_course()` (golf_loader.py:159): Parses OSM elements into doc structure; rejects nodes without names or coordinates; generates search fields
- `normalize_text()` (golf_loader.py:60): Removes diacritics and special characters (okina, apostrophes) for search normalization
- `generate_ngrams()` (golf_loader.py:72): Creates 3-character n-grams for fuzzy search indexing
- `generate_name_tokens()` (golf_loader.py:86): Splits names into whitespace-separated tokens for search
- `compute_osm_fingerprint()` (golf_loader.py:143): Deterministic hash for change detection
- `upsert_courses()` (golf_loader.py:270): Batch upserts with fingerprint comparison; uses `client.get_all()` for bulk reads (300 docs/batch)
- `mark_stale_for_states()` (golf_loader.py:322): Marks docs not seen in current run_id
- `purge_stale()` (golf_loader.py:346): Deletes docs with `stale=true` and `stale_at` older than threshold

### Retry Logic
`call_overpass()` (golf_loader.py:234): Uses `tenacity` with exponential backoff (1-60s, max 5 attempts)

### State Coverage
Processes all 50 US states via `US_STATES` constant (golf_loader.py:18). Default behavior: `--all` is auto-enabled if no `--state` flags provided (golf_loader.py:452).

## Adding New Firestore Fields

To add normalized fields or other computed fields to existing documents, create a one-time migration script following this pattern:

```python
#!/usr/bin/env python3
"""
Add normalized fields to golf-course-db for improved search
"""
import unicodedata
from google.cloud import firestore

db = firestore.Client(database="golf-course-db")

def normalize_text(text: str) -> str:
    """Remove special characters and diacritics"""
    if not text:
        return ""
    # NFD decomposition + remove combining marks
    text = unicodedata.normalize('NFD', text)
    text = ''.join(c for c in text if unicodedata.category(c) != 'Mn')
    # Remove apostrophes, okina, etc.
    text = text.replace("ʻ", "").replace("'", "").replace("`", "").replace("'", "")
    return text.strip().lower()

def generate_ngrams(text: str, n: int = 3):
    """Generate n-grams for search indexing"""
    text = (text or "").strip().lower()
    if not text or len(text) < n:
        return []
    grams = []
    for i in range(len(text) - n + 1):
        gram = text[i : i + n]
        if gram not in grams:
            grams.append(gram)
    return grams

def update_course_fields():
    """Add name_lower_normalized and name_ngrams_normalized to all docs"""
    courses_ref = db.collection("courses")
    batch_size = 500
    processed = 0

    docs = courses_ref.stream()
    batch = db.batch()
    batch_count = 0

    for doc in docs:
        data = doc.to_dict()
        name = data.get("name", "")

        # Compute new fields
        name_normalized = normalize_text(name)
        ngrams_normalized = generate_ngrams(name_normalized, 3)

        # Update document
        batch.update(doc.reference, {
            "name_lower_normalized": name_normalized,
            "name_ngrams_normalized": ngrams_normalized
        })

        batch_count += 1
        processed += 1

        # Commit every 500 docs
        if batch_count >= batch_size:
            batch.commit()
            print(f"✅ {processed} documents updated")
            batch = db.batch()
            batch_count = 0

    # Commit remaining
    if batch_count > 0:
        batch.commit()
        print(f"✅ {processed} documents updated")

    print(f"\n🎉 Total {processed} golf courses updated")

if __name__ == "__main__":
    print("🏌️ Adding normalized fields...\n")
    update_course_fields()
```

**Usage:**
```bash
# Set credentials
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/serviceAccountKey.json

# Run migration
python add_normalized_fields.py
```

**Performance:** ~30s for 1,000 docs, ~5min for 10,000 docs

**Indexing:** Firestore auto-creates indexes for new fields. For composite indexes, create via Firebase Console (Firestore → Indexes) or define in `firestore.indexes.json`.

## CI/CD

### GitHub Actions Workflow
Automated deployment configured in `.github/workflows/deploy-cloud-run-job.yml`:
- **Triggers**: Push to `main` branch or manual `workflow_dispatch`
- **Steps**:
  1. Authenticates using `GCP_SA_KEY` secret
  2. Builds Docker image and tags with commit SHA + `latest`
  3. Pushes to Artifact Registry (`us-central1-docker.pkg.dev/buoyant-ability-465005-d7/golf-db-loader`)
  4. Creates or updates Cloud Run Job `golf-loader-all`
  5. Optionally executes job if triggered manually

**Required Secret**: `GCP_SA_KEY` (service account JSON with Artifact Registry and Cloud Run permissions)

## Important Constraints

- **Firestore Batch Limits**: Max 500 operations per batch; code uses 400 for safety margin
- **Overpass Timeout**: Queries set to 90s; larger states (CA, TX) may hit rate limits
- **Credential Security**: `credentials/` excluded via `.gitignore` and `.dockerignore`
- **No Unit Tests**: Repository has no test infrastructure; validate changes via `--dry-run`
