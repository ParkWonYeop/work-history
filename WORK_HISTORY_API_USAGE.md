# Work History 읽기 API 사용법

이 문서는 Work History 서버에 수집된 Jira·Confluence·GitLab·Slack 업무 활동, 업무 대상과 생성 보고서를
외부 프로그램에서 조회하는 방법을 설명한다. 수집 데이터 업로드 API와 Report Agent 서명 API는 장치
키를 사용하는 내부 인터페이스이므로 여기서는 다루지 않는다.

## 1. 기본 정보

- 기본 URL 예시: `https://work-history.example.com`
- 응답 형식: JSON, UTF-8
- 인증: `Authorization: Bearer <READ_API_TOKEN>`
- 공개 엔드포인트: `GET /healthz`
- 인증 필요 엔드포인트: `/v1/` 아래의 모든 읽기 API
- OpenAPI·Swagger UI: 외부에서 제공하지 않음
- 저장 시각: UTC
- 일간·주간·월간 보고서의 날짜 경계: `Asia/Seoul`
- 읽기 요청 제한: 클라이언트 IP별 1분에 30회

본문, 댓글, 변경 내용과 보고서에는 회사 업무정보가 포함될 수 있다. 응답을 로그, 공개 저장소 또는
보호되지 않은 임시 파일에 저장하지 않는다.

## 2. 인증 준비

읽기 토큰은 Work History LXC의 root 권한으로만 확인한다.

```bash
/opt/work-history/source/deploy/server/show-read-token.sh
```

토큰을 URL 쿼리나 셸 명령 기록에 직접 입력하지 말고 비밀관리 도구 또는 프로세스 환경변수로 전달한다.
다음은 Bash에서 현재 세션에만 입력하는 예다.

```bash
export WORK_HISTORY_API_URL='https://work-history.example.com'
read -r -s -p 'Read API token: ' WORK_HISTORY_API_TOKEN
printf '\n'
export WORK_HISTORY_API_TOKEN
```

공통 인증 헤더:

```text
Authorization: Bearer <READ_API_TOKEN>
```

작업이 끝나면 환경변수를 제거한다.

```bash
unset WORK_HISTORY_API_TOKEN
```

## 3. 빠른 확인

### 서버와 DB 상태

`/healthz`는 인증이 필요하지 않으며 애플리케이션이 PostgreSQL에 질의할 수 있는지 확인한다.

```bash
curl --fail-with-body --silent --show-error \
  "${WORK_HISTORY_API_URL:?}/healthz" | jq
```

정상 응답:

```json
{
  "status": "ok"
}
```

### 소스별 최근 동기화 상태

```bash
curl --fail-with-body --silent --show-error \
  -H "Authorization: Bearer ${WORK_HISTORY_API_TOKEN:?}" \
  "${WORK_HISTORY_API_URL:?}/v1/sync-status" | jq
```

응답 예시:

```json
{
  "sources": {
    "jira": {
      "status": "success",
      "job_kind": "incremental",
      "started_at": "2026-08-07T00:58:32Z",
      "finished_at": "2026-08-07T00:58:43Z",
      "counters": {
        "accepted": 3,
        "duplicates": 75,
        "records": 78
      },
      "error": null
    },
    "gitlab": {
      "status": "success",
      "job_kind": "signed_ingest",
      "started_at": null,
      "finished_at": "2026-08-07T00:00:19Z",
      "counters": {
        "devices": 1
      },
      "error": null
    }
  }
}
```

`status=success`는 마지막 수집 실행의 성공 여부다. 실제 업무 기간까지 수집됐는지는 `finished_at`과
보고서의 `source_snapshot`을 함께 확인한다.

## 4. 업무 활동 조회

```text
GET /v1/activities
```

### 쿼리 파라미터

| 이름 | 필수 | 설명 |
|---|---:|---|
| `from` | 예 | 조회 시작 시각. RFC 3339 timezone offset 필수, 시작 시각 포함 |
| `to` | 예 | 조회 종료 시각. RFC 3339 timezone offset 필수, 종료 시각 제외 |
| `sources` | 아니요 | `jira`, `confluence`, `gitlab`, `slack`을 쉼표로 구분 |
| `cursor` | 아니요 | 이전 응답의 `next_cursor`. 내용을 해석하거나 수정하지 않고 그대로 전달 |
| `limit` | 아니요 | 페이지 크기. 기본 200, 최소 1, 최대 500 |

