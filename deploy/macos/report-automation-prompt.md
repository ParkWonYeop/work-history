# Work-history daily report automation

Use only the signed `.venv/bin/work-history-report-agent` client with its default config. Never print, read, or request its
Keychain signing key. Store temporary context and Markdown only below `.report-tmp`, set files
to mode 600, and remove them after a confirmed upload.
Before cleanup, verify `.report-tmp` is a real directory and not a symbolic link. Remove only the
exact files created by the current run; never use a wildcard or recursive deletion.

All dates use `Asia/Seoul`. On every run:

1. Determine yesterday's calendar date.
2. Ask the agent for missing or changed-partial daily periods from `2026-04-01` through
   yesterday. Process yesterday first, then up to two older periods.
3. Fetch each period's daily context. If `activity_count` is zero, use `upload-empty`.
4. Otherwise create and upload two substantial Korean Markdown documents. Write in complete,
   specific sentences and develop each section as far as the evidence permits. Do not reduce the
   documents to a short event list, but do not add filler or repeat the same fact merely to make
   them longer.

   The `work_report` must contain these sections:

   - `종합 정리`: explain the day's overall purpose, main workstreams, how the separate activities
     connect, meaningful outcomes, current state, and remaining work. Use two or more paragraphs
     when the evidence supports them.
   - `주요 업무 한눈에 보기`: give a compact workstream table or list with objective, actions,
     result/current state, and evidence.
   - `시간순 업무 진행`: reconstruct the sequence of work and explain why each transition or
     follow-up happened, instead of copying API events verbatim.
   - `업무별 상세 내용`: group related events into coherent work items. For every work item cover
     background/objective, concrete actions and artifacts, technical or business reasoning,
     decisions and trade-offs, collaboration/review, result and verification, remaining work, and
     evidence links. Describe what was actually changed or investigated in enough detail that a
     reader unfamiliar with the day can understand the work.
   - `결정·협업·문서화`: describe decisions, review exchanges, hand-offs, comments, and documents,
     including their effect on the work.
   - `문제·장애물·미해결 사항`: distinguish resolved issues, active blockers, risks, and facts that
     cannot be confirmed from the collected data.
   - `다음 작업 및 우선순위`: list concrete follow-up work in priority order and explain the reason
     and completion condition for each item.
   - `데이터 완전성 및 근거`: state source freshness, gaps, redactions or truncation, and provide
     Jira, Confluence, GitLab, or Slack links near the claims they support.

   The `feedback` must contain these sections:

   - `종합 평가`: provide a detailed, balanced assessment of the day's work pattern and outcomes.
     Cover execution, prioritization, problem solving, quality/verification, ownership,
     collaboration, and documentation. Connect every conclusion to evidence and explicitly label
     reasonable inference as inference.
   - `잘한 점과 유지할 업무 방식`: identify specific habits or approaches worth preserving. For
     each, explain the observed behavior, why it helped, when it should be repeated, and how to keep
     it sustainable.
   - `개선할 점과 원인`: identify concrete bottlenecks or risky patterns. For each, separate the
     observed symptom, likely cause, impact, and confidence/limitation; avoid generic advice.
   - `추천 개선 방법`: for every material improvement, use the structure `현재 방식 → 권장 방식 →
     실행 방법 → 기대 효과 → 확인 기준`. Recommendations must be realistic for the user's Jira,
     Confluence, GitLab, Slack, review, and development workflow.
   - `앞으로의 업무 진행 방향`: recommend how to plan, sequence, document, validate, and close work
     going forward. Include short-term priority, a repeatable daily work loop, communication and
     documentation checkpoints, and rules for handling blockers or scope changes.
   - `다음 근무일 실행 항목`: give three prioritized, immediately actionable items with purpose and
     a clear done condition.
   - `1~2주 개선 실험`: when evidence permits, propose one or two small experiments and measurable
     signals for deciding whether to keep them.
   - `평가의 한계`: explain missing sources, incomplete context, and anything that must not be
     inferred.

   When enough evidence exists, the work report should normally be about 1,500-3,500 Korean
   characters and the feedback about 1,200-3,000 Korean characters. Exceed these ranges when the
   work genuinely requires it; use shorter documents when evidence is sparse rather than inventing
   detail.
5. Treat counts as context, never as a productivity score. Separate observations from
   inferences, do not invent impact, intent, completion, or causality, and cite Jira, Confluence,
   GitLab, or Slack URLs near factual work claims. Slack events with `actor_is_self=false` are
   collaboration context, not the user's own output. Use them only to explain requests, decisions,
   reviews, blockers, or outcomes connected to the user's work; never count general channel traffic
   as the user's productivity. Synthesize related events, but preserve important technical details,
   decisions, and unresolved uncertainty.
6. Upload with prompt version `work-history-report-v2` and model `gpt-5.6-sol`. The client
   decides `partial` versus `final` from source freshness.
7. On the first calendar day of a month, after the previous day is stored, process the previous
   month's missing or changed-partial monthly period. Apply the same detailed structure at monthly
   scale. The monthly work report must synthesize the month's overall direction, workstreams,
   significant deliverables, decisions and trade-offs, verified impact, collaboration, unresolved
   risks, and carry-over work, supported by stored daily documents and source evidence. The monthly
   feedback must include a detailed `종합 평가`, recurring strengths to preserve, recurring patterns
   to correct, concrete improvement recommendations, and an actionable next-month operating plan
   with priorities, routines, experiments, and success signals.
8. On every Monday, after the previous day's daily documents are stored, process the previous
   completed Monday-through-Sunday ISO week. Ask for missing or changed-partial weekly periods from
   the ISO week containing `2026-04-01` through the previous Sunday. Process the immediately
   preceding week first, then up to two older periods so an interrupted backlog catches up without
   delaying the daily report indefinitely. Weekly period keys use `YYYY-Www`, for example
   `2026-W14`.

   Fetch the weekly context and create both `work_report` and `feedback` even when the week has no
   activity; do not use the daily `upload-empty` template for a whole week. Use the same evidence,
   factuality, citation, and detailed feedback rules above at weekly scale. The weekly work report
   must synthesize the week's objectives, connected workstreams, chronological progress,
   deliverables, decisions and trade-offs, verification, collaboration, unresolved risks, and
   carry-over work. The weekly feedback must include a detailed `종합 평가`, strengths to preserve,
   patterns to correct, concrete recommendations using `현재 방식 → 권장 방식 → 실행 방법 → 기대
   효과 → 확인 기준`, and an actionable next-week plan. Use stored daily documents as supporting
   evidence and reconcile them with the Jira, Confluence, GitLab, and Slack source records in the
   weekly context. When evidence is sufficient, the weekly work report should normally be about
   3,000-6,000 Korean characters and the feedback about 2,000-4,000 Korean characters; let the
   evidence, not a quota, determine the final length.
9. If the API or upload fails, keep the server state unchanged, remove temporary files, and
   report the concise error so the next scheduled run can resume from `missing`.

When no period needs work, finish without creating a document.
