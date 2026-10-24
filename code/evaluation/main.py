
import argparse
import csv
import json
import re
from pathlib import Path
from collections import Counter

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "dataset"

EXPECTED_COLUMNS = [
    "case_id",
    "deletion_status",
    "steps_json",
]

ALLOWED_STATUSES = {
    "COMPLETE",
    "INCOMPLETE",
    "NOT_APPLICABLE",
}

STEP_FIELDS = [
    "step_no",
    "phase",
    "system_id",
    "resource_id",
    "operation",
    "state",
    "evidence_id",
]


def read_csv(path):
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def case_sort(row):
    case_id = row.get("case_id", "")
    match = re.search(r"(\d+)$", case_id)
    return int(match.group(1)) if match else float("inf")


def validate_predictions(rows):
    errors = []
    seen = set()

    for row_number, row in enumerate(rows, start=2):
        case_id = row.get("case_id", "")

        if not case_id:
            errors.append(f"Row {row_number}: missing case_id")
        elif case_id in seen:
            errors.append(f"Row {row_number}: duplicate case_id {case_id}")

        seen.add(case_id)

        status = row.get("deletion_status", "")
        if status not in ALLOWED_STATUSES:
            errors.append(
                f"{case_id}: invalid deletion_status {status!r}"
            )

        try:
            steps = json.loads(row.get("steps_json", ""))
        except (json.JSONDecodeError, TypeError):
            errors.append(f"{case_id}: invalid steps_json")
            continue

        if not isinstance(steps, list):
            errors.append(f"{case_id}: steps_json must be a JSON array")
            continue

        if status == "NOT_APPLICABLE" and steps != []:
            errors.append(
                f"{case_id}: NOT_APPLICABLE must have an empty steps array"
            )

        for index, step in enumerate(steps, start=1):
            if not isinstance(step, dict):
                errors.append(
                    f"{case_id}: step {index} must be a JSON object"
                )
                continue

            if list(step.keys()) != STEP_FIELDS:
                errors.append(
                    f"{case_id}: step {index} has incorrect fields"
                )

            if step.get("step_no") != index:
                errors.append(
                    f"{case_id}: step numbers must be consecutive"
                )

            if step.get("phase") not in {
                "FINDING", "REMEDIATION", "VERIFICATION"
            }:
                errors.append(
                    f"{case_id}: invalid phase in step {index}"
                )

        phases = [
            step.get("phase")
            for step in steps
            if isinstance(step, dict)
        ]

        phase_order = {
            "FINDING": 0,
            "REMEDIATION": 1,
            "VERIFICATION": 2,
        }

        if phases != sorted(
            phases, key=lambda phase: phase_order.get(phase, 99)
        ):
            errors.append(f"{case_id}: phases are out of order")

    return errors


def compare_with_sample(predictions, expected):
    predicted_by_id = {
        row["case_id"]: row for row in predictions
    }
    expected_by_id = {
        row["case_id"]: row for row in expected
    }

    common_ids = set(predicted_by_id) & set(expected_by_id)
    missing_ids = set(expected_by_id) - set(predicted_by_id)
    extra_ids = set(predicted_by_id) - set(expected_by_id)

    matches = 0
    mismatches = []

    for case_id in sorted(
        common_ids,
        key=lambda value: (
            int(re.search(r"\d+$", value).group())
            if re.search(r"\d+$", value) else float("inf")
        ),
    ):
        predicted = predicted_by_id[case_id]["deletion_status"]
        actual = expected_by_id[case_id]["deletion_status"]

        if predicted == actual:
            matches += 1
        else:
            mismatches.append({
                "case_id": case_id,
                "expected": actual,
                "predicted": predicted,
            })

    accuracy = (
        matches / len(common_ids) * 100
        if common_ids else 0.0
    )

    print("\n===== SAMPLE EVALUATION =====")
    print(f"Expected sample cases : {len(expected)}")
    print(f"Predicted cases       : {len(predictions)}")
    print(f"Cases compared        : {len(common_ids)}")
    print(f"Status matches        : {matches}")
    print(f"Status mismatches     : {len(mismatches)}")
    print(f"Status accuracy       : {accuracy:.2f}%")
    print(f"Missing case IDs      : {len(missing_ids)}")
    print(f"Unexpected case IDs   : {len(extra_ids)}")

    if mismatches:
        print("\n===== STATUS MISMATCHES =====")
        for item in mismatches:
            print(
                f"{item['case_id']}: "
                f"expected={item['expected']}, "
                f"predicted={item['predicted']}"
            )

    if missing_ids:
        print("\nMissing IDs:", ", ".join(sorted(missing_ids)))

    if extra_ids:
        print("\nUnexpected IDs:", ", ".join(sorted(extra_ids)))

    return accuracy


def main():
    parser = argparse.ArgumentParser(
        description="Evaluate deletion-audit predictions"
    )
    parser.add_argument(
        "--predictions",
        default=str(ROOT / "sample_predictions.csv"),
        help="Prediction CSV to evaluate",
    )
    parser.add_argument(
        "--expected",
        default=str(DATA / "sample_output.csv"),
        help="Ground-truth sample CSV",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Also validate the main output.csv file",
    )
    args = parser.parse_args()

    prediction_path = Path(args.predictions)
    expected_path = Path(args.expected)

    if not prediction_path.exists():
        raise SystemExit(
            f"Prediction file not found: {prediction_path}"
        )

    if not expected_path.exists():
        raise SystemExit(
            f"Expected sample file not found: {expected_path}"
        )

    predictions = read_csv(prediction_path)
    expected = read_csv(expected_path)

    if predictions and list(predictions[0].keys()) != EXPECTED_COLUMNS:
        print("WARNING: prediction CSV columns do not match expected schema.")

    errors = validate_predictions(predictions)

    print("===== STRUCTURE VALIDATION =====")
    if errors:
        print(f"Validation errors: {len(errors)}")
        for error in errors:
            print("-", error)
    else:
        print("CSV and JSON structure: PASS")

    compare_with_sample(predictions, expected)

    if args.full:
        full_path = ROOT / "output.csv"

        if not full_path.exists():
            print("\nFull output.csv not found.")
        else:
            full_rows = read_csv(full_path)
            full_errors = validate_predictions(full_rows)

            print("\n===== FULL OUTPUT VALIDATION =====")
            print(f"Rows in output.csv: {len(full_rows)}")

            if full_errors:
                print(f"Validation errors: {len(full_errors)}")
                for error in full_errors:
                    print("-", error)
            else:
                print("Full output structure: PASS")


if __name__ == "__main__":
    main()
