# Work History 에이전트 인수인계 가이드

이 문서는 다른 Codex/자동화 에이전트가 현재 Work History 시스템을 점검·배포·복구·확장할 때 사용하는 운영 기준이다.

> API 키, PAT, 비밀번호, 개인키, Slack 토큰은 이 문서·Git·채팅·로그에 평문으로 넣지 않는다. 실제 비밀값은 아래 보관 위치에서 서버 내부 프로세스로만 사용하거나 비밀번호 관리자를 통해 별도로 전달한다.

## 1. 현재 운영 환경

| 항목 | 실제 값 | 용도 |
|---|---|---|
| GitHub 저장소 | https://github.com/ParkWonYeop/work-history (private) | 코드·배포·문서 |
| Proxmox LXC | Debian 13, hostname work-history-lxc, 192.0.2.4 | API·PostgreSQL·수집·아카이브 |
| 긴급 SSH | ssh -o BatchMode=yes -o IdentitiesOnly=yes -i /Users/you/.ssh/work-history-lxc -p 2222 root@ssh.example.com | 긴급 운영 전용 |
| 공개 API | https://work-history.example.com | HTTPS 읽기 API·GitLab 수신·Report Agent |
| NPM | 192.0.2.111 | LXC 192.0.2.4:8080의 유일한 LAN 프록시 |
| GitLab | https://gitlab.example.com | Mac VPN 연결 시 수집 |
| Atlassian | https://example.atlassian.net | Jira·Confluence 수집 |
| Atlassian 계정 | me@example.com | API 토큰 소유자 |
| Slack Workspace | https://example-workspace.slack.com | Slack 수집 |
| Slack App ID | A0EXAMPLE | 설치된 Slack 앱 식별자 |
| R2 endpoint | https://ACCOUNT_ID.r2.cloudflarestorage.com | S3 호환 원본 아카이브 |
| R2 bucket / prefix | work-history-archive / raw/v1/ | 암호화 원본 JSONL |
| age 공개 수신자 | age1recipient | 서버 암호화용 공개키 |

## 2. 데이터 흐름

    Jira + Confluence ───▶ Debian LXC ───▶ PostgreSQL
    Slack ───────────────▶ Debian LXC ───▶ raw JSON (180일) ───▶ age+zstd ───▶ R2
    Mac + FortiClient VPN ▶ 사내 GitLab ─▶ signed HTTPS ───────▶ Debian LXC
    Codex Report Agent ──▶ signed HTTPS ─▶ generated_reports
    외부 읽기 클라이언트 ─▶ NPM HTTPS ───▶ LXC :8080

- Jira·Confluence는 LXC가 직접 수집한다.
- GitLab은 VPN/MFA를 자동화하지 않는다. 사용자가 Mac에서 VPN에 로그인하면 Mac LaunchAgent가 수집한다.
- Slack은 설치된 앱 토큰으로 LXC가 수집한다.
- 정규화 활동·업무 대상·보고서는 PostgreSQL에 장기 보관한다.
- raw API JSON은 DB에서 180일 보관한 뒤 zstd+age 암호화본을 로컬·R2에 보관한다.
- R2 raw/v1/은 무기한 Bucket Lock 상태여야 한다.

## 3. 비밀값 보관 위치

| 비밀 | 보관 위치 | 사용 주체 |
|---|---|---|
| 읽기 API Bearer 토큰 | /etc/work-history/credentials/read-api-token | API 클라이언트 |
| Atlassian API 토큰 | /etc/work-history/credentials/atlassian-api-token | sync, reconcile, backfill |
| Slack user token | /etc/work-history/credentials/slack-user-token | Slack daily/backfill |
| Slack app token | /etc/work-history/credentials/slack-app-token | Socket Mode |
| R2 Access Key ID | /etc/work-history/credentials/r2-access-key-id | archive/verify, DB 외부 백업 |
| R2 Secret Access Key | /etc/work-history/credentials/r2-secret-access-key | archive/verify, DB 외부 백업 |
| Mac GitLab PAT·장치 개인키 | macOS Keychain | GitLab 에이전트 |
| Report Agent 개인키 | Keychain service com.workhistory.report-agent | 보고서 서명 |
| age 개인키 | Keychain service com.workhistory.raw-archive + 비밀번호 관리자 복구본 | 원본 복호화 |

실제 값은 문서에 넣지 않는다. LXC에서 읽기 토큰이 정말 필요한 프로세스는 다음 명령을 직접 실행한다.

    /opt/work-history/source/deploy/server/show-read-token.sh

토큰을 회전하거나 교체했으면 기존 값을 폐기하고 권한 0600, 소유자 root:root를 유지한다.

