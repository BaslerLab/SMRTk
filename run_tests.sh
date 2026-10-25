#!/usr/bin/env bash
# ==============================================================================
# SMRTk: Sandboxed Multi-agent Research Toolkit — Test Runner
# ==============================================================================
# Reads tests from tests.tsv (or custom TSV) where columns are:
#   1: Short name (used for session name: ./sessions/Session_ID_<name>)
#   2: Short description
#   3: Full prompt
#   4: Follow-up prompt (e.g. self-analysis, supports {SESSION_ID}, {SESSION_NAME})
#
# Usage:
#   ./run_tests.sh                   # Run all tests sequentially
#   ./run_tests.sh 1 3 9             # Run specific tests by number
#   ./run_tests.sh Test01* Test10*   # Run specific tests by name or wildcard pattern
#   ./run_tests.sh --analyze         # Run tests + follow-up prompt on each
#   ./run_tests.sh --analyze 1 2     # Run tests 1 & 2 + follow-up
#   ./run_tests.sh --list            # List all available tests
#   ./run_tests.sh --dry-run 1 2     # Preview commands without running
#   ./run_tests.sh --tsv custom.tsv  # Use a custom TSV file
# ==============================================================================

# Entire script is wrapped in main() so bash loads the full script into memory
# at launch, preventing buffer/offset shift errors if the file is modified during runs.
main() {
    set -uo pipefail

    local SCRIPT_DIR
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    cd "$SCRIPT_DIR"

    local TSV_FILE="${SCRIPT_DIR}/tests.tsv"
    local RUN_ANALYZE=false
    local LIST_ONLY=false
    local DRY_RUN=false
    local FORMAT="markdown"
    local VERBOSITY="standard"
    local TARGET_PATTERNS=()

    # --- Parse Arguments ---
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --list|-l)
                LIST_ONLY=true
                shift
                ;;
            --analyze|-a)
                RUN_ANALYZE=true
                shift
                ;;
            --dry-run|-d)
                DRY_RUN=true
                shift
                ;;
            --tsv)
                TSV_FILE="$2"
                shift 2
                ;;
            -f|--format)
                FORMAT="$2"
                shift 2
                ;;
            -v|--verbosity)
                VERBOSITY="$2"
                shift 2
                ;;
            -h|--help)
                sed -n '2,20p' "$0" | tr -d '#'
                return 0
                ;;
            *)
                TARGET_PATTERNS+=("$1")
                shift
                ;;
        esac
    done

    if [[ ! -f "$TSV_FILE" ]]; then
        echo "Error: TSV file '$TSV_FILE' not found." >&2
        return 1
    fi

    # --- Load Tests from TSV ---
    local -a TEST_NAMES=()
    local -a TEST_DESCS=()
    local -a TEST_PROMPTS=()
    local -a TEST_FOLLOWUPS=()

    while IFS=$'\t' read -r name desc prompt followup || [[ -n "$name" ]]; do
        [[ -z "$name" || "$name" =~ ^# ]] && continue
        TEST_NAMES+=("$name")
        TEST_DESCS+=("$desc")
        TEST_PROMPTS+=("$prompt")
        TEST_FOLLOWUPS+=("${followup:-}")
    done < "$TSV_FILE"

    local TOTAL_TESTS=${#TEST_NAMES[@]}

    # --- List Mode ---
    if [[ "$LIST_ONLY" == true ]]; then
        printf "\nAvailable Tests in %s (%d total):\n\n" "$(basename "$TSV_FILE")" "$TOTAL_TESTS"
        printf "  %-3s  %-35s  %s\n" "#" "Session Name" "Description"
        printf "  %-3s  %-35s  %s\n" "---" "-----------------------------------" "----------------------------------------"
        for i in "${!TEST_NAMES[@]}"; do
            local num=$((i + 1))
            printf "  %2d.  %-35s  %s\n" "$num" "${TEST_NAMES[$i]}" "${TEST_DESCS[$i]}"
        done
        echo ""
        return 0
    fi

    # --- Determine Which Tests to Run ---
    local -a RUN_INDICES=()

    if [[ ${#TARGET_PATTERNS[@]} -eq 0 ]]; then
        for i in "${!TEST_NAMES[@]}"; do
            RUN_INDICES+=("$i")
        done
    else
        for pat in "${TARGET_PATTERNS[@]}"; do
            local matched=false
            if [[ "$pat" =~ ^[0-9]+$ ]] && (( pat >= 1 && pat <= TOTAL_TESTS )); then
                local idx=$((pat - 1))
                RUN_INDICES+=("$idx")
                matched=true
            else
                for i in "${!TEST_NAMES[@]}"; do
                    if [[ "${TEST_NAMES[$i]}" == $pat ]]; then
                        RUN_INDICES+=("$i")
                        matched=true
                    fi
                done
            fi
            if [[ "$matched" == false ]]; then
                echo "Warning: No test matching '$pat' found." >&2
            fi
        done
    fi

    if [[ ${#RUN_INDICES[@]} -eq 0 ]]; then
        echo "No valid tests selected to run." >&2
        return 1
    fi

    # Deduplicate indices while preserving order
    local -a UNIQUE_INDICES=()
    local -A SEEN_INDICES=()
    for idx in "${RUN_INDICES[@]}"; do
        if [[ -z "${SEEN_INDICES[$idx]:-}" ]]; then
            SEEN_INDICES[$idx]=1
            UNIQUE_INDICES+=("$idx")
        fi
    done

    # --- Run Selected Tests ---
    local -a RES_NAMES=()
    local -a RES_STATUS=()
    local -a RES_DURATIONS=()
    local -a RES_FOLDERS=()

    local SELECTED_COUNT=${#UNIQUE_INDICES[@]}
    local SUITE_START
    SUITE_START=$(date +%s)
    local PASSED_COUNT=0
    local FAILED_COUNT=0

    echo ""
    echo "======================================================================"
    echo " SMRTk Test Runner — Running ${SELECTED_COUNT} test(s)"
    echo " TSV File : $(basename "$TSV_FILE")"
    echo " Format   : ${FORMAT} | Verbosity: ${VERBOSITY} | Followup Analysis: ${RUN_ANALYZE}"
    echo "======================================================================"

    for step in "${!UNIQUE_INDICES[@]}"; do
        local idx="${UNIQUE_INDICES[$step]}"
        local step_num=$((step + 1))
        local name="${TEST_NAMES[$idx]}"
        local desc="${TEST_DESCS[$idx]}"
        local raw_prompt="${TEST_PROMPTS[$idx]}"
        local raw_followup="${TEST_FOLLOWUPS[$idx]}"

        local session_id="Session_ID_${name}"
        local session_dir="sessions/${session_id}"

        # Expand variables and escaped newlines in prompts
        local prompt="${raw_prompt//\\n/$'\n'}"
        prompt="${prompt//\{SESSION_NAME\}/$name}"
        prompt="${prompt//\{SESSION_ID\}/$session_id}"
        prompt="${prompt//\{LOGS_DIR\}/\/app\/workspace\/logs}"
        prompt="${prompt//\{OUTPUTS_DIR\}/\/app\/workspace\/outputs}"
        prompt="${prompt//\{WORKSPACE_DIR\}/\/app\/workspace}"
        prompt="${prompt//\{HOST_SESSION_DIR\}/$session_dir}"
        prompt="${prompt//sessions\/$session_id\//\/app\/workspace\/}"
        prompt="${prompt//\{SESSION_DIR\}/\/app\/workspace}"

        local followup="${raw_followup//\\n/$'\n'}"
        followup="${followup//\{SESSION_NAME\}/$name}"
        followup="${followup//\{SESSION_ID\}/$session_id}"
        followup="${followup//\{LOGS_DIR\}/\/app\/workspace\/logs}"
        followup="${followup//\{OUTPUTS_DIR\}/\/app\/workspace\/outputs}"
        followup="${followup//\{WORKSPACE_DIR\}/\/app\/workspace}"
        followup="${followup//\{HOST_SESSION_DIR\}/$session_dir}"
        followup="${followup//sessions\/$session_id\//\/app\/workspace\/}"
        followup="${followup//\{SESSION_DIR\}/\/app\/workspace}"

        echo ""
        echo "----------------------------------------------------------------------"
        echo "[${step_num}/${SELECTED_COUNT}] ${name}: ${desc}"
        echo "Session: ${session_id} (${session_dir})"
        echo "----------------------------------------------------------------------"

        if [[ "$DRY_RUN" == true ]]; then
            echo "[DRY-RUN] pixi run python smrtk.py -x -f \"$FORMAT\" -v \"$VERBOSITY\" -s \"$name\" -p \"${prompt:0:80}...\""
            if [[ "$RUN_ANALYZE" == true && -n "$followup" ]]; then
                echo "[DRY-RUN-FOLLOWUP] pixi run python smrtk.py -x -f \"$FORMAT\" -v \"$VERBOSITY\" -s \"$name\" -p \"${followup:0:80}...\""
            fi
            RES_NAMES+=("$name")
            RES_STATUS+=("DRY_RUN")
            RES_DURATIONS+=("0s")
            RES_FOLDERS+=("$session_dir")
            continue
        fi

        local T_START
        T_START=$(date +%s)

        # 1. Main prompt execution
        pixi run python smrtk.py -x -f "$FORMAT" -v "$VERBOSITY" -s "$name" -p "$prompt"
        local MAIN_EXIT=$?

        # 2. Optional follow-up prompt execution in same session
        local FOLLOWUP_EXIT=0
        if [[ "$RUN_ANALYZE" == true && -n "$followup" && $MAIN_EXIT -eq 0 ]]; then
            echo ""
            echo ">>> Running follow-up analysis for ${name}..."
            pixi run python smrtk.py -x -f "$FORMAT" -v "$VERBOSITY" -s "$name" -p "$followup"
            FOLLOWUP_EXIT=$?
        fi

        local T_END
        T_END=$(date +%s)
        local DUR=$((T_END - T_START))

        RES_NAMES+=("$name")
        RES_DURATIONS+=("${DUR}s")
        RES_FOLDERS+=("$session_dir")

        if [[ $MAIN_EXIT -eq 0 && $FOLLOWUP_EXIT -eq 0 ]]; then
            RES_STATUS+=("OK")
            ((PASSED_COUNT++))
            echo "✓ [${name}] Completed successfully in ${DUR}s"
        else
            local EXIT_CODE=$MAIN_EXIT
            [[ $MAIN_EXIT -eq 0 ]] && EXIT_CODE=$FOLLOWUP_EXIT
            RES_STATUS+=("FAIL_${EXIT_CODE}")
            ((FAILED_COUNT++))
            echo "✗ [${name}] Failed with exit code ${EXIT_CODE} after ${DUR}s"
        fi
    done

    local SUITE_END
    SUITE_END=$(date +%s)
    local TOTAL_DUR=$((SUITE_END - SUITE_START))

    # --- Compact Results Summary ---
    echo ""
    echo "======================================================================"
    echo " RESULTS SUMMARY"
    echo "======================================================================"
    printf "%-35s  %-10s  %-8s  %s\n" "Test Name" "Status" "Time" "Session Directory"
    echo "----------------------------------------------------------------------"
    for i in "${!RES_NAMES[@]}"; do
        printf "%-35s  %-10s  %-8s  %s\n" "${RES_NAMES[$i]}" "${RES_STATUS[$i]}" "${RES_DURATIONS[$i]}" "${RES_FOLDERS[$i]}"
    done
    echo "======================================================================"
    if [[ "$DRY_RUN" == true ]]; then
        echo "Dry run complete for ${SELECTED_COUNT} test(s)."
    else
        echo "Finished ${SELECTED_COUNT} test(s) in ${TOTAL_DUR}s (${PASSED_COUNT} passed, ${FAILED_COUNT} failed)."
    fi
    echo ""
}

main "$@"
