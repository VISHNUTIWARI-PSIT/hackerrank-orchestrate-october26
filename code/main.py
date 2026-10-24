
import argparse
import csv
import json
import re
import sys
from pathlib import Path
from datetime import datetime, timezone


ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "dataset"
EVIDENCE_DIR = DATA / "evidence"
DEFAULT_INPUT = DATA / "deletion_requests.csv"
DEFAULT_OUTPUT = ROOT / "output.csv"
LOG_FILE = ROOT / "log.txt"

OUTPUT_COLUMNS = ["case_id", "deletion_status", "steps_json"]
STEP_FIELDS = [
    "step_no", "phase", "system_id", "resource_id",
    "operation", "state", "evidence_id"
]

DELETE_RE = re.compile(
    r"\b(delete|deletion|deleted|erase|erasure|remove|removal|"
    r"purge|destroy|forget|close my account|close this account|"
    r"account closure|right to be forgotten|"
    r"eliminar|eliminaci[oó]n|borrar|suprimir|supresi[oó]n|"
    r"effacer|suppression|supprimer|l[oö]schen|entfernen|"
    r"削除|消去|アカウントを削除)\b",
    re.IGNORECASE,
)

NON_DELETE_RE = re.compile(
    r"\b(export my data|download my data|access request|"
    r"correct my data|update my data|change my email|"
    r"opt.?out|unsubscribe|data portability)\b",
    re.IGNORECASE,
)

IDENTIFIER_FIELDS = {
    "email", "email_address", "email_hash", "user_email",
    "user_id", "identity_id", "account_id", "customer_id",
    "subject_id", "person_id", "username", "phone",
    "phone_number", "external_id", "old_email", "new_email",
}

TIME_FIELDS = {
    "audit_at", "audit_time", "event_time", "timestamp",
    "created_at", "updated_at", "captured_at", "occurred_at",
    "request_time", "reviewed_at", "completed_at",
}

ID_FIELDS = {
    "file_id", "activity_id", "audit_id", "evidence_id",
    "request_id", "case_id", "record_id", "resource_id",
    "system_id", "user_id", "identity_id",
}

STATUS_GOOD = {
    "deleted", "absent", "removed", "purged", "redacted",
    "anonymized", "anonymised", "blocked", "disabled",
    "complete", "completed", "success", "successful",
    "not_found", "not found",
}

STATUS_BAD = {
    "present", "active", "enabled", "failed", "pending",
    "incomplete", "exists", "found", "restorable",
    "unredacted", "identifiable",
}


def log(message):
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(message.rstrip() + "\n")
    except OSError:
        pass


def read_csv(path):
    if not path.exists():
        return []

    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            with path.open("r", encoding=encoding, newline="") as f:
                return [
                    {str(k).strip(): (v or "").strip()
                     for k, v in row.items() if k is not None}
                    for row in csv.DictReader(f)
                ]
        except (UnicodeDecodeError, csv.Error):
            continue
        except OSError:
            return []

    return []


def key_name(value):
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def get_value(row, *names):
    if not row:
        return ""

    normalized = {key_name(k): v for k, v in row.items()}

    for name in names:
        value = normalized.get(key_name(name), "")
        if value is not None and str(value).strip():
            return str(value).strip()

    return ""


def parse_time(value):
    if not value:
        return None

    value = str(value).strip()

    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        pass

    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y",
        "%d-%m-%Y %H:%M:%S",
        "%d-%m-%Y",
    ):
        try:
            return datetime.strptime(value, fmt).replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue

    return None


def before_cutoff(row, cutoff):
    if cutoff is None:
        return True

    for field, value in row.items():
        if key_name(field) in {key_name(x) for x in TIME_FIELDS}:
            parsed = parse_time(value)
            if parsed is not None:
                return parsed <= cutoff

    return True


def text_blob(row):
    return " ".join(
        f"{key}: {value}"
        for key, value in row.items()
        if value is not None and str(value).strip()
    )