한 요청의 기간은 최대 31일이다. 31일보다 긴 기간은 여러 구간으로 나누어 호출해야 한다. 결과는
`occurred_at`, 내부 ID의 오름차순으로 반환된다.

### 서울 시간 기준 하루 조회

```bash
curl --fail-with-body --silent --show-error --get \
  -H "Authorization: Bearer ${WORK_HISTORY_API_TOKEN:?}" \
  --data-urlencode 'from=2026-08-07T00:00:00+09:00' \
  --data-urlencode 'to=2026-08-08T00:00:00+09:00' \
  --data-urlencode 'sources=jira,confluence,gitlab,slack' \
  --data-urlencode 'limit=500' \
  "${WORK_HISTORY_API_URL:?}/v1/activities" | jq
```

특정 소스만 조회할 수도 있다.

```bash
curl --fail-with-body --silent --show-error --get \
  -H "Authorization: Bearer ${WORK_HISTORY_API_TOKEN:?}" \
  --data-urlencode 'from=2026-08-01T00:00:00+09:00' \
  --data-urlencode 'to=2026-08-08T00:00:00+09:00' \
  --data-urlencode 'sources=gitlab' \
  "${WORK_HISTORY_API_URL:?}/v1/activities" | jq
```

### 응답 구조

```json
{
  "items": [
    {
      "id": "event-uuid",
      "source": "jira",
      "kind": "issue",
      "action": "changed",
      "occurred_at": "2026-08-07T01:20:00Z",
      "actor_remote_id": "remote-user-id",
      "actor_is_self": true,
      "artifact_remote_id": "WORK-123",
      "title": "WORK-123",
      "changes": {
        "status": {
          "from": "In Progress",
          "to": "Done"
        }
      },
      "url": "https://example.atlassian.net/browse/WORK-123"
    }
  ],
  "next_cursor": null
}
```

주요 필드:

- `source`: 원천 시스템
- `kind`: 이슈, 페이지, MR, 커밋 등 원천 대상 종류
- `action`: 생성, 변경, 댓글, 커밋, 병합 등의 정규화된 동작
- `occurred_at`: 실제 활동 발생 시각
- `actor_is_self`: 본인 활동 여부
- `artifact_remote_id`: 상세 대상 조회에 사용할 원천 ID
- `changes`: 상태·담당자·필드 변경 등 구조화된 변경 내용
- `url`: 원본 Jira·Confluence·GitLab·Slack 링크

### 모든 페이지 조회

`next_cursor`가 `null`이 될 때까지 같은 `from`, `to`, `sources` 값과 함께 다음 요청에 전달한다.

```python
import json
import os
from urllib.parse import urlencode
from urllib.request import Request, urlopen

base_url = os.environ["WORK_HISTORY_API_URL"].rstrip("/")
token = os.environ["WORK_HISTORY_API_TOKEN"]
params = {
    "from": "2026-08-01T00:00:00+09:00",
    "to": "2026-08-08T00:00:00+09:00",
    "sources": "jira,confluence,gitlab,slack",
    "limit": 500,
}

activities = []
while True:
    request = Request(
        f"{base_url}/v1/activities?{urlencode(params)}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urlopen(request, timeout=30) as response:
        page = json.load(response)
    activities.extend(page["items"])
    if page["next_cursor"] is None:
        break
    params["cursor"] = page["next_cursor"]

print(f"loaded {len(activities)} activities")
```

## 5. 업무 대상 상세 조회

```text
GET /v1/artifacts/{source}/{remote_id}
```

활동의 `artifact_remote_id`를 이용해 이슈·Confluence 페이지·GitLab MR·커밋 또는 Slack 메시지의 현재 본문과 저장된
버전을 조회한다.

```bash
curl --fail-with-body --silent --show-error \
  -H "Authorization: Bearer ${WORK_HISTORY_API_TOKEN:?}" \
  "${WORK_HISTORY_API_URL:?}/v1/artifacts/jira/WORK-123" | jq
```

`source`는 `jira`, `confluence`, `gitlab`, `slack` 중 하나다. `remote_id`에 `/`, 공백, `#` 같은 특수문자가
있으면 URL path component로 인코딩해야 한다.

응답 예시:

