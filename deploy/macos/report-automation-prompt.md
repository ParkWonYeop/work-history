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
4. Otherwise create and upload two Korean Markdown documents:
   - `work_report`: summary, chronological activity, detailed work by goal/action/result,
     decisions and collaboration, blockers and unresolved work, next work, data completeness,
     and evidence links.
   - `feedback`: evidence-based assessment, strengths, bottlenecks and improvements,
     collaboration and documentation, three next-workday actions, and limitations.
5. Treat counts as context, never as a productivity score. Separate observations from
   inferences, do not invent impact, and cite Jira, Confluence, or GitLab URLs for factual work.
6. Upload with prompt version `work-history-report-v1` and model `gpt-5.6-sol`. The client
   decides `partial` versus `final` from source freshness.
7. On the first calendar day of a month, after the previous day is stored, process the previous
   month's missing or changed-partial monthly period. Its work report covers outcomes,
   workstreams, deliverables, decisions, impact, blockers, carry-over work, and evidence. Its
   feedback covers recurring strengths, bottlenecks, planning, collaboration, quality,
   experiments, and next-month goals.
8. If the API or upload fails, keep the server state unchanged, remove temporary files, and
   report the concise error so the next scheduled run can resume from `missing`.

When no period needs work, finish without creating a document.