def deletion_intent(row):
    text = text_blob(row).lower()

    if NON_DELETE_RE.search(text):
        # A request may mention both deletion and another action.
        # Continue when explicit account deletion is also requested.
        explicit_delete = bool(
            re.search(
                r"\b(delete|deletion|erase|remove|purge|"
                r"eliminar|eliminaci[oó]n|borrar|effacer|"
                r"suppression|supprimer|削除)\b",
                text,
                re.IGNORECASE,
            )
        )
        if not explicit_delete:
            return False

    return bool(DELETE_RE.search(text))


def load_all_csvs():
    result = {}

    for path in DATA.rglob("*.csv"):
        if path.name.lower() == "output.csv":
            continue
        result[path.name.lower()] = read_csv(path)

    return result


def build_identity_index(tables, cutoff):
    identities = {}
    aliases = {}

    for filename in (
        "user_identities.csv",
        "user_audit_log.csv",
    ):
        for row in tables.get(filename, []):
            if not before_cutoff(row, cutoff):
                continue

            uid = get_value(
                row, "user_id", "identity_id", "account_id",
                "subject_id", "customer_id"
            )

            if not uid:
                continue

            identities.setdefault(uid, set()).add(uid)

            for field, value in row.items():
                if not value:
                    continue

                fname = key_name(field)

                if (
                    fname in {key_name(x) for x in IDENTIFIER_FIELDS}
                    or "email" in fname
                    or "phone" in fname
                ):
                    value = value.strip().lower()
                    if len(value) >= 3:
                        identities[uid].add(value)
                        aliases.setdefault(value, set()).add(uid)

            old_value = get_value(
                row, "old_value", "previous_value", "old_email"
            )
            new_value = get_value(
                row, "new_value", "current_value", "new_email"
            )

            for value in (old_value, new_value):
                if value:
                    value = value.lower()
                    identities[uid].add(value)
                    aliases.setdefault(value, set()).add(uid)

    return identities, aliases


def resolve_subject(request, identities, aliases):
    candidates = set()
    request_text = text_blob(request).lower()

    direct_ids = [
        get_value(request, field)
        for field in (
            "user_id", "identity_id", "account_id",
            "subject_id", "customer_id"
        )
    ]

    for value in direct_ids:
        if value and value in identities:
            candidates.add(value)

    for alias, user_ids in aliases.items():
        if alias and alias in request_text:
            candidates.update(user_ids)

    matched_values = set()
    for uid in candidates:
        matched_values.update(identities.get(uid, set()))

    for value in direct_ids:
        if value:
            matched_values.add(value.lower())

    # Include direct email-like identifiers in the request.
    emails = re.findall(
        r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}",
        request_text,
        re.IGNORECASE,
    )
    matched_values.update(email.lower() for email in emails)

    return candidates, matched_values


def load_manifest(tables):
    manifest = []

    for row in tables.get("evidence_files.csv", []):
        path_value = get_value(
            row, "file_path", "path", "relative_path", "filename"
        )
        file_id = get_value(row, "file_id", "evidence_id", "id")

        if path_value:
            manifest.append({
                "file_id": file_id or path_value,
                "path": path_value,
                "captured_at": get_value(
                    row, "captured_at", "created_at", "timestamp"
                ),
                "row": row,
            })

    return manifest


def safe_evidence_path(relative_path):
    candidate = (DATA / relative_path).resolve()

    try:
        candidate.relative_to(DATA.resolve())
    except ValueError:
        return None

    if candidate.is_file():
        return candidate

    # Some manifests store paths relative to the evidence directory.
    candidate = (EVIDENCE_DIR / relative_path).resolve()

    try:
        candidate.relative_to(EVIDENCE_DIR.resolve())
    except ValueError:
        return None

    return candidate if candidate.is_file() else None