```json
{
  "id": "artifact-uuid",
  "source": "jira",
  "remote_id": "WORK-123",
  "kind": "스토리",
  "title": "업무 이력 조회 기능",
  "body_text": "업무 설명 본문",
  "state": "Done",
  "namespace": "WORK",
  "url": "https://example.atlassian.net/browse/WORK-123",
  "created_at_remote": "2026-08-01T00:00:00Z",
  "updated_at_remote": "2026-08-07T01:20:00Z",
  "versions": [
    {
      "remote_version_id": "3",
      "author_remote_id": "remote-user-id",
      "body_text": "해당 버전의 본문",
      "created_at_remote": "2026-08-07T01:20:00Z"
    }
  ]
}
```

첨부파일 바이너리는 반환하지 않는다. 첨부파일 관련 활동과 링크, 메타데이터만 수집 범위에 포함될 수
있다.

## 6. 생성 보고서 목록 조회

```text
GET /v1/reports
```

### 쿼리 파라미터

| 이름 | 필수 | 허용값·설명 |
|---|---:|---|
| `cadence` | 아니요 | `daily`, `weekly`, `monthly`, `overall` |
| `kind` | 아니요 | `work_report`, `feedback` |
| `status` | 아니요 | `partial`, `final` |
| `from` | 아니요 | `period_start`가 이 날짜 이상인 보고서 |
| `to` | 아니요 | `period_start`가 이 날짜 이하인 보고서 |
| `limit` | 아니요 | 기본 200, 최대 500 |

목록은 기간 시작일 내림차순으로 반환되며 본문 대신 메타데이터만 포함한다. 이 엔드포인트에는 cursor
pagination이 없으므로 필요하면 날짜 범위를 나누어 조회한다.

```bash
curl --fail-with-body --silent --show-error --get \
  -H "Authorization: Bearer ${WORK_HISTORY_API_TOKEN:?}" \
  --data-urlencode 'cadence=weekly' \
  --data-urlencode 'kind=work_report' \
  --data-urlencode 'status=final' \
  --data-urlencode 'from=2026-04-01' \
  --data-urlencode 'to=2026-08-31' \
  --data-urlencode 'limit=500' \
  "${WORK_HISTORY_API_URL:?}/v1/reports" | jq
```

응답 예시:

```json
{
  "items": [
    {
      "id": "report-uuid",
      "cadence": "weekly",
      "period": "2026-W31",
      "kind": "work_report",
      "status": "final",
      "title": "2026-W31 주간 업무 보고서",
      "content_sha256": "sha256-hex",
      "current_revision": 1,
      "updated_at": "2026-08-06T02:30:00Z"
    }
  ]
}
```

## 7. 보고서 본문 조회

```text
GET /v1/reports/{cadence}/{period}/{kind}
```

기간 형식:

| cadence | period 형식 | 예시 |
|---|---|---|
| `daily` | `YYYY-MM-DD` | `2026-08-07` |
| `weekly` | ISO 주차 `YYYY-Www` | `2026-W31` |
| `monthly` | `YYYY-MM` | `2026-07` |
| `overall` | `YYYY-MM-DD_to_YYYY-MM-DD` | `2026-04-01_to_2026-08-05` |

`weekly`은 월요일부터 일요일까지다. 응답의 `period_end`는 내부 조회 경계에 사용하는 종료일 제외
값이다. 예를 들어 `2026-W31`은 `period_start=2026-07-27`, `period_end=2026-08-03`이다.

### 주간 업무 보고서

```bash
curl --fail-with-body --silent --show-error \
  -H "Authorization: Bearer ${WORK_HISTORY_API_TOKEN:?}" \
  "${WORK_HISTORY_API_URL:?}/v1/reports/weekly/2026-W31/work_report" | jq
```

### 월간 피드백

```bash
curl --fail-with-body --silent --show-error \
  -H "Authorization: Bearer ${WORK_HISTORY_API_TOKEN:?}" \
  "${WORK_HISTORY_API_URL:?}/v1/reports/monthly/2026-07/feedback" | jq
```

### Markdown 본문만 파일로 저장

```bash
curl --fail-with-body --silent --show-error \
  -H "Authorization: Bearer ${WORK_HISTORY_API_TOKEN:?}" \
  "${WORK_HISTORY_API_URL:?}/v1/reports/weekly/2026-W31/work_report" \
  | jq -r '.markdown' > 2026-W31-work-report.md
```