## 4. API 계약

기본 URL: https://work-history.example.com

읽기 API 인증 헤더:

    Authorization: Bearer <READ_API_TOKEN>

| 메서드 | 엔드포인트 | 용도 |
|---|---|---|
| GET | /healthz | API·DB 상태 |
| GET | /v1/sync-status | 소스별 최근 수집 상태, systemd unit 실패 기록 |
| GET | /v1/archive-status | 아카이브 원장·R2 목록 비교·archive/verify/DB 외부 백업 작업 상태 |
| GET | /v1/activities?from=&to=&sources=&cursor=&limit= | 업무 활동. 최대 31일/요청, 500건/페이지 |
| GET | /v1/artifacts/{source}/{remote_id} | 이슈·페이지·MR·커밋 상세 |
| GET | /v1/reports?cadence=&kind=&status=&from=&to= | 보고서 목록 |
| GET | /v1/reports/{cadence}/{period}/{work_report|feedback} | 보고서 본문 |
| GET | /v1/ingest/gitlab/checkpoint | Mac GitLab 체크포인트 |
| POST | /v1/ingest/gitlab/batches | GitLab 장치 서명 batch |
| POST | /v1/report-agent/context | Report Agent 서명 문맥 |
| POST | /v1/report-agent/missing | 누락 보고서 기간 |
| PUT | /v1/reports/{cadence}/{period}/{work_report|feedback} | 보고서 저장 |

GitLab 수신과 Report Agent API는 Bearer 토큰이 아니라 장치별 Ed25519 키, 시각, nonce, 본문 해시를 검증한다. 장치 키를 서로 재사용하지 않는다.

## 5. LXC 설치와 배포

권장 LXC: 2 vCPU, RAM 4 GiB, swap 512 MiB, 디스크 40 GiB, 고정 DHCP 주소 192.0.2.4.

    git clone git@github.com:ParkWonYeop/work-history.git
    cd work-history
    ./deploy/server/install.sh

기본 환경 파일 /etc/work-history/server.env:

    DATABASE_URL=postgresql+psycopg:///workhistory?host=/var/run/postgresql
    PUBLIC_BASE_URL=https://work-history.example.com
    DEFAULT_TIMEZONE=Asia/Seoul
    RAW_RETENTION_DAYS=180
    FORWARDED_ALLOW_IPS=192.0.2.111
    LOG_LEVEL=INFO

Atlassian·Slack·R2는 환경 파일에 토큰을 직접 쓰지 않고 다음 스크립트로 설정한다.

    /opt/work-history/source/deploy/server/configure-atlassian.sh
    /opt/work-history/source/deploy/server/configure-slack.sh
    /opt/work-history/source/deploy/server/configure-archive.sh

## 6. NPM·방화벽

NPM Proxy Host: work-history.example.com → http://192.0.2.4:8080. SSL 인증서와 Force SSL을 활성화하고 deploy/npm/advanced.conf를 적용한다.

LXC nftables 규칙은 아래만 허용해야 한다.

    loopback TCP/8080      allow
    192.0.2.111 TCP/8080 allow
    그 외 IPv4/IPv6 8080   drop

PostgreSQL은 Unix socket/localhost 전용으로 유지한다. 공유기 SSH 포트포워딩은 평상시 제거한다.

## 7. Mac GitLab 에이전트

설치 위치:

    /Users/you/Library/Application Support/WorkHistoryAgent/
    ~/Library/LaunchAgents/com.workhistory.gitlab-agent.plist

설치·활성화·업데이트:

    deploy/macos/install-agent.sh
    deploy/macos/enable-agent.sh
    deploy/macos/update-agent.sh

- 09:00~17:00 KST에 실행하고, 실패하면 업무시간 동안 재시도한다.
- VPN 미연결이면 조용히 종료한다.
- FortiClient 실행·로그인·OTP·메일 MFA 자동화는 하지 않는다.
- GitLab Events API에 scope=all을 붙이지 않는다.
- 실제 author_id를 저장하고 actor_is_self는 author_id와 로그인 사용자 ID 비교 결과로 계산한다.

## 8. 수집 범위

- Jira: 본인 account ID 기준 이슈, 변경이력, 댓글, worklog, 담당 변경.
- Confluence: creator=currentUser()·contributor=currentUser() 후보의 본문, 버전, 댓글.
- Slack: 본인이 속한 채널·그룹 DM·본인 1:1 DM의 메시지.
- 보고서 Slack 문맥: 본인 활동, 본인 참여 스레드, 멘션, 본인 메시지 반응, 본인 포함 DM 우선.
- 타인의 일반 채널 대화는 DB에 보존될 수 있어도 본인 성과로 평가하지 않는다.