def read_text_file(path):
    suffix = path.suffix.lower()

    if suffix in {
        ".txt", ".log", ".json", ".sql", ".csv", ".xml",
        ".html", ".md", ".yaml", ".yml", ".jsonl"
    }:
        for encoding in ("utf-8-sig", "utf-8", "latin-1"):
            try:
                return path.read_text(encoding=encoding, errors="replace")
            except OSError:
                return ""

    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
            reader = PdfReader(str(path))
            return "\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception:
            return ""

    return ""


def evidence_documents(tables, cutoff):
    docs = []
    manifest = load_manifest(tables)
    known_paths = set()

    for item in manifest:
        captured = parse_time(item["captured_at"])
        if cutoff and captured and captured > cutoff:
            continue

        path = safe_evidence_path(item["path"])
        if not path:
            continue

        known_paths.add(path.resolve())

        content = read_text_file(path)
        if content:
            docs.append({
                "evidence_id": item["file_id"],
                "system_id": get_value(
                    item["row"], "system_id", "system", "source_system"
                ) or "unknown",
                "resource_id": get_value(
                    item["row"], "resource_id", "record_id", "object_id"
                ) or path.stem,
                "text": content,
                "row": item["row"],
            })

    if EVIDENCE_DIR.exists():
        for path in EVIDENCE_DIR.rglob("*"):
            if not path.is_file() or path.resolve() in known_paths:
                continue

            content = read_text_file(path)
            if content:
                docs.append({
                    "evidence_id": path.relative_to(DATA).as_posix(),
                    "system_id": "unknown",
                    "resource_id": path.stem,
                    "text": content,
                    "row": {},
                })

    return docs


def resource_category(text):
    t = text.lower()

    patterns = [
        (r"backup|archive|snapshot|restore point", "backup"),
        (r"search index|search result|indexed document", "search_index"),
        (r"cache|cached", "cache"),
        (r"support attachment|support ticket|support message", "support"),
        (r"email delivery|delivery log|mail log", "email_log"),
        (r"security audit|audit log|security log", "security_log"),
        (r"legal hold|litigation hold|retention hold", "legal_hold"),
        (r"analytics|statistics|telemetry profile", "analytics"),
        (r"marketing profile|marketing", "marketing"),
        (r"tombstone|deleted.account reference", "tombstone"),
        (r"export file|user export|data export", "export"),
        (r"replication|replica|synchronization|sync job", "data_flow"),
        (r"user profile|customer profile|account record", "profile"),
    ]

    for pattern, category in patterns:
        if re.search(pattern, t):
            return category

    return "record"


def policy_action(category, combined_text):
    text = combined_text.lower()

    if re.search(r"legal hold|litigation hold|mandatory retention", text):
        return "RETAIN"

    if category == "data_flow":
        return "DISABLE_DATA_FLOW"

    if category == "backup":
        if re.search(r"restore enabled|restorable|can restore", text):
            return "BLOCK_RESTORE"
        return "DELETE_RECORD"

    if category in {"security_log", "email_log"}:
        if re.search(r"must retain|retention required|legally required", text):
            return "REDACT_RECORD"
        return "REDACT_RECORD"

    if category == "analytics":
        if re.search(r"anonymous|aggregate only|anonymiz", text):
            return "ANONYMIZE_RECORD"
        return "ANONYMIZE_RECORD"

    if category == "tombstone":
        return "RETAIN"

    if re.search(r"redact|redaction", text):
        return "REDACT_RECORD"

    if re.search(r"anonym|aggregate", text):
        return "ANONYMIZE_RECORD"

    return "DELETE_RECORD"


def evidence_matches_identifiers(doc, identifiers):
    text = (doc.get("text", "") + " " + text_blob(doc.get("row", {}))).lower()

    for identifier in identifiers:
        if not identifier or len(identifier) < 3:
            continue

        if identifier in text:
            return True

    return False