보고서 상세 응답의 주요 필드:

- `status`: `final`이면 보고 기간 종료까지 모든 원천이 수집된 상태, `partial`이면 원천 일부가 덜
  수집된 상태
- `markdown`: 현재 revision의 Markdown 본문
- `source_snapshot`: 생성 당시 Jira·Confluence·GitLab·Slack 수집 범위
- `source_event_counts`: 생성에 사용한 원천별 이벤트 수. 생산성 점수가 아닌 문맥 정보
- `content_sha256`: 현재 Markdown 본문의 SHA-256
- `current_revision`: 현재 revision 번호
- `versions`: 이전 revision을 포함한 버전 메타데이터. 이전 본문 전체는 반환하지 않음
- `finalized_at`: 최초로 `final`이 된 시각

## 8. 전날 업무 이력 조회 예시

매일 보고서 생성기가 전날 서울 시간 기준 데이터를 가져오는 형태는 다음과 같다.

```bash
FROM_KST='2026-08-07T00:00:00+09:00'
TO_KST='2026-08-08T00:00:00+09:00'

curl --fail-with-body --silent --show-error --get \
  -H "Authorization: Bearer ${WORK_HISTORY_API_TOKEN:?}" \
  --data-urlencode "from=${FROM_KST}" \
  --data-urlencode "to=${TO_KST}" \
  --data-urlencode 'sources=jira,confluence,gitlab,slack' \
  --data-urlencode 'limit=500' \
  "${WORK_HISTORY_API_URL:?}/v1/activities" > activities.json
```

각 활동의 `artifact_remote_id`가 있으면 `/v1/artifacts/{source}/{remote_id}`를 추가 호출해 현재 본문과
버전을 결합한다. 이미 만들어진 일간 보고서가 필요하면 원천 활동을 다시 조합하지 않고 다음 API를
사용한다.

```bash
curl --fail-with-body --silent --show-error \
  -H "Authorization: Bearer ${WORK_HISTORY_API_TOKEN:?}" \
  "${WORK_HISTORY_API_URL:?}/v1/reports/daily/2026-08-07/work_report" | jq -r '.markdown'
```

## 9. HTTP 오류

| 상태 | 의미 | 주요 원인 |
|---:|---|---|
| `400` | 잘못된 조회 요청 | timezone 누락, `to <= from`, 31일 초과, 잘못된 source 또는 cursor |
| `401` | 인증 실패 | Bearer 헤더 누락 또는 토큰 불일치 |
| `404` | 대상 없음 | 존재하지 않는 artifact·보고서 또는 잘못된 source |
| `422` | period·파라미터 검증 실패 | 주차·월·전체 기간 형식 오류 |
| `429` | 요청 제한 초과 | 같은 클라이언트 IP에서 1분에 30회 초과 |
| `503` | DB 사용 불가 | `/healthz`의 PostgreSQL 연결 실패 |

오류 응답 예시:

```json
{
  "detail": "maximum range is 31 days"
}
```

## 10. 운영 및 보안 주의사항

- 읽기 토큰과 GitLab 수집 장치 키는 서로 다른 자격증명이다.
- 읽기 토큰을 브라우저 프론트엔드 코드, URL, Git 저장소, CI 로그에 넣지 않는다.
- PostgreSQL은 직접 노출하지 않고 HTTPS 읽기 API만 사용한다.
- `next_cursor`는 불투명한 값으로 취급하고 수정하거나 장기 체크포인트로 재해석하지 않는다.
- 데이터는 UTC로 반환되므로 사용자 표시 시 `Asia/Seoul`로 변환한다.
- 원본 API JSON은 기본 180일 후 삭제되지만 정규화한 활동, 산출물 문맥과 보고서는 유지된다.
- 생성 보고서는 수집 기록을 해석한 2차 자료다. 공식 증빙에는 보고서와 함께 각 활동의 `url`,
  `occurred_at`, 업무 대상의 저장 버전을 사용한다.
- 자동화에서는 `429`, 일시적인 `5xx`, 네트워크 오류에 지수 백오프를 적용하고 `401`, `400`, `422`는
  재시도하기 전에 설정이나 요청을 수정한다.
- 대량 조회는 31일 이하의 고정된 시간 구간과 cursor pagination을 함께 사용한다.