## 9. 자동 일정 (Asia/Seoul)

| 시각/주기 | 작업 |
|---|---|
| 부팅 5분 뒤, 이후 10분마다 | Jira·Confluence 증분 수집 |
| 09:05~17:05 매시 | 전날 Slack 재검증, 최대 2분 지터 |
| 매일 02:30 | DB backup + 별도 DB 실제 복원 시험 + age 암호화본 R2 db/v1/ 업로드(7개 보관) |
| 매일 03:30 | 만료 7일 전 raw JSON 암호화 아카이브 + R2 버킷 목록·원장 비교 기록 |
| 매일 03:30 + 최대 5분 | 최근 14일 재검증 |
| 매일 04:10 | 검증된 아카이브 지문만 DB raw 삭제 |
| 일요일 05:00 | 로컬·R2 암호문 SHA-256 전체 검증 |
| 매일 11:00 | Codex Report Agent: gpt-5.6-sol / high |

모든 unit은 실패하면 work-history-failure@.service가 sync_runs에 source=systemd로 기록한다. Slack 앱은 읽기 권한만 있어 직접 알림을 보내지 않으며, Codex "R2 무료 한도 일일 점검" 자동화가 /v1/archive-status를 읽어 알린다.

보고서는 generated_reports와 generated_report_versions에 저장한다. Slack 최신성이 부족하면 partial 상태로 저장하고 원본이 최신화되면 재생성 대상이 된다.

## 10. Report Agent

공식 Mac 설치 경로:

    /Users/you/Library/Application Support/WorkHistoryReportAgent/venv/bin/work-history-report-agent
    /Users/you/Library/Application Support/WorkHistoryReportAgent/config.toml

설치·등록·업데이트:

    deploy/macos/install-report-agent.sh https://work-history.example.com codex-report-agent
    /opt/work-history/source/deploy/server/register-report-device.sh codex-report-agent PRINTED_PUBLIC_KEY
    deploy/macos/update-report-agent.sh

자동화 프롬프트: deploy/macos/report-automation-prompt.md.
모델: gpt-5.6-sol, reasoning: high.

## 11. 백필·아카이브 복구

GitLab 재수집은 VPN 연결된 Mac에서만 한다.

    work-history-agent --config '/Users/you/Library/Application Support/WorkHistoryAgent/config.toml' replay --from '2026-04-01T00:00:00+09:00' --to '<현재 KST 시각>' --chunk-days 7

R2 raw/v1/만 무기한 잠근다. db/v1/은 DB 백업 7개 보관 정리를 코드가 하므로 잠그지 않는다.

R2 오염 객체를 교체할 때는 새 객체 업로드·원격 검증·DB 원장 등록을 먼저 끝낸다. 그 뒤에만 Bucket Lock을 잠시 해제하여 키·크기·SHA-256이 모두 맞는 기존 객체 하나만 삭제하고 즉시 raw/v1/ 무기한 잠금을 다시 건다.

도구:

    deploy/macos/repack-archive.py
    deploy/server/archive-object-admin.py

## 12. 점검 명령

    curl --fail-with-body https://work-history.example.com/healthz
    curl --fail-with-body -H "Authorization: Bearer $READ_API_TOKEN" https://work-history.example.com/v1/archive-status
    systemctl list-timers 'work-history-*'
    journalctl -u work-history-sync.service -n 100 --no-pager
    journalctl -u work-history-slack-daily.service -n 100 --no-pager
    journalctl -u work-history-api.service -n 100 --no-pager
    journalctl -u work-history-backup.service -n 100 --no-pager
    systemctl start work-history-archive-verify.service
    journalctl -u work-history-archive-verify.service -n 100 --no-pager

## 13. 인수인계 체크리스트

- [ ] GitHub private repository 접근 권한
- [ ] Proxmox/LXC 콘솔 또는 긴급 SSH 경로 접근 권한
- [ ] 비밀을 평문으로 출력하지 않는 root/systemd 권한 또는 비밀번호 관리자 공유 절차
- [ ] Atlassian, Slack, GitLab, R2의 회사 정책상 권한 확인
- [ ] Mac Keychain의 GitLab·Report Agent·age 개인키와 비밀번호 관리자 age 복구본 확인
- [ ] NPM, Cloudflare DNS/SSL 관리 권한
- [ ] healthz 성공, backup 복원 성공, R2 verify 성공, /v1/archive-status의 jobs 전부 ok 확인
- [ ] 변경 전 pytest -q, ruff check ., 셸 문법 검사 실행
- [ ] 의존성 변경 시 constraints.txt 재생성(README 18장)