def find_status_clues(text):
    t = text.lower()

    bad = []
    good = []

    bad_patterns = [
        (r"deleted\s*=\s*false", "record still present"),
        (r"still exists|record present|query hit|record found", "record still present"),
        (r"contains email|exposed pii|pii scan.*present", "possible exposed PII"),
        (r"restore enabled|restorable|can restore", "restore risk"),
        (r"sync active|replication active|status active", "active data flow"),
        (r"deletion failed|delete failed|operation failed", "deletion failure"),
        (r"identifiable analytics|analytics.*identifiable", "identifiable analytics"),
    ]

    good_patterns = [
        (r"deleted\s*=\s*true|record absent|not found|no record", "record absent"),
        (r"redacted\s*=\s*true|pii removed|redaction complete", "record redacted"),
        (r"anonymized\s*=\s*true|anonymised\s*=\s*true|anonymous only", "record anonymized"),
        (r"restore blocked|restore disabled", "restore blocked"),
        (r"sync disabled|replication disabled|flow disabled", "data flow disabled"),
        (r"deletion complete|deletion successful|delete successful", "deletion completed"),
    ]

    for pattern, clue in bad_patterns:
        if re.search(pattern, t):
            bad.append(clue)

    for pattern, clue in good_patterns:
        if re.search(pattern, t):
            good.append(clue)

    return bad, good


def make_step(step_no, phase, system_id, resource_id,
              operation, state, evidence_id=""):
    return {
        "step_no": step_no,
        "phase": phase,
        "system_id": str(system_id or "unknown"),
        "resource_id": str(resource_id or "unknown"),
        "operation": operation,
        "state": state,
        "evidence_id": str(evidence_id or ""),
    }


