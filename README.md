### 미국 골프장 로더 (OSM → Firestore)

- **GCP 프로젝트**: `buoyant-ability-465005-d7`
- **Firestore 데이터베이스**: `golf-course-db`
- **데이터 출처**: OSM Overpass API (`leisure=golf_course`)

### 설치
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 인증
- 기본값: `credentials/serviceAccountKey.json` 자동 인식
- 또는 환경변수 지정: `export GOOGLE_APPLICATION_CREDENTIALS=/path/to/key.json`

### 사용법 예시
```bash
# (기본) 플래그가 없으면 전체 50개 주 실행
python golf_loader.py --project buoyant-ability-465005-d7 --database golf-course-db

# 특정 주
python golf_loader.py --state CA --project buoyant-ability-465005-d7 --database golf-course-db

# 여러 주
python golf_loader.py --state CA --state AZ --project buoyant-ability-465005-d7 --database golf-course-db

# 전체 50개 주 (명시적)
python golf_loader.py --all --project buoyant-ability-465005-d7 --database golf-course-db

# 저장 없이 미리보기
python golf_loader.py --state CA --dry-run

# 변경 없는 문서 스킵 + 이번 실행에서 미발견 문서 스테일 표시
python golf_loader.py --all --skip-unchanged --mark-stale \
  --project buoyant-ability-465005-d7 --database golf-course-db

# 스테일 표시 후 N일 지난 문서 삭제 (예: 14일)
python golf_loader.py --all --purge-stale-days 14 \
  --project buoyant-ability-465005-d7 --database golf-course-db
```

### 주요 옵션
- `--project`: GCP 프로젝트 ID (예: `buoyant-ability-465005-d7`)
- `--database`: Firestore DB ID (예: `golf-course-db`)
- `--state`: US 주 코드(반복 가능), `--all`: 전체 주
- `--dry-run`: Firestore에 쓰지 않고 샘플 출력
- `--skip-unchanged`: OSM 지문 동일 시 쓰기 스킵
- `--mark-stale`: 이번 실행에서 미발견 문서에 `stale=true` 표시
- `--purge-stale-days N`: `stale=true`이고 N일 지난 문서를 삭제
- `--run-id`: 실행 식별자(미지정 시 자동 생성)

### 스키마 예시
```json
{
  "name": "Pebble Beach Golf Links",
  "name_lower": "pebble beach golf links",
  "aliases": ["Pebble Beach", "PBGL"],
  "city": "Pebble Beach",
  "state": "CA",
  "country": "US",
  "lat": 36.567,
  "lng": -121.948,
  "holes": 18,
  "website": "https://www.pebblebeach.com",
  "source": "osm:2025-08",
  "updated_at": "Firestore Timestamp",
  "osm_id": "way:123456789",
  "osm_fingerprint": "<sha256>",
  "stale": false,
  "stale_at": null,
  "last_seen_run_id": "run-..."
}
```

### 참고
- `.gitignore`에 `credentials/`, `.venv/`, 빌드/캐시 파일 등이 포함되어 민감정보가 커밋되지 않습니다.

### 도커 사용법

빌드:
```bash
docker build -t golf-loader:latest .
```

실행(로컬 자격증명 마운트, 특정 주):
```bash
docker run --rm \
  -v $(pwd)/credentials:/app/credentials:ro \
  -e GOOGLE_CLOUD_PROJECT=buoyant-ability-465005-d7 \
  golf-loader:latest \
  --state CA --project buoyant-ability-465005-d7 --database golf-course-db
```

전체 주 + 변경 없는 문서 스킵 + 스테일 표시:
```bash
docker run --rm \
  -v $(pwd)/credentials:/app/credentials:ro \
  -e GOOGLE_CLOUD_PROJECT=buoyant-ability-465005-d7 \
  golf-loader:latest \
  --all --skip-unchanged --mark-stale \
  --project buoyant-ability-465005-d7 --database golf-course-db
```

미리보기(dry-run):
```bash
docker run --rm \
  -e OVERPASS_API_URL=https://overpass-api.de/api/interpreter \
  golf-loader:latest --state CA --dry-run
```

주의:
- `credentials/` 폴더가 이미지에 포함되지 않도록 `.dockerignore`가 설정되어 있습니다.
- GCP 서비스 계정 키는 컨테이너 내부 `/app/credentials/serviceAccountKey.json`로 마운트되며, 스크립트가 자동 인식합니다.

### Cloud Run Jobs로 실행

- 기본 동작: 인자 없이 전체 50개 주 실행, 컨테이너 기본 인자 `--skip-unchanged --mark-stale` 적용
- 별도 환경변수 필요 없음(Overpass URL 변경 시에만 `OVERPASS_API_URL` 지정)

준비 변수:
```bash
PROJECT=buoyant-ability-465005-d7
REGION=us-central1
REPO=golf-db-loader
IMAGE=$REGION-docker.pkg.dev/$PROJECT/$REPO/golf-loader:latest
```

아티팩트 리포지토리(최초 1회):
```bash
gcloud artifacts repositories create $REPO \
  --repository-format=docker --location=$REGION --project $PROJECT
```

빌드/푸시:
```bash
gcloud builds submit --project $PROJECT --tag $IMAGE
```

서비스 계정 및 권한:
```bash
gcloud iam service-accounts create golf-loader-sa --project $PROJECT

gcloud projects add-iam-policy-binding $PROJECT \
  --member="serviceAccount:golf-loader-sa@$PROJECT.iam.gserviceaccount.com" \
  --role="roles/datastore.user"
```

Job 생성(인자 없이 기본 전체 주 실행):
```bash
gcloud run jobs create golf-loader-all \
  --image $IMAGE \
  --region $REGION --project $PROJECT \
  --service-account golf-loader-sa@$PROJECT.iam.gserviceaccount.com
```

실행:
```bash
gcloud run jobs execute golf-loader-all --region $REGION --project $PROJECT
```