def build_case(request, tables, all_requests):
    case_id = get_value(request, "case_id", "request_id", "id")
    request_text = text_blob(request)
    cutoff = parse_time(get_value(
        request, "audit_at", "audit_time", "reviewed_at", "created_at"
    ))

    if not deletion_intent(request):
        return {
            "case_id": case_id,
            "deletion_status": "NOT_APPLICABLE",
            "steps_json": "[]",
        }

    identities, aliases = build_identity_index(tables, cutoff)
    user_ids, identifiers = resolve_subject(request, identities, aliases)

    # Include email-like strings found directly in request fields.
    identifiers.update(
        x.lower() for x in re.findall(
            r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}",
            request_text, re.IGNORECASE
        )
    )

    docs = evidence_documents(tables, cutoff)

    activities = []
    for filename in ("system_activity.csv", "user_audit_log.csv"):
        for row in tables.get(filename, []):
            if not before_cutoff(row, cutoff):
                continue

            row_text = text_blob(row).lower()
            matches = any(
                identifier and len(identifier) >= 3 and identifier in row_text
                for identifier in identifiers
            )

            direct_user = get_value(
                row, "user_id", "identity_id", "account_id", "subject_id"
            )
            if matches or (direct_user and direct_user in user_ids):
                activities.append({
                    "evidence_id": get_value(
                        row, "activity_id", "audit_id", "event_id", "id"
                    ) or filename,
                    "system_id": get_value(
                        row, "system_id", "system", "source_system"
                    ) or "unknown",
                    "resource_id": get_value(
                        row, "resource_id", "record_id", "object_id"
                    ) or "unknown",
                    "text": row_text,
                    "row": row,
                })

    matched_docs = [
        doc for doc in docs
        if evidence_matches_identifiers(doc, identifiers)
    ]

    matched_docs.extend(activities)

    # A request with no resolvable identity or matching evidence cannot
    # safely be declared complete.
    findings = []
    remediation = []
    verifications = []
    unresolved = not identifiers or not matched_docs

    for doc in matched_docs:
        text = doc.get("text", "")
        row = doc.get("row", {})
        combined = text + " " + text_blob(row)

        category = resource_category(combined)
        action = policy_action(category, combined)
        bad_clues, good_clues = find_status_clues(combined)

        system_id = doc.get("system_id", "unknown")
        resource_id = doc.get("resource_id", "unknown")
        evidence_id = doc.get("evidence_id", "")

        # Only treat explicit evidence as proof of a problem or compliant state.
        if bad_clues:
            clue = bad_clues[0]
            if "restore" in clue:
                findings.append(make_step(
                    0, "FINDING", system_id, resource_id,
                    "REPORT_RESTORE_RISK", "ACTIVE", evidence_id
                ))
                remediation.append(make_step(
                    0, "REMEDIATION", system_id, resource_id,
                    "BLOCK_RESTORE", "BLOCKED", ""
                ))
                verifications.append(make_step(
                    0, "VERIFICATION", system_id, resource_id,
                    "VERIFY_RESTORE_BLOCKED", "BLOCKED", ""
                ))
            elif "flow" in clue:
                findings.append(make_step(
                    0, "FINDING", system_id, resource_id,
                    "REPORT_ACTIVE_DATA_FLOW", "ACTIVE", evidence_id
                ))
                remediation.append(make_step(
                    0, "REMEDIATION", system_id, resource_id,
                    "DISABLE_DATA_FLOW", "DISABLED", ""
                ))
                verifications.append(make_step(
                    0, "VERIFICATION", system_id, resource_id,
                    "VERIFY_DATA_FLOW_DISABLED", "DISABLED", ""
                ))
            elif "pii" in clue or "analytics" in clue:
                findings.append(make_step(
                    0, "FINDING", system_id, resource_id,
                    "REPORT_EXPOSED_PII", "PRESENT", evidence_id
                ))
                target_action = (
                    "ANONYMIZE_RECORD" if category == "analytics"
                    else "REDACT_RECORD"
                )
                target_state = (
                    "ANONYMIZED" if target_action == "ANONYMIZE_RECORD"
                    else "REDACTED"
                )
                remediation.append(make_step(
                    0, "REMEDIATION", system_id, resource_id,
                    target_action, target_state, ""
                ))
                verifications.append(make_step(
                    0, "VERIFICATION", system_id, resource_id,
                    "VERIFY_RECORD_ANONYMIZED" if target_state == "ANONYMIZED"
                    else "VERIFY_RECORD_REDACTED",
                    target_state, ""
                ))
            else:
                findings.append(make_step(
                    0, "FINDING", system_id, resource_id,
                    "REPORT_REMAINING_RECORD", "PRESENT", evidence_id
                ))

                if action == "RETAIN":
                    # Retention is not proof that the deletion request is compliant.
                    remediation.append(make_step(
                        0, "REMEDIATION", system_id, resource_id,
                        "REDACT_RECORD", "REDACTED", ""
                    ))
                    verifications.append(make_step(
                        0, "VERIFICATION", system_id, resource_id,
                        "VERIFY_RECORD_REDACTED", "REDACTED", ""
                    ))
                elif action == "REDACT_RECORD":
                    remediation.append(make_step(
                        0, "REMEDIATION", system_id, resource_id,
                        "REDACT_RECORD", "REDACTED", ""
                    ))
                    verifications.append(make_step(
                        0, "VERIFICATION", system_id, resource_id,
                        "VERIFY_RECORD_REDACTED", "REDACTED", ""
                    ))
                elif action == "ANONYMIZE_RECORD":
                    remediation.append(make_step(
                        0, "REMEDIATION", system_id, resource_id,
                        "ANONYMIZE_RECORD", "ANONYMIZED", ""
                    ))
                    verifications.append(make_step(
                        0, "VERIFICATION", system_id, resource_id,
                        "VERIFY_RECORD_ANONYMIZED", "ANONYMIZED", ""
                    ))
                elif action == "BLOCK_RESTORE":
                    remediation.append(make_step(
                        0, "REMEDIATION", system_id, resource_id,
                        "BLOCK_RESTORE", "BLOCKED", ""
                    ))
                    verifications.append(make_step(
                        0, "VERIFICATION", system_id, resource_id,
                        "VERIFY_RESTORE_BLOCKED", "BLOCKED", ""
                    ))
                elif action == "DISABLE_DATA_FLOW":
                    remediation.append(make_step(
                        0, "REMEDIATION", system_id, resource_id,
                        "DISABLE_DATA_FLOW", "DISABLED", ""
                    ))
                    verifications.append(make_step(
                        0, "VERIFICATION", system_id, resource_id,
                        "VERIFY_DATA_FLOW_DISABLED", "DISABLED", ""
                    ))
                else:
                    remediation.append(make_step(
                        0, "REMEDIATION", system_id, resource_id,
                        "DELETE_RECORD", "ABSENT", ""
                    ))
                    verifications.append(make_step(
                        0, "VERIFICATION", system_id, resource_id,
                        "VERIFY_RECORD_ABSENT", "ABSENT", ""
                    ))

        elif good_clues:
            # A positive clue is evidence-backed, but it must match the
            # requested resource state.
            clue = good_clues[0]

            if "restore blocked" in clue:
                verifications.append(make_step(
                    0, "VERIFICATION", system_id, resource_id,
                    "VERIFY_RESTORE_BLOCKED", "BLOCKED", evidence_id
                ))
            elif "flow disabled" in clue:
                verifications.append(make_step(
                    0, "VERIFICATION", system_id, resource_id,
                    "VERIFY_DATA_FLOW_DISABLED", "DISABLED", evidence_id
                ))
            elif "redacted" in clue:
                verifications.append(make_step(
                    0, "VERIFICATION", system_id, resource_id,
                    "VERIFY_RECORD_REDACTED", "REDACTED", evidence_id
                ))
            elif "anonymized" in clue:
                verifications.append(make_step(
                    0, "VERIFICATION", system_id, resource_id,
                    "VERIFY_RECORD_ANONYMIZED", "ANONYMIZED", evidence_id
                ))
            else:
                verifications.append(make_step(
                    0, "VERIFICATION", system_id, resource_id,
                    "VERIFY_RECORD_ABSENT", "ABSENT", evidence_id
                ))

    # Deduplicate equivalent steps.
    def dedupe(items):
        seen = set()
        result = []
        for item in items:
            key = tuple(item.get(field, "") for field in STEP_FIELDS if field != "step_no")
            if key not in seen:
                seen.add(key)
                result.append(item)
        return result

    findings = dedupe(findings)
    remediation = dedupe(remediation)
    verifications = dedupe(verifications)

    # Any unresolved identity/evidence or any known problem means the case
    # cannot be safely marked COMPLETE.
    if unresolved:
        remediation.append(make_step(
            0, "REMEDIATION", "unknown", "identity_resolution",
            "DELETE_RECORD", "ABSENT", ""
        ))
        verifications.append(make_step(
            0, "VERIFICATION", "unknown", "identity_resolution",
            "VERIFY_RECORD_ABSENT", "ABSENT", ""
        ))

    # COMPLETE only when there is positive, timestamp-eligible evidence and
    # no finding or future remediation required.
    has_problem = bool(findings or remediation)
    evidence_backed_verifications = [
        step for step in verifications if step.get("evidence_id")
    ]

    if (
        not unresolved
        and not has_problem
        and evidence_backed_verifications
        and len(evidence_backed_verifications) == len(verifications)
    ):
        status = "COMPLETE"
        steps = verifications
    else:
        status = "INCOMPLETE"
        steps = findings + remediation + verifications

        # If there is no explicit finding, record that verification is unresolved.
        if not findings:
            steps.insert(0, make_step(
                0, "FINDING", "unknown", "verification",
                "REPORT_REMAINING_RECORD", "PRESENT",
                matched_docs[0].get("evidence_id", "") if matched_docs else ""
            ))

    # Order by phase, then assign consecutive step numbers.
    phase_order = {
        "FINDING": 0,
        "REMEDIATION": 1,
        "VERIFICATION": 2,
    }
    steps.sort(key=lambda x: phase_order.get(x["phase"], 9))

    # Keep only valid, schema-compliant steps.
    clean_steps = []
    for item in steps:
        clean_steps.append({
            "step_no": len(clean_steps) + 1,
            "phase": item["phase"],
            "system_id": item["system_id"],
            "resource_id": item["resource_id"],
            "operation": item["operation"],
            "state": item["state"],
            "evidence_id": item["evidence_id"],
        })

    return {
        "case_id": case_id,
        "deletion_status": status,
        "steps_json": json.dumps(
            clean_steps, ensure_ascii=False, separators=(",", ":")
        ),
    }


def numeric_case_sort(row):
    value = get_value(row, "case_id")
    match = re.search(r"(\d+)$", value)
    return (0, int(match.group(1))) if match else (1, value)


def validate_output(rows):
    allowed_statuses = {"COMPLETE", "INCOMPLETE", "NOT_APPLICABLE"}

    for row in rows:
        if row["deletion_status"] not in allowed_statuses:
            raise ValueError(
                f"Invalid status for {row['case_id']}: "
                f"{row['deletion_status']}"
            )

        steps = json.loads(row["steps_json"])

        if row["deletion_status"] == "NOT_APPLICABLE" and steps:
            raise ValueError(
                f"NOT_APPLICABLE must have []: {row['case_id']}"
            )

        for step in steps:
            if list(step.keys()) != STEP_FIELDS:
                raise ValueError(
                    f"Invalid step schema for {row['case_id']}"
                )


def main():
    parser = argparse.ArgumentParser(
        description="Evidence-based deletion audit agent"
    )
    parser.add_argument(
        "--input", default=str(DEFAULT_INPUT),
        help="Input deletion requests CSV"
    )
    parser.add_argument(
        "--output", default=str(DEFAULT_OUTPUT),
        help="Output prediction CSV"
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)

    requests = read_csv(input_path)
    if not requests:
        print(f"ERROR: No input rows found in {input_path}", file=sys.stderr)
        sys.exit(1)

    tables = load_all_csvs()
    predictions = []

    for request in requests:
        try:
            predictions.append(build_case(request, tables, requests))
        except Exception as exc:
            case_id = get_value(request, "case_id", "request_id", "id")
            log(f"case_id={case_id} error={type(exc).__name__}: {exc}")

            if deletion_intent(request):
                predictions.append({
                    "case_id": case_id,
                    "deletion_status": "INCOMPLETE",
                    "steps_json": json.dumps([
                        make_step(
                            1, "FINDING", "unknown", "audit",
                            "REPORT_REMAINING_RECORD", "PRESENT", ""
                        ),
                        make_step(
                            2, "REMEDIATION", "unknown", "audit",
                            "DELETE_RECORD", "ABSENT", ""
                        ),
                        make_step(
                            3, "VERIFICATION", "unknown", "audit",
                            "VERIFY_RECORD_ABSENT", "ABSENT", ""
                        ),
                    ], ensure_ascii=False, separators=(",", ":")),
                })
            else:
                predictions.append({
                    "case_id": case_id,
                    "deletion_status": "NOT_APPLICABLE",
                    "steps_json": "[]",
                })

    predictions.sort(key=numeric_case_sort)
    validate_output(predictions)

    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(predictions)

    log(
        f"tool=python model=GPT-6 action=generate_output "
        f"input={input_path} rows={len(predictions)} output={output_path}"
    )

    print(f"Created: {output_path}")
    print(f"Input cases: {len(requests)}")
    print(f"Output rows: {len(predictions)}")
    print("CSV schema and JSON structure: PASS")


if __name__ == "__main__":
    main()
